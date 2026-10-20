"""Concurrent transcription must preserve complete source order and resumability.

All recognition is replaced with local fakes. Only the final media-lifetime test
uses FFmpeg, against a small generated PCM recording under pytest's temp folder.
"""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import json
from pathlib import Path
import struct
import tempfile
from threading import Barrier, Event, Lock, get_ident
import wave

import pytest
from pydantic import ValidationError

from app.core.config import Settings, settings
from app.services import audio_media, omni_service as omni_module
from app.services.audio_media import AudioSegment
from app.services.media_service import MediaPipelineError, _resolve_ffmpeg_path
from app.services.omni_service import AudioAnalysis, OmniService, OmniServiceError


def write_pcm(path: Path, duration: int, marker: int = 1, rate: int = 16000) -> bytes:
    payload = struct.pack("<h", marker) * (duration * rate)
    with wave.open(str(path), "wb") as output:
        output.setparams((1, 2, rate, 0, "NONE", "not compressed"))
        output.writeframes(payload)
    return payload


def recognized(segment: AudioSegment) -> tuple[AudioAnalysis, str]:
    index = int(segment.start / 5)
    return AudioAnalysis.model_validate({
        "transcript_segments": [{
            "start_seconds": 0, "end_seconds": segment.duration,
            "text": f"第 {index} 段的完整原话。",
        }],
        "warnings": [f"第 {index} 段的不确定性"],
    }), f"original-response-{index}"


@pytest.fixture
def configured(monkeypatch, tmp_path):
    # These tests isolate root scheduling/recovery, not optional re-listening.
    monkeypatch.setattr(settings, "audio_boundary_review_enabled", False)
    monkeypatch.setattr(settings, "qwen_omni_api_key", "offline-unit-test-key")
    monkeypatch.setattr(settings, "qwen_omni_model", "offline-unit-test-model")
    monkeypatch.setattr(settings, "video_segment_seconds", 5)
    monkeypatch.setattr(settings, "audio_transcription_concurrency", 3)
    monkeypatch.setattr(settings, "audio_cache_dir", str(tmp_path / "audio-cache"))


def fake_parts(tmp_path, monkeypatch, *, count=3, on_yield=None):
    source = tmp_path / "complete-source.wav"
    write_pcm(source, count * 5)
    parts = []
    for index in range(count):
        target = tmp_path / f"part-{index}.wav"
        write_pcm(target, 5, index + 1)
        parts.append(AudioSegment(target, index * 5, 5))

    @contextmanager
    def prepare(path):
        assert path == source

        def produce():
            for index, part in enumerate(parts):
                if on_yield:
                    on_yield(index)
                yield part

        yield count * 5, produce()

    monkeypatch.setattr(omni_module, "prepare_audio_segments", prepare)
    return source, parts


def test_out_of_order_completion_preserves_all_text_raw_warnings_and_bounded_work(
    tmp_path, monkeypatch, configured,
):
    lock, first_three = Lock(), Barrier(3)
    second_finished, third_finished = Event(), Event()
    active = peak = produced = completed = 0
    check_bounded_work = True
    completion_order, progress = [], []
    caller_thread = get_ident()

    def on_yield(_):
        nonlocal produced
        if not check_bounded_work:
            return
        with lock:
            produced += 1
            assert produced - completed <= 3, "The generator must not enqueue the whole recording."

    source, parts = fake_parts(tmp_path, monkeypatch, count=7, on_yield=on_yield)
    service = OmniService()

    def recognize(part):
        nonlocal active, peak, completed
        index = int(part.start / 5)
        with lock:
            active += 1
            peak = max(peak, active)
        try:
            if index < 3:
                first_three.wait(timeout=5)
            if index == 0:
                assert second_finished.wait(timeout=5)
            elif index == 1:
                assert third_finished.wait(timeout=5)
            assert part.path.is_file()
            result = recognized(part)
            with lock:
                completion_order.append(index)
                completed += 1
            if index == 2:
                third_finished.set()
            elif index == 1:
                second_finished.set()
            return result
        finally:
            with lock:
                active -= 1

    def report(done, total):
        assert get_ident() == caller_thread, "UI progress must be emitted by the coordinator."
        progress.append((done, total))

    monkeypatch.setattr(service, "_analyze_audio_segment", recognize)
    result = service.analyze_audio(source, report)

    assert peak == 3
    assert completion_order.index(2) < completion_order.index(1) < completion_order.index(0)
    assert produced == completed == 7
    assert result.transcript_text.splitlines() == [f"第 {index} 段的完整原话。" for index in range(7)]
    assert [(item["start_seconds"], item["end_seconds"])
            for item in result.payload["transcript_segments"]] == [
        (part.start, part.start + part.duration) for part in parts
    ]
    assert [item["audio_response"] for item in result.payload["raw_results"]] == [
        f"original-response-{index}" for index in range(7)
    ]
    assert result.payload["warnings"] == [
        f"[00:{index * 5:02d}] 第 {index} 段的不确定性" for index in range(7)
    ]
    assert progress == [(index, 7) for index in range(8)]
    assert result.payload["audio_processing"]["recognition_attempts"] == 7
    assert result.payload["audio_processing"]["new_root_segments"] == 7
    assert result.payload["audio_processing"]["reused_root_segments"] == 0

    restarted = OmniService()
    check_bounded_work = False

    def no_network(part):
        raise AssertionError(f"Concurrent checkpoint lost the completed root at {part.start} seconds.")

    monkeypatch.setattr(restarted, "_analyze_audio_segment", no_network)
    reused = restarted.analyze_audio(source)
    assert reused.transcript_text == result.transcript_text
    assert reused.payload["raw_results"] == result.payload["raw_results"]
    assert reused.payload["audio_processing"]["reused_root_segments"] == 7
    assert reused.payload["audio_processing"]["new_root_segments"] == 0
    assert reused.payload["audio_processing"]["recognition_attempts"] == 0


def test_failure_waits_for_started_roots_and_resume_only_recognizes_failed_root(
    tmp_path, monkeypatch, configured,
):
    source, parts = fake_parts(tmp_path, monkeypatch, count=3)
    started = Barrier(3)
    failure_reached, release_successes = Event(), Event()
    first = OmniService()
    successful_reads = []
    lock = Lock()

    def first_recognition(part):
        started.wait(timeout=5)
        if part.start == 0:
            failure_reached.set()
            raise OmniServiceError("first root failed", code="api_error", raw_response="provider-failure")
        assert release_successes.wait(timeout=5)
        with wave.open(str(part.path)) as audio:
            assert audio.getnframes() == 80000
            assert len(audio.readframes(audio.getnframes())) == 160000
        with lock:
            successful_reads.append(part.start)
        return recognized(part)

    monkeypatch.setattr(first, "_analyze_audio_segment", first_recognition)
    with ThreadPoolExecutor(max_workers=1) as caller:
        future = caller.submit(first.analyze_audio, source)
        try:
            assert failure_reached.wait(timeout=5)
            assert not future.done(), "A failed root must not release storage still read by other workers."
        finally:
            release_successes.set()
        with pytest.raises(OmniServiceError, match="first root failed"):
            future.result(timeout=5)

    assert sorted(successful_reads) == [5, 10]
    saved = json.loads(source.with_name(source.name + ".analysis.json").read_text(encoding="utf-8"))
    entries = {item["start_seconds"]: item for item in saved["segments"].values()}
    assert entries[0]["attempts"][-1]["outcome"] == "failure"
    assert entries[5]["success"]["raw_response"] == "original-response-1"
    assert entries[10]["success"]["raw_response"] == "original-response-2"

    resumed, called = OmniService(), []

    def retry(part):
        called.append(part.start)
        assert part.start == 0, "Successful parallel roots must survive a sibling's failure."
        return recognized(part)

    monkeypatch.setattr(resumed, "_analyze_audio_segment", retry)
    progress = []
    result = resumed.analyze_audio(source, lambda done, total: progress.append((done, total)))
    assert called == [0]
    assert result.transcript_text.splitlines() == [f"第 {index} 段的完整原话。" for index in range(3)]
    assert [item["start_seconds"] for item in result.payload["raw_results"]] == [part.start for part in parts]
    assert result.payload["audio_processing"]["recognition_attempts"] == 1
    assert result.payload["audio_processing"]["new_root_segments"] == 1
    assert result.payload["audio_processing"]["reused_root_segments"] == 2
    assert progress == [(index, 3) for index in range(4)]


def test_lazy_generation_failure_waits_for_workers_before_context_cleanup(
    tmp_path, monkeypatch, configured,
):
    monkeypatch.setattr(settings, "audio_transcription_concurrency", 2)
    source = tmp_path / "source.wav"
    write_pcm(source, 15)
    second_started, release_second = Event(), Event()
    generation_failed = Event()
    temporary_paths = []
    second_read_completed = Event()

    @contextmanager
    def prepare(path):
        assert path == source
        with tempfile.TemporaryDirectory(dir=tmp_path, prefix="audio-parts-") as folder:
            def generate():
                for index in range(2):
                    target = Path(folder) / f"part-{index}.wav"
                    write_pcm(target, 5, index + 1)
                    temporary_paths.append(target)
                    yield AudioSegment(target, index * 5, 5)
                generation_failed.set()
                raise MediaPipelineError("third segment extraction failed")
            yield 15, generate()

    monkeypatch.setattr(omni_module, "prepare_audio_segments", prepare)
    service = OmniService()

    def recognize(part):
        if part.start == 0:
            assert second_started.wait(timeout=5)
        else:
            second_started.set()
            assert release_second.wait(timeout=5)
            # Context cleanup before draining active workers would delete this WAV.
            with wave.open(str(part.path)) as audio:
                assert len(audio.readframes(audio.getnframes())) == 160000
            second_read_completed.set()
        return recognized(part)

    monkeypatch.setattr(service, "_analyze_audio_segment", recognize)
    with ThreadPoolExecutor(max_workers=1) as caller:
        future = caller.submit(service.analyze_audio, source)
        try:
            assert generation_failed.wait(timeout=5)
            assert not future.done()
            assert all(path.is_file() for path in temporary_paths)
        finally:
            release_second.set()
        with pytest.raises(MediaPipelineError, match="third segment extraction failed"):
            future.result(timeout=5)
    assert second_read_completed.is_set()
    assert all(not path.exists() for path in temporary_paths)
    saved = json.loads(source.with_name(source.name + ".analysis.json").read_text(encoding="utf-8"))
    assert sorted(item["start_seconds"] for item in saved["segments"].values() if "success" in item) == [0, 5]


def test_real_pcm_ffmpeg_parts_remain_readable_until_workers_finish_and_are_cleaned(
    tmp_path, monkeypatch, configured,
):
    try:
        ffmpeg = _resolve_ffmpeg_path()
    except MediaPipelineError:
        bundled = Path(__file__).resolve().parents[1] / "app" / "storage" / "tools" / "ffmpeg.exe"
        if not bundled.is_file():
            pytest.skip("FFmpeg is unavailable for this local media integration check.")
        ffmpeg = str(bundled)
    monkeypatch.setattr(audio_media, "_resolve_ffmpeg_path", lambda: ffmpeg)
    source = tmp_path / "whole-recording.wav"
    expected = b"".join(struct.pack("<h", index + 1) * (5 * 16000) for index in range(3))
    with wave.open(str(source), "wb") as output:
        output.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
        output.writeframes(expected)
    all_started = Barrier(3)
    lock = Lock()
    read_pcm, temporary_paths = {}, []
    service = OmniService()

    def recognize(part):
        all_started.wait(timeout=10)
        # By now the coordinator has advanced the lazy generator twice. Earlier
        # segments must still exist while their recognition calls read them.
        with wave.open(str(part.path)) as audio:
            assert audio.getframerate() == 16000
            assert audio.getnframes() == 80000
            frames = audio.readframes(audio.getnframes())
        with lock:
            read_pcm[part.start] = frames
            temporary_paths.append(part.path)
        return recognized(part)

    monkeypatch.setattr(service, "_analyze_audio_segment", recognize)
    result = service.analyze_audio(source)
    assert b"".join(read_pcm[start] for start in sorted(read_pcm)) == expected
    assert [item["end_seconds"] for item in result.payload["transcript_segments"]] == [5, 10, 15]
    assert len(temporary_paths) == 3
    assert all(not path.exists() for path in temporary_paths)
    assert not list((tmp_path / "audio-cache").glob("segments-*"))
    assert source.is_file()


def test_serial_and_parallel_results_match_with_retry_split_and_short_tail(
    tmp_path, monkeypatch, configured,
):
    monkeypatch.setattr(settings, "video_segment_seconds", 20)
    outputs = []

    for concurrency in (1, 3):
        folder = tmp_path / f"concurrency-{concurrency}"
        folder.mkdir()
        source = folder / "source.wav"
        write_pcm(source, 47)
        parts = []
        for index, (start, duration) in enumerate(((0, 20), (20, 20), (40, 7))):
            target = folder / f"part-{index}.wav"
            write_pcm(target, duration, index + 1)
            parts.append(AudioSegment(target, start, duration))

        @contextmanager
        def prepare(path):
            assert path == source
            yield 47, iter(parts)

        monkeypatch.setattr(omni_module, "prepare_audio_segments", prepare)
        monkeypatch.setattr(settings, "audio_transcription_concurrency", concurrency)
        service = OmniService()
        calls, lock = {}, Lock()
        roots_started = Barrier(3) if concurrency == 3 else None

        def recognize(part):
            span = (part.start, part.duration)
            with lock:
                calls[span] = calls.get(span, 0) + 1
                attempt = calls[span]
            if roots_started is not None and span in {(0, 20), (20, 20), (40, 7)} and attempt == 1:
                roots_started.wait(timeout=5)
            if span == (0, 20) or (span == (20, 20) and attempt == 1):
                raise OmniServiceError("invalid model JSON", code="invalid_json", raw_response="bad-original-response")
            # Split recovery reads the actual PCM child files, including their
            # complete ending, rather than pretending a truncated result passed.
            with wave.open(str(part.path)) as audio:
                assert audio.getnframes() == part.duration * audio.getframerate()
                assert audio.readframes(audio.getnframes())
            text = f"原话 {part.start:g}–{part.start + part.duration:g}"
            return AudioAnalysis.model_validate({
                "transcript_segments": [{"start_seconds": 0, "end_seconds": part.duration, "text": text}],
                "warnings": [f"片段 {part.start:g}/{part.duration:g} 的不确定性"],
            }), f"raw-{part.start:g}-{part.duration:g}"

        monkeypatch.setattr(service, "_analyze_audio_segment", recognize)
        result = service.analyze_audio(source)
        assert result.payload["audio_processing"]["recognition_attempts"] == 7
        assert result.payload["audio_processing"]["failed_recognition_attempts"] == 3
        assert [(item["start_seconds"], item["end_seconds"]) for item in result.payload["transcript_segments"]] == [
            (0, 10), (10, 20), (20, 40), (40, 47),
        ]
        outputs.append(result)

    serial, parallel = outputs
    assert serial.transcript_text == parallel.transcript_text
    assert serial.payload["transcript_segments"] == parallel.payload["transcript_segments"]
    assert serial.payload["raw_results"] == parallel.payload["raw_results"]
    assert serial.payload["warnings"] == parallel.payload["warnings"]


def test_concurrency_configuration_is_small_and_validated(monkeypatch):
    monkeypatch.delenv("AUDIO_TRANSCRIPTION_CONCURRENCY", raising=False)
    assert Settings(_env_file=None).audio_transcription_concurrency == 3
    for count in (1, 2, 3, 4):
        assert Settings(_env_file=None, audio_transcription_concurrency=count).audio_transcription_concurrency == count
    for count in (0, 5):
        with pytest.raises(ValidationError):
            Settings(_env_file=None, audio_transcription_concurrency=count)
