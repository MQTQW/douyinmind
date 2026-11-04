"""Read-only, non-repairing comparison of SQL material and index observations.

No default clients, settings, models or workers are constructed. Two observed
snapshots can detect change, not create an atomic cross-storage transaction.
Legacy index content stays unknown unless explicitly verified locally.
"""
from collections import Counter
from datetime import date, datetime
import hashlib
import json
import re
from types import SimpleNamespace

from sqlalchemy import select

from app.models.entities import KnowledgeDeletionOperation, VideoCache
from app.services.material_deletion import _fingerprint


_VIDEO = re.compile(r'[A-Za-z0-9_-]{1,64}\Z')
_UUID = re.compile(r'[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}\Z')
_HASH = re.compile(r'[a-f0-9]{64}\Z')
_ACTIVE = {'pending', 'index_deleted', 'files_deleted'}
_INDEX_FAILURES = {'index_read_unavailable', 'index_metadata_invalid', 'index_snapshot_changed',
                   'index_scan_limit', 'index_text_read_unavailable'}


def _digest(value):
    def scalar(item):
        if isinstance(item, (datetime, date)):
            return item.isoformat()
        raise TypeError()
    return hashlib.sha256(json.dumps(value, ensure_ascii=True, sort_keys=True, allow_nan=False,
                                     separators=(',', ':'), default=scalar).encode()).hexdigest()


def _safe_video(value):
    return value if type(value) is str and _VIDEO.fullmatch(value) else None


def _safe_operation(value):
    return value if type(value) is str and _UUID.fullmatch(value) else None


def _same(value, other):
    try:
        return _digest(value) == _digest(other)
    except Exception:
        return False


def _sql_snapshot(db):
    errors = []
    caches = operations = fingerprints = None
    with db.no_autoflush:
        try:
            caches = [dict(row) for row in db.execute(select(
                VideoCache.id, VideoCache.platform_item_id, VideoCache.status,
                VideoCache.processed_at, VideoCache.updated_at).order_by(VideoCache.id)).mappings()]
        except Exception:
            errors.append('sql_material_read_unavailable')
        try:
            operations = [dict(row) for row in db.execute(select(
                KnowledgeDeletionOperation.operation_id, KnowledgeDeletionOperation.platform_item_id,
                KnowledgeDeletionOperation.cache_id, KnowledgeDeletionOperation.material_fingerprint,
                KnowledgeDeletionOperation.active_key, KnowledgeDeletionOperation.state,
                KnowledgeDeletionOperation.revision).order_by(KnowledgeDeletionOperation.operation_id)).mappings()]
        except Exception:
            errors.append('sql_operation_read_unavailable')
        if caches is not None and operations is not None:
            # Source text is read only for an active deletion whose existing
            # cache identity/state already match; it is hashed, never returned.
            by_video = {row['platform_item_id']: row for row in caches}
            needed = sorted({row['cache_id'] for row in operations
                if row['active_key'] == 1 and type(row['cache_id']) is int
                and (cache := by_video.get(row['platform_item_id'])) is not None
                and cache['id'] == row['cache_id'] and cache['status'] == 'deleting'})
            fingerprints = {}
            try:
                for offset in range(0, len(needed), 500):
                    records = db.execute(select(VideoCache.id, VideoCache.platform_item_id,
                        VideoCache.transcript_text, VideoCache.visual_analysis_text,
                        VideoCache.analysis_json, VideoCache.analysis_model, VideoCache.analysis_duration_seconds,
                        VideoCache.summary, VideoCache.processed_at).where(
                            VideoCache.id.in_(needed[offset:offset + 500]))).mappings()
                    for row in records:
                        fingerprints[row['id']] = _fingerprint(SimpleNamespace(**dict(row)))
                if set(fingerprints) != set(needed):
                    raise ValueError()
            except Exception:
                fingerprints = None
                errors.append('sql_fingerprint_read_unavailable')
    try:
        signature = _digest({'caches': caches, 'operations': operations, 'fingerprints': fingerprints})
    except Exception:
        signature = None
        errors.append('sql_snapshot_invalid')
    return {'caches': caches, 'operations': operations, 'fingerprints': fingerprints,
            'hash': signature, 'errors': sorted(set(errors))}


def validate_index_snapshot(value):
    """Validate an exported census without treating absent values as zeros."""
    if (type(value) is not dict or value.get('read_complete') is not True
            or type(value.get('content_verified')) is not bool or not isinstance(value.get('counts'), list)
            or type(value.get('index_records')) is not int or value['index_records'] < 0
            or type(value.get('index_hash')) is not str or not _HASH.fullmatch(value['index_hash'])):
        raise ValueError('invalid_index_metadata_snapshot')
    rows, seen = [], set()
    for source in value['counts']:
        if type(source) is not dict or _safe_video(source.get('platform_item_id')) is None:
            raise ValueError('invalid_index_metadata_snapshot')
        key = source['platform_item_id']
        fields = ('total_chunks', 'verified_content_chunks', 'metadata_chunks', 'unverified_chunks')
        if key in seen or any(type(source.get(name)) is not int or source[name] < 0 for name in fields):
            raise ValueError('invalid_index_metadata_snapshot')
        if source['total_chunks'] < 1 or source['total_chunks'] != sum(source[name] for name in fields[1:]):
            raise ValueError('invalid_index_metadata_snapshot')
        verified = source['unverified_chunks'] == 0
        if ('content_chunks' not in source or source.get('content_verified') is not verified
                or (verified and (type(source.get('content_chunks')) is not int
                                  or source['content_chunks'] != source['verified_content_chunks']))
                or (not verified and source.get('content_chunks') is not None)):
            raise ValueError('invalid_index_metadata_snapshot')
        seen.add(key)
        rows.append({name: source[name] for name in ('platform_item_id', *fields,
                                                    'content_chunks', 'content_verified')})
    if sum(row['total_chunks'] for row in rows) != value['index_records']:
        raise ValueError('invalid_index_metadata_snapshot')
    if value['content_verified'] is not all(row['content_verified'] for row in rows):
        raise ValueError('invalid_index_metadata_snapshot')
    kind = value.get('source_kind')
    mode = value.get('verification_mode')
    return {'read_complete': True, 'content_verified': value['content_verified'], 'counts': sorted(rows, key=lambda row: row['platform_item_id']),
            'index_records': value['index_records'], 'index_hash': value['index_hash'],
            'verification_mode': mode if type(mode) is str and mode in {'metadata_only', 'local_legacy_text'} else 'metadata_only',
            'source_kind': kind if type(kind) is str and kind in {'live_index_metadata', 'provided_index_metadata_export'} else 'injected_snapshot'}


def _index_snapshot(chroma, verify_legacy_text):
    try:
        value = chroma.list_indexed_video_counts(verify_legacy_text=verify_legacy_text)
        return validate_index_snapshot(value), None
    except Exception as error:
        reason = getattr(error, 'reason', None)
        return None, reason if type(reason) is str and reason in _INDEX_FAILURES else 'index_read_unavailable'


def audit_materials(db, *, chroma, limit=50, offset=0, verify_legacy_text=False):
    """Observe only: no flush/commit, restoration, deletion or service calls.

    ``read_complete`` describes successful stable reads. ``content_verified``
    separately says whether index roles were verifiable. A provided export is
    explicitly a comparison with that export, not the current live index.
    Missing/failed source reads yield null totals, never invented zero counts.
    """
    if (type(limit) is not int or not 1 <= limit <= 200 or type(offset) is not int or offset < 0
            or type(verify_legacy_text) is not bool):
        raise ValueError('invalid_material_audit_page')
    before = _sql_snapshot(db)
    index, index_error = _index_snapshot(chroma, verify_legacy_text)
    after = _sql_snapshot(db)
    index_after, index_after_error = _index_snapshot(chroma, verify_legacy_text)
    errors = sorted(set(before['errors'] + after['errors'] + [error for error in (index_error, index_after_error) if error]))
    changed = (before['hash'] is not None and after['hash'] is not None and before['hash'] != after['hash'])
    if index is not None and index_after is not None:
        changed |= _digest(index) != _digest(index_after)
    if 'index_snapshot_changed' in errors:
        changed = True
    cache_known = (before['caches'] is not None and after['caches'] is not None
                   and _same(before['caches'], after['caches']) and not changed)
    operation_known = (before['operations'] is not None and after['operations'] is not None
                       and _same(before['operations'], after['operations']) and not changed)
    index_known = index is not None and index_after is not None and _same(index, index_after) and not changed
    read_complete = not errors and not changed
    snapshot_changed = bool(changed) if changed or not errors else None
    issues = []
    def issue(reason, video=None, operation=None, kind='issue'):
        row = {'reason': reason, 'kind': kind}
        if _safe_video(video) is not None: row['platform_item_id'] = video
        if _safe_operation(operation) is not None: row['operation_id'] = operation
        issues.append(row)
    for error in errors:
        issue(error, kind='unavailable')
    if changed:
        issue('audit_snapshot_changed', kind='review')
    caches = before['caches'] if cache_known else None
    operations = before['operations'] if operation_known else None
    fingerprints = before['fingerprints'] if (before['fingerprints'] is not None
        and before['fingerprints'] == after['fingerprints']) else None
    if not changed:
        by_video = {row['platform_item_id']: row for row in caches} if caches is not None else None
        by_index = {row['platform_item_id']: row for row in index['counts']} if index_known else None
        if by_video is not None:
            for cache in caches:
                if _safe_video(cache['platform_item_id']) is None:
                    issue('sql_material_identifier_invalid')
            if by_index is not None:
                for key, cache in by_video.items():
                    if cache['status'] != 'done': continue
                    row = by_index.get(key)
                    if row is None:
                        issue('sql_done_without_content_index', key)
                    elif row['verified_content_chunks'] == 0:
                        if row['unverified_chunks']:
                            issue('content_index_unverified', key, kind='review')
                        else:
                            issue('sql_done_metadata_only_index', key)
                for key in by_index:
                    if key not in by_video: issue('index_without_sql_material', key)
                    elif by_video[key]['status'] != 'done': issue('index_for_non_done_material', key)
        if operations is not None:
            active = {}
            for operation in operations:
                key, identity, state = operation['platform_item_id'], operation['operation_id'], operation['state']
                if _safe_operation(identity) is None or _safe_video(key) is None:
                    issue('deletion_operation_identifier_invalid', key, identity)
                if operation['active_key'] == 1:
                    active.setdefault(key, []).append(operation)
                valid_state = ((type(operation['active_key']) is int and operation['active_key'] == 1 and state in _ACTIVE)
                               or (operation['active_key'] is None and state == 'done'))
                if not valid_state:
                    issue('deletion_operation_state_invalid', key, identity)
            for key, records in active.items():
                if len(records) > 1: issue('multiple_active_deletions', key)
                if by_video is None: continue
                cache = by_video.get(key)
                for operation in records:
                    identity = operation['operation_id']
                    if cache is None: issue('active_deletion_cache_missing', key, identity)
                    elif cache['status'] != 'deleting': issue('active_deletion_state_mismatch', key, identity)
                    elif cache['id'] != operation['cache_id']: issue('active_deletion_material_changed', key, identity)
                    elif fingerprints is not None and fingerprints.get(cache['id']) != operation['material_fingerprint']:
                        issue('active_deletion_fingerprint_mismatch', key, identity)
            if by_video is not None:
                for key, cache in by_video.items():
                    if cache['status'] == 'deleting' and key not in active:
                        issue('deleting_without_active_operation', key)
    issues = [dict(value) for _, value in sorted({
        (row['reason'], row.get('platform_item_id', ''), row.get('operation_id', '')): row for row in issues}.items())]
    summary = {
        'sql_materials': len(caches) if caches is not None else None,
        'sql_done': sum(row['status'] == 'done' for row in caches) if caches is not None else None,
        'sql_deleting': sum(row['status'] == 'deleting' for row in caches) if caches is not None else None,
        'deletion_operations': len(operations) if operations is not None else None,
        'active_deletion_operations': sum(row['active_key'] == 1 for row in operations) if operations is not None else None,
        'index_videos': len(index['counts']) if index_known else None,
        'index_records': index['index_records'] if index_known else None,
        'verified_content_chunks': sum(row['verified_content_chunks'] for row in index['counts']) if index_known else None,
        'unverified_index_chunks': sum(row['unverified_chunks'] for row in index['counts']) if index_known else None,
        'issue_count': sum(row['kind'] == 'issue' for row in issues) if read_complete else None,
        'review_count': sum(row['kind'] == 'review' for row in issues) if read_complete else None,
    }
    page = issues[offset:offset + limit]
    content_verified = index['content_verified'] if index_known else None
    return {'schema_version': 1, 'read_only': True, 'repair_supported': False,
            'read_complete': read_complete, 'content_verified': content_verified,
            'complete': read_complete and content_verified is True, 'snapshot_changed': snapshot_changed,
            'summary': summary, 'issues': page, 'total_issues': len(issues) if read_complete else None,
            'next_offset': offset + len(page) if offset + len(page) < len(issues) else None,
            'snapshot': {'sql_before_hash': before['hash'], 'sql_after_hash': after['hash'],
                         'index_before_hash': index['index_hash'] if index is not None else None,
                         'index_after_hash': index_after['index_hash'] if index_after is not None else None,
                         'source_kind': index['source_kind'] if index is not None else None,
                         'verification_mode': index['verification_mode'] if index is not None else None,
                         'scope': 'two_observations_not_atomic_cross_storage_transaction'}}
