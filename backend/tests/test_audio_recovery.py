"""Bounded audio recovery must keep complete PCM input and complete transcripts."""
import json
import struct
import wave
from contextlib import contextmanager
from types import SimpleNamespace

import httpx
import pytest
from openai import APIConnectionError

from app.core.config import settings
from app.services import omni_service as omni_module
from app.services.audio_media import AudioSegment
from app.services.omni_service import AudioAnalysis, OmniService, OmniServiceError
from tests.test_omni_ingestion import FakeClient


@pytest.fixture(autouse=True)
def isolate_root_recovery_policy(monkeypatch):
    # The independent boundary tests exercise its enabled-by-default path.
    monkeypatch.setattr(settings, "audio_boundary_review_enabled", False)


def recording(tmp_path, *, duration=20, start=0, extra_frames=0, rate=8000):
    """Write a small real PCM WAV, with marked samples to detect overlap or loss."""
    frames = int(duration * rate) + extra_frames
    payload = b"".join(struct.pack("<h", index % 30000) for index in range(frames))
    target = tmp_path / "source.wav"
    with wave.open(str(target), "wb") as output:
        output.setparams((1, 2, rate, 0, "NONE", "not compressed"))
        output.writeframes(payload)
    return AudioSegment(target, start, frames / rate), payload


def transcript(text, duration):
    return AudioAnalysis.model_validate({
        "transcript_segments": [{"start_seconds": 0, "end_seconds": duration, "text": text}],
        "warnings": [],
    })


def observe(attempts):
    def callback(segment, analysis, raw_response, error_code):
        attempts.append((segment.start, segment.duration, analysis, raw_response, error_code))
    return callback


def test_service_error_defaults_and_retains_original_response():
    error = OmniServiceError("invalid result", raw_response="original text")
    assert str(error) == "invalid result"
    assert error.code == "service_error"
    assert error.raw_response == "original text"


@pytest.mark.parametrize("code", ["invalid_json", "schema_invalid", "time_out_of_range", "output_incomplete"])
def test_recoverable_failure_retries_same_complete_audio_and_retains_attempts(tmp_path, monkeypatch, code):
    root, payload = recording(tmp_path)
    service, calls, attempts = OmniService(), [], []

    def recognize(segment):
        calls.append(segment)
        assert segment.path == root.path
        with wave.open(str(segment.path)) as source:
            assert source.readframes(source.getnframes()) == payload
        if len(calls) == 1:
            raise OmniServiceError("bad model result", code=code, raw_response="bad original")
        return transcript("完整末句。", segment.duration), "good original"

    monkeypatch.setattr(service, "_analyze_audio_segment", recognize)
    result, raw = service._analyze_audio_resilient(root, on_attempt=observe(attempts))
    assert len(calls) == 2
    assert result.transcript_segments[-1].text == "完整末句。"
    assert result.transcript_segments[-1].end_seconds == root.duration
    assert raw == "good original"
    assert [(item[3], item[4]) for item in attempts] == [("bad original", code), ("good original", None)]
    assert attempts[0][2] is None
    assert attempts[1][2] is not None


@pytest.mark.parametrize("code", ["api_error", "permission_denied", "service_error"])
def test_nonrecoverable_error_stops_after_one_call(tmp_path, monkeypatch, code):
    root, _ = recording(tmp_path, duration=80)
    service, calls, attempts = OmniService(), [], []

    def recognize(segment):
        calls.append(segment)
        raise OmniServiceError("permission or API failure", code=code, raw_response="provider failure")

    monkeypatch.setattr(service, "_analyze_audio_segment", recognize)
    with pytest.raises(OmniServiceError) as caught:
        service._analyze_audio_resilient(root, on_attempt=observe(attempts))
    assert caught.value.code == code
    assert len(calls) == 1
    assert attempts[0][3:] == ("provider failure", code)


def test_failed_root_splits_exact_pcm_samples_and_merges_relative_times_without_losing_tail(tmp_path, monkeypatch):
    root, payload = recording(tmp_path, duration=21, start=120, extra_frames=1)
    service, calls, successful_pcm, attempts = OmniService(), [], [], []

    def recognize(segment):
        calls.append(segment)
        if segment.path == root.path:
            raise OmniServiceError("malformed root", code="invalid_json", raw_response=f"root-bad-{len(calls)}")
        with wave.open(str(segment.path)) as source:
            assert source.getframerate() == 8000
            assert source.getnchannels() == 1
            assert source.getsampwidth() == 2
            successful_pcm.append((source.getnframes(), source.readframes(source.getnframes())))
        position = "first" if segment.start == root.start else "last"
        return transcript("前半完整原话。" if position == "first" else "最后一句不能丢。", segment.duration), f"raw-{position}"

    monkeypatch.setattr(service, "_analyze_audio_segment", recognize)
    result, raw = service._analyze_audio_resilient(root, on_attempt=observe(attempts))
    assert len(calls) == len(attempts) == 4
    left_frames, right_frames = successful_pcm[0][0], successful_pcm[1][0]
    assert right_frames == left_frames + 1
    assert b"".join(item[1] for item in successful_pcm) == payload
    assert calls[2].start == root.start
    assert calls[3].start == pytest.approx(root.start + left_frames / 8000)
    first, last = result.transcript_segments
    assert first.start_seconds == 0
    assert first.end_seconds == pytest.approx(left_frames / 8000)
    assert last.start_seconds == pytest.approx(left_frames / 8000)
    assert last.end_seconds == pytest.approx(root.duration)
    assert last.text == "最后一句不能丢。"
    assert "raw-first" in raw and "raw-last" in raw
    assert [item[3] for item in attempts] == ["root-bad-1", "root-bad-2", "raw-first", "raw-last"]
    assert root.path.exists()


def test_failed_half_does_not_return_successful_half_as_complete(tmp_path, monkeypatch):
    root, _ = recording(tmp_path, duration=20)
    service, calls, attempts = OmniService(), [], []

    def recognize(segment):
        calls.append(segment)
        if segment.path != root.path and segment.start == root.start:
            return transcript("只有前半成功。", segment.duration), "first-half"
        raise OmniServiceError("failed remainder", code="invalid_json", raw_response="broken remainder")

    monkeypatch.setattr(service, "_analyze_audio_segment", recognize)
    with pytest.raises(OmniServiceError):
        service._analyze_audio_resilient(root, on_attempt=observe(attempts))
    assert len(calls) == len(attempts) == 5
    assert attempts[2][2] is not None
    assert attempts[-1][2] is None
    assert attempts[-1][3] == "broken remainder"


def test_two_levels_of_splitting_can_succeed_at_the_shared_ten_call_budget(tmp_path, monkeypatch):
    root, payload = recording(tmp_path, duration=80)
    service, calls, leaves, attempts = OmniService(), [], [], []

    def recognize(segment):
        calls.append(segment)
        if segment.duration > 20:
            raise OmniServiceError("bad parent", code="schema_invalid", raw_response="bad parent response")
        with wave.open(str(segment.path)) as source:
            leaves.append(source.readframes(source.getnframes()))
        return transcript(f"原话 {segment.start}", segment.duration), f"leaf-{segment.start}"

    monkeypatch.setattr(service, "_analyze_audio_segment", recognize)
    result, _ = service._analyze_audio_resilient(root, on_attempt=observe(attempts))
    assert len(calls) == len(attempts) == 10
    assert len(result.transcript_segments) == 4
    assert [(item.start_seconds, item.end_seconds) for item in result.transcript_segments] == [(0, 20), (20, 40), (40, 60), (60, 80)]
    assert b"".join(leaves) == payload


def test_call_budget_is_shared_across_all_halves_and_fails_whole_result(tmp_path, monkeypatch):
    root, _ = recording(tmp_path, duration=80)
    service, calls, per_span, attempts = OmniService(), [], {}, []

    def recognize(segment):
        calls.append(segment)
        span = (segment.start, segment.duration)
        per_span[span] = per_span.get(span, 0) + 1
        if segment.duration <= 20 and per_span[span] == 2:
            return transcript("重试后原话", segment.duration), "good leaf"
        raise OmniServiceError("needs retry", code="invalid_json", raw_response="bad attempt")

    monkeypatch.setattr(service, "_analyze_audio_segment", recognize)
    with pytest.raises(OmniServiceError):
        service._analyze_audio_resilient(root, on_attempt=observe(attempts))
    assert len(calls) == len(attempts) == 10
    assert len([item for item in attempts if item[2] is not None]) == 2


def test_recursion_never_exceeds_two_split_levels(tmp_path, monkeypatch):
    root, _ = recording(tmp_path, duration=80)
    service, calls = OmniService(), []

    def recognize(segment):
        calls.append(segment)
        raise OmniServiceError("bad output", code="invalid_json", raw_response="raw")

    monkeypatch.setattr(service, "_analyze_audio_segment", recognize)
    with pytest.raises(OmniServiceError):
        service._analyze_audio_resilient(root)
    assert [item.duration for item in calls] == [80, 80, 40, 40, 20, 20]


def test_ten_second_segment_is_retried_but_never_split(tmp_path, monkeypatch):
    root, _ = recording(tmp_path, duration=10)
    service, calls = OmniService(), []

    def recognize(segment):
        calls.append(segment)
        raise OmniServiceError("bad output", code="invalid_json", raw_response="raw")

    monkeypatch.setattr(service, "_analyze_audio_segment", recognize)
    with pytest.raises(OmniServiceError):
        service._analyze_audio_resilient(root)
    assert calls == [root, root]


@pytest.mark.parametrize("raw,finish,code", [
    ("{broken", "stop", "invalid_json"),
    (json.dumps({"warnings": []}), "stop", "schema_invalid"),
    (json.dumps({"transcript_segments": [{"start_seconds": 0, "end_seconds": 50, "text": "明显越界，不能剪裁"}]}), "stop", "time_out_of_range"),
    ('{"transcript_segments": [', "length", "output_incomplete"),
])
def test_strict_segment_validation_classifies_failure_and_preserves_raw(tmp_path, monkeypatch, raw, finish, code):
    root, _ = recording(tmp_path, duration=5)
    monkeypatch.setattr(settings, "qwen_omni_api_key", "unit-test-key")
    service = OmniService()
    service._client = FakeClient([raw], finish=finish)
    with pytest.raises(OmniServiceError) as caught:
        service._analyze_audio_segment(root)
    assert caught.value.code == code
    assert caught.value.raw_response == raw
    assert len(service._client.calls) == 1
    assert service._client.streams[0].closed


def test_main_audio_entry_resumes_validated_roots_after_later_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "audio_transcription_concurrency", 1)
    source, _ = recording(tmp_path, duration=15)
    parts = []
    for name, start, duration in (("first", 0, 5), ("last", 5, 10)):
        folder = tmp_path / name
        folder.mkdir()
        part, _ = recording(folder, duration=duration, start=start)
        parts.append(part)
    monkeypatch.setattr(settings, "qwen_omni_api_key", "unit-test-key")
    monkeypatch.setattr(settings, "qwen_omni_model", "unit-test-audio-model")
    monkeypatch.setattr(settings, "video_segment_seconds", 10)

    @contextmanager
    def prepared(path):
        assert path == source.path
        yield 15, iter(parts)

    monkeypatch.setattr(omni_module, "prepare_audio_segments", prepared)
    first_service, first_calls = OmniService(), []

    def first_recognition(segment):
        first_calls.append(segment)
        if segment.start == 0:
            return transcript("第一段原话已完整保存。", segment.duration), "first-original-raw"
        raise OmniServiceError("bad second segment", code="invalid_json", raw_response=f"bad-second-{len(first_calls)}")

    monkeypatch.setattr(first_service, "_analyze_audio_segment", first_recognition)
    with pytest.raises(OmniServiceError):
        first_service.analyze_audio(source.path)
    assert [part.start for part in first_calls] == [0, 5, 5]
    sidecar = source.path.with_name(source.path.name + ".analysis.json")
    assert sidecar.exists()
    saved = sidecar.read_text(encoding="utf-8")
    assert "first-original-raw" in saved
    assert "bad-second-2" in saved and "bad-second-3" in saved

    resumed_service, resumed_calls = OmniService(), []

    def resumed_recognition(segment):
        resumed_calls.append(segment)
        assert segment.start == 5, "The validated first segment must not be sent again."
        return transcript("第二段的最后一句也完整保留。", segment.duration), "last-original-raw"

    monkeypatch.setattr(resumed_service, "_analyze_audio_segment", resumed_recognition)
    result = resumed_service.analyze_audio(source.path)
    assert len(resumed_calls) == 1
    assert result.transcript_text == "第一段原话已完整保存。\n第二段的最后一句也完整保留。"
    assert [(item["start_seconds"], item["end_seconds"]) for item in result.payload["transcript_segments"]] == [(0, 5), (5, 15)]
    assert [item["audio_response"] for item in result.payload["raw_results"]] == ["first-original-raw", "last-original-raw"]
    assert result.payload["duration_seconds"] == 15
    assert result.payload["audio_processing"]["reused_root_segments"] == 1
    assert result.payload["audio_processing"]["new_root_segments"] == 1
    assert result.payload["audio_processing"]["recognition_attempts"] == 1


def test_main_audio_entry_revalidates_tampered_checkpoint_timestamps(tmp_path, monkeypatch):
    root, _ = recording(tmp_path, duration=5)
    monkeypatch.setattr(settings, "qwen_omni_api_key", "unit-test-key")
    monkeypatch.setattr(settings, "qwen_omni_model", "unit-test-audio-model")
    monkeypatch.setattr(settings, "video_segment_seconds", 5)

    @contextmanager
    def prepared(path):
        yield root.duration, iter([root])

    monkeypatch.setattr(omni_module, "prepare_audio_segments", prepared)
    initial = OmniService()
    monkeypatch.setattr(initial, "_analyze_audio_segment", lambda part: (transcript("原先正确的文字。", part.duration), "original-valid-raw"))
    initial.analyze_audio(root.path)
    sidecar = root.path.with_name(root.path.name + ".analysis.json")
    saved = json.loads(sidecar.read_text(encoding="utf-8"))
    segment_entry = next(iter(saved["segments"].values()))
    segment_entry["success"]["analysis"]["transcript_segments"][0]["end_seconds"] = 50
    sidecar.write_text(json.dumps(saved, ensure_ascii=False), encoding="utf-8")

    resumed, calls = OmniService(), []

    def recognize(part):
        calls.append(part)
        return transcript("重新识别并验证的完整文字。", part.duration), "replacement-valid-raw"

    monkeypatch.setattr(resumed, "_analyze_audio_segment", recognize)
    result = resumed.analyze_audio(root.path)
    assert len(calls) == 1
    assert result.transcript_text == "重新识别并验证的完整文字。"
    assert result.payload["transcript_segments"][0]["end_seconds"] == root.duration
    assert result.payload["raw_results"][0]["audio_response"] == "replacement-valid-raw"


def test_split_recovery_replaces_invalid_cached_root_for_next_restart(tmp_path, monkeypatch):
    root, _ = recording(tmp_path, duration=20)
    monkeypatch.setattr(settings, "qwen_omni_api_key", "unit-test-key")
    monkeypatch.setattr(settings, "qwen_omni_model", "unit-test-audio-model")
    monkeypatch.setattr(settings, "video_segment_seconds", 20)

    @contextmanager
    def prepared(path):
        yield root.duration, iter([root])

    monkeypatch.setattr(omni_module, "prepare_audio_segments", prepared)
    initial = OmniService()
    monkeypatch.setattr(initial, "_analyze_audio_segment", lambda part: (transcript("原先正确的整段文字。", part.duration), "initial-root-raw"))
    initial.analyze_audio(root.path)
    sidecar = root.path.with_name(root.path.name + ".analysis.json")
    saved = json.loads(sidecar.read_text(encoding="utf-8"))
    segment_entry = next(iter(saved["segments"].values()))
    segment_entry["success"]["analysis"]["transcript_segments"][0]["end_seconds"] = 50
    sidecar.write_text(json.dumps(saved, ensure_ascii=False), encoding="utf-8")

    recovering, recovery_calls = OmniService(), []

    def recover_recognition(part):
        recovery_calls.append(part)
        if part.path == root.path:
            raise OmniServiceError("root returned malformed JSON", code="invalid_json", raw_response="malformed-root-raw")
        text = "修复后的前半原话。" if part.start == 0 else "修复后的完整末句。"
        return transcript(text, part.duration), f"repaired-child-{part.start}"

    monkeypatch.setattr(recovering, "_analyze_audio_segment", recover_recognition)
    repaired = recovering.analyze_audio(root.path)
    assert len(recovery_calls) == 4
    assert repaired.transcript_text == "修复后的前半原话。\n修复后的完整末句。"
    assert [(item["start_seconds"], item["end_seconds"]) for item in repaired.payload["transcript_segments"]] == [(0, 10), (10, 20)]

    restarted, unexpected_calls = OmniService(), []

    def unexpected_recognition(part):
        unexpected_calls.append(part)
        raise AssertionError("The repaired complete root should be loaded from checkpoint.")

    monkeypatch.setattr(restarted, "_analyze_audio_segment", unexpected_recognition)
    reused = restarted.analyze_audio(root.path)
    assert unexpected_calls == []
    assert reused.transcript_text == repaired.transcript_text
    assert reused.payload["transcript_segments"] == repaired.payload["transcript_segments"]
    assert reused.payload["raw_results"] == repaired.payload["raw_results"]
    retained_raw = reused.payload["raw_results"][0]["audio_response"]
    assert "repaired-child-0" in retained_raw and "repaired-child-10" in retained_raw


def test_interrupted_provider_stream_retains_partial_raw_and_closes_stream(monkeypatch):
    monkeypatch.setattr(settings, "qwen_omni_api_key", "unit-test-key")

    class InterruptedStream:
        closed = False

        def __iter__(self):
            yield SimpleNamespace(choices=[SimpleNamespace(
                delta=SimpleNamespace(content="partial transcript"), finish_reason=None,
            )])
            raise APIConnectionError(request=httpx.Request("POST", "https://example.test"))

        def close(self):
            self.closed = True

    stream = InterruptedStream()
    service = OmniService()
    service._client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=lambda **kwargs: stream)))
    with pytest.raises(OmniServiceError) as caught:
        service._request([{"type": "text", "text": "unit test"}])
    assert caught.value.code == "api_error"
    assert caught.value.raw_response == "partial transcript"
    assert stream.closed


@pytest.mark.parametrize("prefix,valid", [("```", True), ("```json", True), ("```", False)])
def test_single_line_fenced_audio_response_parses_or_classifies_invalid_json(tmp_path, monkeypatch, prefix, valid):
    root, _ = recording(tmp_path, duration=5)
    monkeypatch.setattr(settings, "qwen_omni_api_key", "unit-test-key")
    payload = json.dumps({"transcript_segments": [{"start_seconds": 0, "end_seconds": 5, "text": "完整原话。"}]}) if valid else "{broken"
    raw = prefix + payload + "```"
    service = OmniService()
    service._client = FakeClient([raw])
    if valid:
        result, retained_raw = service._analyze_audio_segment(root)
        assert result.transcript_segments[0].text == "完整原话。"
        assert retained_raw == raw
    else:
        with pytest.raises(OmniServiceError) as caught:
            service._analyze_audio_segment(root)
        assert caught.value.code == "invalid_json"
        assert caught.value.raw_response == raw
    assert service._client.streams[0].closed
