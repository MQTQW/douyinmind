"""Explicit, resumable deletion across SQL, the index and owned cache files.

The caller must hold the backend's maintenance exclusion for the whole call.
Only one backend owner is supported. No model, import submission or background
recovery is started here. A committed intent isolates material before external
deletion, and the original source text survives until the final SQL commit.
"""
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import stat
import uuid

from sqlalchemy import delete, select, update
from sqlalchemy.exc import SQLAlchemyError

from app.models.entities import KnowledgeBaseVideo, KnowledgeDeletionOperation, VideoCache


PHASES = ('pending', 'index_deleted', 'files_deleted', 'done')
_MEDIA = (('.mp3', True), ('.wav', True), ('.m4a', True), ('.webm', True), ('.opus', True))
_VIDEO = ('.mp4', '.mkv', '.webm', '.mov')
# AudioCheckpoint permits analysis/boundary-analysis; PackCache writes the
# separate boundary-packs sidecar. These may retain recognized source text.
_AUDIO_SIDECARS = ('.analysis.json', '.boundary-analysis.json', '.boundary-packs.json')
_MESSAGES = {
    'intent_commit_failed': '删除意图未能确认保存，尚未开始外部删除；请保留记录并核对。',
    'index_delete_failed': '索引删除尚未确认，原文和删除记录已保留，可显式继续。',
    'files_delete_failed': '缓存清理尚未完成，原文和删除记录已保留，可显式继续。',
    'progress_commit_failed': '删除进度未能确认保存，记录已保留，请显式继续核对。',
    'final_commit_failed': '删除的最终提交尚未确认，记录已保留，请显式继续核对。',
    'material_state_changed': '资料状态或内容已变化，未继续旧删除操作，请核对原记录。',
    'operation_not_found': '没有找到这条删除操作，未执行外部删除。',
    'operation_state_invalid': '删除记录状态无法确认，未继续执行，请核对原记录。',
    'operation_conflict': '删除记录已变化，未继续当前步骤，请核对原操作。',
    'deletion_intent_missing': '资料有删除标记但缺少匹配记录，未继续外部删除，请核对。',
    'operation_lookup_failed': '删除记录暂时无法读取，未继续外部删除，请保留记录后重试。',
}


class DeletionIncomplete(RuntimeError):
    def __init__(self, operation_id, reason):
        if reason not in _MESSAGES:
            raise ValueError('invalid_deletion_reason')
        self.operation_id, self.reason, self.code = operation_id, reason, reason
        self.message = _MESSAGES[reason]
        super().__init__(self.message)


def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _rollback(db):
    try:
        db.rollback()
    except Exception:
        # Never leave destructive dirty ORM values available for a caller's
        # later automatic commit when rollback itself lost its connection.
        try:
            db.invalidate()
        except Exception:
            try:
                db.close()
            except Exception:
                pass


def _fingerprint(cache):
    processed = cache.processed_at
    if processed is not None and processed.tzinfo is not None:
        processed = processed.astimezone(timezone.utc).replace(tzinfo=None)
    value = {'cache_id': cache.id, 'platform_item_id': cache.platform_item_id,
             'transcript_text': cache.transcript_text, 'visual_analysis_text': cache.visual_analysis_text,
             'analysis_json': cache.analysis_json, 'analysis_model': cache.analysis_model,
             'analysis_duration_seconds': float(cache.analysis_duration_seconds or 0),
             'summary': cache.summary, 'processed_at': processed.isoformat() if processed else None}
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     allow_nan=False, separators=(',', ':')).encode()).hexdigest()


def describe_operation(operation):
    return {'operation_id': operation.operation_id, 'platform_item_id': operation.platform_item_id,
            'deletion_state': operation.state, 'last_error_code': operation.last_error_code,
            'active': operation.active_key == 1}


def _done(operation, *, already_completed=False):
    return {'platform_item_id': operation.platform_item_id, 'status': 'pending',
            'operation_id': operation.operation_id, 'deletion_state': 'done',
            'already_completed': already_completed}


def _load(db, operation_id):
    try:
        return db.scalar(select(KnowledgeDeletionOperation).where(
            KnowledgeDeletionOperation.operation_id == operation_id).execution_options(populate_existing=True))
    except SQLAlchemyError:
        _rollback(db)
        raise DeletionIncomplete(operation_id, 'operation_lookup_failed') from None


def _cache(db, video_id):
    try:
        return db.scalar(select(VideoCache).where(VideoCache.platform_item_id == video_id)
                         .execution_options(populate_existing=True))
    except SQLAlchemyError:
        _rollback(db)
        raise DeletionIncomplete(None, 'operation_lookup_failed') from None


def _check_material(db, operation):
    try:
        cache = _cache(db, operation.platform_item_id)
    except DeletionIncomplete as error:
        raise DeletionIncomplete(operation.operation_id, error.reason) from None
    if (operation.active_key != 1 or operation.state not in PHASES[:-1]
            or cache is None or cache.status != 'deleting' or cache.id != operation.cache_id
            or _fingerprint(cache) != operation.material_fingerprint):
        raise DeletionIncomplete(operation.operation_id, 'material_state_changed')
    return cache


def _failed(db, operation_id, reason):
    # Roll back only this SQL transaction. It cannot undo external deletion.
    try:
        _rollback(db)
        db.execute(update(KnowledgeDeletionOperation).where(
            KnowledgeDeletionOperation.operation_id == operation_id,
            KnowledgeDeletionOperation.active_key == 1).values(last_error_code=reason, updated_at=_now()))
        db.commit()
    except Exception:
        _rollback(db)
    raise DeletionIncomplete(operation_id, reason) from None


def _advance(db, operation_id, expected, following):
    try:
        operation = _load(db, operation_id)
        if operation is None:
            raise DeletionIncomplete(operation_id, 'operation_not_found')
        if operation.state == 'done' and operation.active_key is None:
            return operation
        _check_material(db, operation)
        if operation.state != expected:
            if operation.state in PHASES and PHASES.index(operation.state) > PHASES.index(expected):
                return operation
            raise DeletionIncomplete(operation_id, 'operation_conflict')
        changed = db.execute(update(KnowledgeDeletionOperation).where(
            KnowledgeDeletionOperation.operation_id == operation_id,
            KnowledgeDeletionOperation.active_key == 1,
            KnowledgeDeletionOperation.state == expected,
            KnowledgeDeletionOperation.revision == operation.revision).values(
                state=following, revision=operation.revision + 1, last_error_code='', updated_at=_now()))
        if changed.rowcount != 1:
            raise DeletionIncomplete(operation_id, 'operation_conflict')
        db.commit()
        return _load(db, operation_id)
    except DeletionIncomplete:
        _rollback(db)
        raise
    except Exception:
        _failed(db, operation_id, 'progress_commit_failed')


def _files(root, video_id, extensions, *, sidecars=False):
    root = Path(root).absolute()
    if root.resolve() != root or root.is_symlink() or (root.exists() and not root.is_dir()):
        raise ValueError('redirected_media_root')
    for extension in extensions:
        target = root / (video_id + extension)
        if target.parent.resolve() != root:
            raise ValueError('redirected_media_path')
        yield target
        if sidecars:
            for suffix in _AUDIO_SIDECARS:
                yield target.with_name(target.name + suffix)


def _validate_media_file(path):
    try:
        info = path.lstat()
    except FileNotFoundError:
        return
    if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or path.is_symlink() or path.resolve() != path
            or getattr(info, 'st_file_attributes', 0) & 0x400):
        raise ValueError('redirected_or_linked_media_file')


def _delete_media(video_id, audio_dir, video_dir):
    paths = [*_files(audio_dir, video_id, [extension for extension, _ in _MEDIA], sidecars=True),
             *_files(video_dir, video_id, _VIDEO),
             # audio-only download retains the original MP4 here, outside the
             # short-lived conversion directory and the separate video cache.
             *_files(Path(audio_dir) / 'source_media', video_id, ('.mp4',))]
    # Validate the complete deletion set before unlinking its first file.
    for path in paths:
        _validate_media_file(path)
    for path in paths:
        _validate_media_file(path)
        path.unlink(missing_ok=True)
    # A successful unlink return alone is not an absence observation (a late
    # writer could have recreated a file). Do not finalize SQL on ambiguity.
    for path in paths:
        try:
            path.lstat()
        except FileNotFoundError:
            continue
        raise ValueError('media_clear_unconfirmed')


def _finalize(db, operation_id):
    try:
        operation = _load(db, operation_id)
        if operation is None:
            raise DeletionIncomplete(operation_id, 'operation_not_found')
        if operation.state == 'done' and operation.active_key is None:
            return _done(operation, already_completed=True)
        cache = _check_material(db, operation)
        if operation.state != 'files_deleted':
            raise DeletionIncomplete(operation_id, 'operation_conflict')
        changed = db.execute(update(KnowledgeDeletionOperation).where(
            KnowledgeDeletionOperation.operation_id == operation_id,
            KnowledgeDeletionOperation.active_key == 1,
            KnowledgeDeletionOperation.state == 'files_deleted',
            KnowledgeDeletionOperation.revision == operation.revision).values(
                state='done', active_key=None, revision=operation.revision + 1,
                last_error_code='', updated_at=_now(), completed_at=_now()))
        if changed.rowcount != 1:
            raise DeletionIncomplete(operation_id, 'operation_conflict')
        db.execute(delete(KnowledgeBaseVideo).where(
            KnowledgeBaseVideo.platform_item_id == operation.platform_item_id))
        cache.status = 'pending'
        cache.transcript_text = cache.visual_analysis_text = cache.analysis_model = cache.summary = ''
        cache.analysis_json = '{}'
        cache.analysis_duration_seconds = 0
        cache.error_message = ''
        cache.processed_at = None
        db.commit()
        return _done(operation)
    except DeletionIncomplete:
        _rollback(db)
        raise
    except Exception:
        # A commit can finish durably and then lose its acknowledgement. The
        # atomic done marker confirms that this operation already completed;
        # reading it cannot grant authority over a later import incarnation.
        _rollback(db)
        try:
            confirmed = _load(db, operation_id)
            if confirmed is not None and confirmed.state == 'done' and confirmed.active_key is None:
                return _done(confirmed, already_completed=True)
        except Exception:
            pass
        _failed(db, operation_id, 'final_commit_failed')


def delete_material(db, platform_item_id, *, chroma, audio_dir, video_dir, expected_operation_id=None):
    """Create/continue one explicitly requested operation; never replay a done one.

    This function owns its SQL commit boundaries. External failures preserve
    the active intent and source text, but already deleted cache/index entries
    are not claimed to have been restored. Only explicit caller actions resume.
    """
    if type(platform_item_id) is not str or not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', platform_item_id):
        raise ValueError('视频编号无效')
    if expected_operation_id is not None:
        try:
            if type(expected_operation_id) is not str or str(uuid.UUID(expected_operation_id)) != expected_operation_id:
                raise ValueError()
        except (ValueError, AttributeError, TypeError):
            raise ValueError('删除操作编号无效') from None
        operation = _load(db, expected_operation_id)
        if operation is None:
            raise DeletionIncomplete(expected_operation_id, 'operation_not_found')
        if operation.platform_item_id != platform_item_id:
            raise DeletionIncomplete(expected_operation_id, 'operation_conflict')
        # Do this before reading material or inspecting external paths: an old
        # completed operation has no authority over a later import incarnation.
        if operation.state == 'done' and operation.active_key is None:
            return _done(operation, already_completed=True)
    else:
        try:
            operation = db.scalar(select(KnowledgeDeletionOperation).where(
                KnowledgeDeletionOperation.platform_item_id == platform_item_id,
                KnowledgeDeletionOperation.active_key == 1).execution_options(populate_existing=True))
        except SQLAlchemyError:
            _rollback(db)
            raise DeletionIncomplete(None, 'operation_lookup_failed') from None
    if operation is None:
        cache = _cache(db, platform_item_id)
        if cache is None:
            raise ValueError('视频不存在')
        if cache.status in {'downloading', 'transcribing', 'embedding'}:
            raise ValueError('视频正在入库中，无法删除')
        if cache.status == 'deleting':
            raise DeletionIncomplete(None, 'deletion_intent_missing')
        operation_id = str(uuid.uuid4())
        try:
            operation = KnowledgeDeletionOperation(
                operation_id=operation_id, platform_item_id=platform_item_id,
                cache_id=cache.id, material_fingerprint=_fingerprint(cache),
                active_key=1, state='pending', revision=1, last_error_code='')
            db.add(operation)
            cache.status = 'deleting'
            db.commit()
        except Exception:
            _rollback(db)
            raise DeletionIncomplete(operation_id, 'intent_commit_failed') from None
    operation_id = operation.operation_id
    operation = _load(db, operation_id)
    if operation is None or operation.state not in PHASES[:-1] or operation.active_key != 1:
        raise DeletionIncomplete(operation_id, 'operation_state_invalid')
    _check_material(db, operation)
    if operation.state == 'pending':
        try:
            chroma.delete_by_video(platform_item_id)
        except Exception:
            _failed(db, operation_id, 'index_delete_failed')
        operation = _advance(db, operation_id, 'pending', 'index_deleted')
    if operation.state == 'done':
        return _done(operation, already_completed=True)
    if operation.state in {'index_deleted', 'files_deleted'}:
        _check_material(db, operation)
        try:
            _delete_media(platform_item_id, audio_dir, video_dir)
        except Exception:
            _failed(db, operation_id, 'files_delete_failed')
        if operation.state == 'index_deleted':
            operation = _advance(db, operation_id, 'index_deleted', 'files_deleted')
    if operation.state == 'done':
        return _done(operation, already_completed=True)
    return _finalize(db, operation_id)
