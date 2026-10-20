"""Resume validated child spans without repeating already paid recognition."""
import json
from contextlib import contextmanager

import pytest

from app.core.config import settings
from app.services import omni_service as omni_module
from app.services.omni_service import OmniService, OmniServiceError
from tests.test_audio_recovery import recording, transcript


@pytest.fixture
def source_context(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "audio_boundary_review_enabled", False)
    root, _ = recording(tmp_path, duration=20)
    monkeypatch.setattr(settings, "qwen_omni_api_key", "synthetic-unit-key")
    monkeypatch.setattr(settings, "qwen_omni_model", "synthetic-audio-model")
    monkeypatch.setattr(settings, "video_segment_seconds", 20)
    monkeypatch.setattr(settings, "audio_transcription_concurrency", 1)

    @contextmanager
    def prepared(path):
        assert path == root.path
        yield root.duration, iter([root])

    monkeypatch.setattr(omni_module, "prepare_audio_segments", prepared)
    return root


def first_failed_run(root, monkeypatch):
    service, calls = OmniService(), []

    def recognize(part):
        calls.append((part.start, part.duration))
        if part.duration == 10 and part.start == 0:
            return transcript("已验证的左半原话。", part.duration), "saved-left-raw"
        raise OmniServiceError("synthetic incomplete result", code="invalid_json", raw_response="synthetic-bad-raw")

    monkeypatch.setattr(service, "_analyze_audio_segment", recognize)
    with pytest.raises(OmniServiceError):
        service.analyze_audio(root.path)
    assert calls == [(0, 20), (0, 20), (0, 10), (10, 10), (10, 10)]
    return root.path.with_name(root.path.name + ".analysis.json")


def resumed_run(root, monkeypatch):
    service, calls = OmniService(), []

    def recognize(part):
        calls.append((part.start, part.duration))
        if part.duration == root.duration:
            raise OmniServiceError("synthetic malformed root", code="invalid_json", raw_response="bad-root")
        text = "重新识别的左半原话。" if part.start == 0 else "右半末句完整保留。"
        return transcript(text, part.duration), f"new-child-raw-{part.start}"

    monkeypatch.setattr(service, "_analyze_audio_segment", recognize)
    return service.analyze_audio(root.path), calls


def test_main_audio_resume_reuses_successful_child_after_other_half_failed(source_context, monkeypatch):
    root = source_context
    first_failed_run(root, monkeypatch)
    result, calls = resumed_run(root, monkeypatch)
    assert calls == [(0, 20), (0, 20), (10, 10)], "Validated left child must not be recognized twice"
    assert result.transcript_text == "已验证的左半原话。\n右半末句完整保留。"
    assert [(item["start_seconds"], item["end_seconds"]) for item in result.payload["transcript_segments"]] == [(0, 10), (10, 20)]
    processing = result.payload["audio_processing"]
    assert processing["reused_child_segments"] == 1
    assert processing["reused_root_segments"] == 0 and processing["new_root_segments"] == 1
    assert processing["recognition_attempts"] == 3 and processing["failed_recognition_attempts"] == 2
    recovered = json.loads(result.payload["raw_results"][0]["audio_response"])["recovered_audio_segments"]
    assert [item["audio_response"] for item in recovered] == ["saved-left-raw", "new-child-raw-10.0"]

    completed = OmniService()
    monkeypatch.setattr(completed, "_analyze_audio_segment", lambda part: pytest.fail("The complete root must be reused on restart"))
    reused = completed.analyze_audio(root.path)
    assert reused.transcript_text == result.transcript_text
    assert reused.payload["audio_processing"]["recognition_attempts"] == 0
    assert reused.payload["audio_processing"]["reused_root_segments"] == 1
    assert reused.payload["audio_processing"]["reused_child_segments"] == 0


@pytest.mark.parametrize("tamper", ["time", "text", "raw"])
def test_invalid_saved_child_is_not_accepted_as_success(source_context, monkeypatch, tamper):
    root = source_context
    sidecar = first_failed_run(root, monkeypatch)
    state = json.loads(sidecar.read_text(encoding="utf-8"))
    child = next(item for item in state["segments"].values()
                 if item["start_seconds"] == 0 and item["duration_seconds"] == 10)
    if tamper == "time":
        child["success"]["analysis"]["transcript_segments"][0]["end_seconds"] = 99
    elif tamper == "text":
        child["success"]["analysis"]["transcript_segments"][0]["text"] = ""
    else:
        child["success"]["raw_response"] = ""
    sidecar.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    result, calls = resumed_run(root, monkeypatch)
    assert calls == [(0, 20), (0, 20), (0, 10), (10, 10)]
    assert result.transcript_text == "重新识别的左半原话。\n右半末句完整保留。"
    assert result.payload["audio_processing"]["reused_child_segments"] == 0
    assert result.payload["audio_processing"]["recognition_attempts"] == 4


@pytest.mark.parametrize("changed", ["source", "model"])
def test_source_or_model_change_invalidates_successful_child(source_context, monkeypatch, changed):
    root = source_context
    first_failed_run(root, monkeypatch)
    if changed == "model":
        monkeypatch.setattr(settings, "qwen_omni_model", "different-synthetic-model")
    else:
        data = bytearray(root.path.read_bytes())
        data[-1] ^= 1
        root.path.write_bytes(data)
    result, calls = resumed_run(root, monkeypatch)
    assert len(calls) == 4 and (0, 10) in calls
    assert result.payload["audio_processing"]["reused_child_segments"] == 0
