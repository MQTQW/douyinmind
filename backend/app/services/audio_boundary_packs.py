"""Parallel independent-window review with a separate, content-bound pack cache."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import tempfile
from threading import Lock
import time
import wave

from app.core.config import settings
from app.services.audio_boundary import propose_boundary_splice
from app.services.audio_boundary_batch import (BATCH_VERSION, BATCH_RULES, plan_boundary_batches,
    prepare_audio_pack, parse_batch_response, has_acoustic_uncertainty, KNOWN_CROP_WARNINGS)
from app.services.media_service import MediaPipelineError


class PackCache:
    def __init__(self, source: Path, fingerprint: dict):
        self.path = source.with_name(source.name + '.boundary-packs.json')
        self.lock = Lock()
        self.fingerprint = hashlib.sha256(json.dumps(fingerprint, sort_keys=True, ensure_ascii=False,
                                                     allow_nan=False).encode()).hexdigest()
        try:
            data = json.loads(self.path.read_text(encoding='utf-8'))
            self.packs = data['packs'] if data['fingerprint'] == self.fingerprint and isinstance(data['packs'], dict) else {}
        except (OSError, ValueError, TypeError, KeyError):
            self.packs = {}

    def get(self, index):
        with self.lock:
            return deepcopy(self.packs.get(str(index)))

    def put(self, index, raw, audio_sha):
        with self.lock:
            fresh = {**self.packs, str(index): {'raw_response': raw, 'audio_sha256': audio_sha}}
            target = None
            try:
                with tempfile.NamedTemporaryFile('w', encoding='utf-8', dir=self.path.parent,
                        prefix='.boundary-packs-', suffix='.tmp', delete=False) as stream:
                    target = Path(stream.name)
                    json.dump({'fingerprint': self.fingerprint, 'packs': fresh}, stream, ensure_ascii=False, allow_nan=False)
                target.replace(self.path)
                self.packs = fresh
            finally:
                if target is not None: target.unlink(missing_ok=True)


def review_boundary_packs(service, source: Path, duration: float, roots: list[dict]):
    from app.services.omni_service import OmniServiceError
    started = time.monotonic()
    original = deepcopy([part for root in roots for part in root['parts']])
    specs = plan_boundary_batches(roots, radius=settings.audio_boundary_batch_radius_seconds,
                                 max_windows=settings.audio_boundary_batch_max_windows)
    audit = {'strategy': BATCH_VERSION, 'acoustic_status': 'unverified_acoustic',
             'timing_kind': 'source_window_envelope_not_utterance_timestamps',
             'original_transcript_segments': original, 'reviews': [], 'pack_results': [],
             'recognition_attempts': 0, 'failed_recognition_attempts': 0, 'reused_windows': 0,
             'reused_packs': 0, 'accepted_windows': 0, 'http_attempts': None,
             'max_primary_windows': settings.audio_boundary_batch_max_windows,
             'concurrency': min(3, settings.audio_transcription_concurrency),
             'sdk_retries': 0, 'stage_admission_budget_seconds': settings.audio_boundary_review_budget_seconds}
    try:
        fingerprint = {'version': BATCH_VERSION, 'source_sha256': hashlib.sha256(source.read_bytes()).hexdigest(),
            'roots': roots, 'model': settings.qwen_omni_model, 'endpoint': settings.qwen_omni_base_url,
            'prompt': BATCH_RULES, 'max_tokens': settings.qwen_omni_max_tokens, 'specs': specs}
        cache = PackCache(source, fingerprint)
    except (OSError, ValueError) as error:
        audit.update(reason='pack_cache_unavailable', error_type=type(error).__name__, elapsed_seconds=time.monotonic()-started)
        return original, audit

    def recognize(pair):
        index, spec = pair
        row = {'pack_index': index, 'stage': spec['stage'], 'status': 'retained_original',
               'recognition_attempts': 0, 'reused': False}
        began = time.monotonic()
        try:
            if began - started >= settings.audio_boundary_review_budget_seconds:
                row['reason'] = 'stage_budget_exhausted'
                return row
            with prepare_audio_pack(source, spec['windows'], spec['radius'], duration) as (segment, layout):
                row.update(layout=layout, audio_sha256=hashlib.sha256(segment.path.read_bytes()).hexdigest(),
                           packed_duration_seconds=segment.duration)
                saved = cache.get(index)
                parsed = None
                if saved and saved.get('audio_sha256') == row['audio_sha256']:
                    try:
                        parsed = parse_batch_response(saved['raw_response'], layout, service)
                        row.update(raw_response=saved['raw_response'], reused=True)
                    except (OmniServiceError, KeyError, TypeError):
                        parsed = None
                if parsed is None:
                    remaining = settings.audio_boundary_review_budget_seconds - (time.monotonic()-started)
                    if remaining <= 0:
                        row['reason'] = 'stage_budget_exhausted'
                        return row
                    row['recognition_attempts'] = 1
                    previous = getattr(service._request_context, 'boundary_deadline', None)
                    try:
                        service._request_context.boundary_deadline = time.monotonic() + min(
                            settings.audio_boundary_review_timeout_seconds, remaining)
                        raw = service._recognize_audio_pack(segment, layout)
                    finally:
                        if previous is None: del service._request_context.boundary_deadline
                        else: service._request_context.boundary_deadline = previous
                    row['raw_response'] = raw
                    parsed = parse_batch_response(raw, layout, service)
                    row['parsed'] = parsed
                    cache.put(index, raw, row['audio_sha256'])
                row.update(status='success', parsed=parsed)
        except OmniServiceError as error:
            row.update(reason=error.code, raw_response=error.raw_response or row.get('raw_response'), error_type=type(error).__name__)
        except (OSError, ValueError, wave.Error, EOFError, MediaPipelineError) as error:
            row.update(reason='pack_unavailable', error_type=type(error).__name__)
        finally:
            row['elapsed_seconds'] = round(time.monotonic()-began, 6)
        return row

    with ThreadPoolExecutor(max_workers=max(1, audit['concurrency'])) as pool:
        packs = list(pool.map(recognize, enumerate(specs)))
    audit['pack_results'] = packs
    audit['recognition_attempts'] = sum(r['recognition_attempts'] for r in packs)
    audit['failed_recognition_attempts'] = sum(r['recognition_attempts'] for r in packs if r['status'] != 'success')
    audit['reused_packs'] = sum(r['reused'] for r in packs)
    audit['reused_windows'] = sum(len(r.get('layout', [])) for r in packs if r['reused'])
    heard = {}
    for pack in packs:
        if pack['status'] != 'success': continue
        for window in pack['layout']:
            analysis = pack['parsed'][window['clip_id']]
            parts = [{**p, 'start_seconds': p['start_seconds'] + window['start_seconds'],
                      'end_seconds': p['end_seconds'] + window['start_seconds']}
                     for p in analysis['transcript_segments']]
            heard[(pack['stage'], window['boundary_seconds'])] = {
                'parts': parts, 'warnings': analysis['warnings'], 'window': window, 'pack_index': pack['pack_index']}
    offsets = [0]
    for root in roots: offsets.append(offsets[-1] + len(root['parts']))
    applied = []
    primary_windows = [w for spec in specs if spec['stage'] == 'primary' for w in spec['windows']]
    for window in primary_windows:
        cut, index = window['boundary_seconds'], window['left_root_index']
        old = deepcopy(roots[index]['parts'] + roots[index+1]['parts'])
        entry = {**window, 'accepted': False, 'status': 'retained_original', 'before_parts': old, 'after_parts': old}
        audit['reviews'].append(entry)
        primary = heard.get(('primary', cut))
        confirmation = heard.get(('confirmation', cut))
        if primary is None:
            entry['reason'] = 'primary_pack_unavailable'
            continue
        entry.update(review_parts=primary['parts'], raw_response_ref=primary['pack_index'],
                     warnings=primary['warnings'], source_window=primary['window'])
        entry['non_blocking_window_edge_warnings'] = [w for w in primary['warnings'] if w.strip() in KNOWN_CROP_WARNINGS]
        if has_acoustic_uncertainty(primary['warnings'], primary['parts']):
            entry['reason'] = 'provider_uncertainty'
            continue
        corroboration = None
        if (confirmation and not has_acoustic_uncertainty(confirmation['warnings'], confirmation['parts'])
                and (confirmation['window']['start_seconds'], confirmation['window']['duration_seconds']) !=
                    (primary['window']['start_seconds'], primary['window']['duration_seconds'])):
            corroboration = confirmation['parts']
            entry.update(confirmation_review_parts=corroboration, confirmation_window=confirmation['window'],
                         confirmation_raw_response_ref=confirmation['pack_index'])
        proposal = propose_boundary_splice(roots[index]['parts'], roots[index+1]['parts'], primary['parts'],
                                          confirmation_parts=corroboration)
        entry.update(proposal)
        if proposal['accepted']:
            first, last = (offsets[index] + proposal['anchors'][key] for key in ('record_start', 'record_end'))
            if any(not (last < a or first > b) for a, b, _ in applied):
                entry.update(accepted=False, reason='overlapping_original_records', after_parts=old)
            else:
                applied.append((first, last, proposal['after_parts'][proposal['anchors']['record_start']]))
                entry['status'] = 'accepted_bounded_splice'
    final = deepcopy(original)
    for first, last, replacement in sorted(applied, reverse=True):
        final[first:last+1] = [replacement]
    final.sort(key=lambda p: p['start_seconds'])
    audit.update(accepted_windows=len(applied), elapsed_seconds=round(time.monotonic()-started, 6),
                 primary_windows=len(primary_windows), confirmation_windows=sum(len(s['windows']) for s in specs if s['stage']=='confirmation'))
    return final, audit
