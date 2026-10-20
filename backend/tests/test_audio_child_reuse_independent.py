"""Independent black-box child-cache adversarial tests: real PCM, fake ASR only."""
from contextlib import contextmanager
import struct
import threading
import wave

import pytest

from app.core.config import settings
from app.services import omni_service as module
from app.services.audio_checkpoint import AudioCheckpoint
from app.services.audio_media import AudioSegment
from app.services.audio_media import split_audio_segment
from app.services.omni_service import AUDIO_PROMPT, AudioAnalysis, OmniService, OmniServiceError


def analysis(text, duration):
    return {'transcript_segments': [{'start_seconds': 0, 'end_seconds': duration, 'text': text}], 'warnings': []}


def fingerprint():
    return {'schema_version': 1, 'model': settings.qwen_omni_model,
            'endpoint': settings.qwen_omni_base_url, 'prompt': AUDIO_PROMPT,
            'max_tokens': settings.qwen_omni_max_tokens, 'segment_seconds': settings.video_segment_seconds}


def prepare(tmp_path, monkeypatch, *, total=80, root_seconds=80, concurrency=1):
    monkeypatch.setattr(settings, 'audio_boundary_review_enabled', False)
    monkeypatch.setattr(settings, 'qwen_omni_api_key', 'synthetic-never-network')
    monkeypatch.setattr(settings, 'qwen_omni_model', 'synthetic-review-model')
    monkeypatch.setattr(settings, 'video_segment_seconds', root_seconds)
    monkeypatch.setattr(settings, 'audio_transcription_concurrency', concurrency)
    rate = 1000
    source = tmp_path/'complete.wav'
    pcm = b''.join(struct.pack('<h', i % 29000) for i in range(total*rate))
    def write(path, block):
        with wave.open(str(path), 'wb') as output:
            output.setparams((1, 2, rate, 0, 'NONE', 'not compressed'))
            output.writeframes(block)
    write(source, pcm)
    roots = []
    for index, start in enumerate(range(0, total, root_seconds)):
        duration = min(root_seconds, total-start)
        target = tmp_path/f'root-{index}.wav'
        write(target, pcm[start*rate*2:(start+duration)*rate*2])
        roots.append(AudioSegment(target, start, duration))
    @contextmanager
    def prepared(path):
        assert path == source
        yield total, iter(roots)
    monkeypatch.setattr(module, 'prepare_audio_segments', prepared)
    checkpoint = AudioCheckpoint(source, fingerprint())
    return source, roots, checkpoint


def seed(checkpoint, path, start, duration, text, *, value=None):
    checkpoint.record_success(AudioSegment(path, start, duration),
                              value if value is not None else analysis(text, duration), 'cached-'+text)


def test_three_cached_leaves_do_not_spend_budget_or_inflate_attempts(tmp_path, monkeypatch):
    source, roots, checkpoint = prepare(tmp_path, monkeypatch)
    for start in (0, 20, 40):
        seed(checkpoint, source, start, 20, f'cached-{start}')
    service, calls, counts = OmniService(), [], {}
    def recognize(part):
        calls.append((part.start, part.duration))
        key = (part.start, part.duration)
        counts[key] = counts.get(key, 0)+1
        if part.duration > 20 or counts[key] == 1:
            raise OmniServiceError('synthetic malformed', code='invalid_json', raw_response='bad')
        return AudioAnalysis.model_validate(analysis('fresh-last', part.duration)), 'fresh-last'
    monkeypatch.setattr(service, '_analyze_audio_segment', recognize)
    result = service.analyze_audio(source)
    assert len(calls) == 8
    assert (0, 20) not in calls and (20, 20) not in calls and (40, 20) not in calls
    assert [t['text'] for t in result.payload['transcript_segments']] == ['cached-0', 'cached-20', 'cached-40', 'fresh-last']
    stats = result.payload['audio_processing']
    assert stats['recognition_attempts'] == 8 and stats['failed_recognition_attempts'] == 7
    assert stats['reused_root_segments'] == 0 and stats['reused_child_segments'] == 3


def test_cached_leaves_can_finish_after_tenth_real_request_exhausts_budget(tmp_path, monkeypatch):
    source, roots, checkpoint = prepare(tmp_path, monkeypatch)
    for start in (40, 60):
        seed(checkpoint, source, start, 20, f'cached-{start}')
    service, calls, counts = OmniService(), [], {}
    def recognize(part):
        calls.append((part.start, part.duration))
        key = (part.start, part.duration)
        counts[key] = counts.get(key, 0)+1
        if part.duration > 20 or counts[key] == 1:
            raise OmniServiceError('synthetic malformed', code='invalid_json', raw_response='bad')
        return AudioAnalysis.model_validate(analysis(f'fresh-{part.start}', part.duration)), 'fresh'
    monkeypatch.setattr(service, '_analyze_audio_segment', recognize)
    result = service.analyze_audio(source)
    assert len(calls) == 10
    assert (40, 20) not in calls and (60, 20) not in calls
    assert result.payload['audio_processing']['reused_child_segments'] == 2
    assert [(t['start_seconds'], t['end_seconds']) for t in result.payload['transcript_segments']] == [(0, 20), (20, 40), (40, 60), (60, 80)]


@pytest.mark.parametrize('change', ['source', 'model', 'prompt', 'max_tokens'])
def test_child_cache_never_crosses_source_or_recognition_configuration(tmp_path, monkeypatch, change):
    source, roots, checkpoint = prepare(tmp_path, monkeypatch, total=20, root_seconds=20)
    seed(checkpoint, source, 0, 10, 'forbidden-old-cache')
    if change == 'source':
        data = bytearray(source.read_bytes());data[-1] ^= 1;source.write_bytes(data)
    elif change == 'model':
        monkeypatch.setattr(settings, 'qwen_omni_model', 'another-synthetic-model')
    elif change == 'prompt':
        monkeypatch.setattr(module, 'AUDIO_PROMPT', AUDIO_PROMPT+' changed-for-isolation-test')
    else:
        monkeypatch.setattr(settings, 'qwen_omni_max_tokens', settings.qwen_omni_max_tokens+1)
    service, calls = OmniService(), []
    def recognize(part):
        calls.append((part.start, part.duration))
        if part.duration > 10:
            raise OmniServiceError('synthetic malformed', code='invalid_json', raw_response='bad')
        return AudioAnalysis.model_validate(analysis(f'fresh-{part.start}', part.duration)), 'fresh'
    monkeypatch.setattr(service, '_analyze_audio_segment', recognize)
    result = service.analyze_audio(source)
    assert len(calls) == 4
    assert 'forbidden-old-cache' not in result.transcript_text
    assert result.payload['audio_processing']['reused_child_segments'] == 0


@pytest.mark.parametrize('invalid', [
    {'warnings': []},
    {'transcript_segments': [{'start_seconds': 0, 'end_seconds': 11, 'text': 'invalid-time'}]},
    {'transcript_segments': [{'start_seconds': 10, 'end_seconds': 10.1, 'text': 'invalid-start'}]},
])
def test_schema_or_time_invalid_child_cache_is_a_miss_not_a_reuse(tmp_path, monkeypatch, invalid):
    source, roots, checkpoint = prepare(tmp_path, monkeypatch, total=20, root_seconds=20)
    seed(checkpoint, source, 0, 10, 'invalid-cache', value=invalid)
    service, calls = OmniService(), []
    def recognize(part):
        calls.append((part.start, part.duration))
        if part.duration > 10:
            raise OmniServiceError('synthetic malformed', code='invalid_json', raw_response='bad')
        return AudioAnalysis.model_validate(analysis(f'fresh-{part.start}', part.duration)), 'fresh'
    monkeypatch.setattr(service, '_analyze_audio_segment', recognize)
    result = service.analyze_audio(source)
    assert len(calls) == 4
    assert result.payload['audio_processing']['reused_child_segments'] == 0
    assert 'invalid-' not in result.transcript_text


def test_parallel_roots_with_same_local_child_offsets_keep_absolute_spans_and_counts(tmp_path, monkeypatch):
    source, roots, checkpoint = prepare(tmp_path, monkeypatch, total=160, root_seconds=80, concurrency=2)
    seed(checkpoint, source, 0, 40, 'cache-A')
    seed(checkpoint, source, 80, 40, 'cache-B')
    service, calls, lock, barrier = OmniService(), [], threading.Lock(), threading.Barrier(2)
    seen = set()
    def recognize(part):
        with lock:
            calls.append((part.start, part.duration))
            first = part.duration == 80 and part.start not in seen
            if part.duration == 80:
                seen.add(part.start)
        if first:
            barrier.wait(timeout=5)
        if part.duration == 80:
            raise OmniServiceError('synthetic malformed', code='invalid_json', raw_response='bad')
        return AudioAnalysis.model_validate(analysis(f'fresh-{part.start}', part.duration)), 'fresh'
    monkeypatch.setattr(service, '_analyze_audio_segment', recognize)
    first = service.analyze_audio(source)
    assert len(calls) == 6
    assert [(t['start_seconds'], t['end_seconds'], t['text']) for t in first.payload['transcript_segments']] == [
        (0, 40, 'cache-A'), (40, 80, 'fresh-40.0'), (80, 120, 'cache-B'), (120, 160, 'fresh-120.0')]
    stats = first.payload['audio_processing']
    assert stats['recognition_attempts'] == 6 and stats['failed_recognition_attempts'] == 4
    assert stats['reused_child_segments'] == 2 and stats['reused_root_segments'] == 0
    calls.clear()
    second = service.analyze_audio(source)
    assert calls == []
    assert second.transcript_text == first.transcript_text
    stats = second.payload['audio_processing']
    assert stats['reused_root_segments'] == 2 and stats['reused_child_segments'] == 0
    assert stats['recognition_attempts'] == stats['failed_recognition_attempts'] == 0


def test_odd_sample_root_at_nonzero_time_and_repeated_reads_keep_correct_offsets(tmp_path, monkeypatch):
    rate, count = 8000, 168001
    pcm = b''.join(struct.pack('<h', i % 29000) for i in range(count))
    source = tmp_path/'odd.wav'
    with wave.open(str(source), 'wb') as output:
        output.setparams((1, 2, rate, 0, 'NONE', 'not compressed'))
        output.writeframes(pcm)
    root = AudioSegment(source, 120, count/rate)
    checkpoint = AudioCheckpoint(source, {'model': 'synthetic-review-model'})
    with split_audio_segment(root) as children:
        left, right = children
        checkpoint.record_success(left, analysis('cached-left', left.duration), 'cached-left-raw')
        expected_right_start, expected_right_duration = right.start, right.duration
    service, calls, attempts, reuses, lookups = OmniService(), [], [], [], []
    def recognize(part):
        calls.append((part.start, part.duration))
        if part.path == source:
            raise OmniServiceError('synthetic malformed root', code='invalid_json', raw_response='bad')
        assert (part.start, part.duration) == (expected_right_start, expected_right_duration)
        with wave.open(str(part.path)) as actual:
            assert actual.readframes(actual.getnframes()) == pcm[(count//2)*2:]
        return AudioAnalysis.model_validate(analysis('fresh-right', part.duration)), 'fresh-right-raw'
    def load(part):
        lookups.append((part.start, part.duration))
        first = checkpoint.get(part)
        second = checkpoint.get(part)
        assert first == second
        return first
    monkeypatch.setattr(service, '_analyze_audio_segment', recognize)
    result, raw = service._analyze_audio_resilient(root,
        on_attempt=lambda *args: attempts.append(args), load_saved=load,
        on_reuse=lambda part: reuses.append((part.start, part.duration)))
    assert len(calls) == len(attempts) == 3
    assert len(lookups) == 2 and reuses == [(120, (count//2)/rate)]
    assert [(x.start_seconds, x.end_seconds) for x in result.transcript_segments] == [
        (0, (count//2)/rate), ((count//2)/rate, count/rate)]
    assert [x.text for x in result.transcript_segments] == ['cached-left', 'fresh-right']
    assert 'cached-left-raw' in raw and 'fresh-right-raw' in raw
