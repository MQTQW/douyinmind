"""No-network coverage of durable, content-bound audio root checkpoints."""
import json
from pathlib import Path

import pytest

from app.services import audio_checkpoint as checkpoint_module
from app.services.audio_checkpoint import AudioCheckpoint
from app.services.audio_media import AudioSegment


@pytest.fixture
def source(tmp_path):
    path = tmp_path / "source.wav"
    path.write_bytes(b"the complete source soundtrack")
    return path


@pytest.fixture
def fingerprint():
    return {"model": "test-omni", "endpoint": "https://example.test/v1",
            "prompt": "Keep the full transcript.", "segment_seconds": 60}


def segment(source, start=0, duration=5):
    return AudioSegment(source, start, duration)


def analysis(text="第一句。最后一句完整保留。"):
    return {"transcript_segments": [{"start_seconds": 0, "end_seconds": 5, "text": text}],
            "warnings": []}


def test_success_survives_reload_and_get_returns_independent_values(source, fingerprint):
    saved = analysis()
    checkpoint = AudioCheckpoint(source, fingerprint)
    checkpoint.record_success(segment(source), saved, "完整的模型原始响应")
    saved["transcript_segments"][0]["text"] = "caller mutation"
    loaded = AudioCheckpoint(source, dict(reversed(list(fingerprint.items()))))
    result = loaded.get(segment(source, start=0.0, duration=5.0))
    assert result == {"analysis": analysis(), "raw_response": "完整的模型原始响应"}
    result["analysis"]["transcript_segments"].clear()
    assert loaded.get(segment(source))["analysis"] == analysis()
    assert loaded.get(segment(source, start=5)) is None
    assert loaded.get(segment(source, duration=6)) is None
    assert checkpoint.path == source.with_name("source.wav.analysis.json")


def test_failures_are_durable_and_do_not_become_success(source, fingerprint):
    checkpoint = AudioCheckpoint(source, fingerprint)
    checkpoint.record_failure(segment(source), "json_invalid", '{"unfinished')
    checkpoint.record_failure(segment(source), "api_error", None)
    assert AudioCheckpoint(source, fingerprint).get(segment(source)) is None
    data = json.loads(checkpoint.path.read_text(encoding="utf-8"))
    stored = next(iter(data["segments"].values()))
    assert stored["start_seconds"] == 0
    assert stored["duration_seconds"] == 5
    assert stored["attempts"] == [
        {"outcome": "failure", "error_code": "json_invalid", "raw_response": '{"unfinished'},
        {"outcome": "failure", "error_code": "api_error", "raw_response": None},
    ]


def test_failure_appends_without_overwriting_previously_validated_success(source, fingerprint):
    checkpoint = AudioCheckpoint(source, fingerprint)
    checkpoint.record_success(segment(source), analysis(), "good response")
    checkpoint.record_failure(segment(source), "time_out_of_range", "bad response")
    loaded = AudioCheckpoint(source, fingerprint)
    assert loaded.get(segment(source)) == {"analysis": analysis(), "raw_response": "good response"}
    stored = next(iter(json.loads(checkpoint.path.read_text(encoding="utf-8"))["segments"].values()))
    assert [attempt["outcome"] for attempt in stored["attempts"]] == ["success", "failure"]


def test_source_content_change_invalidates_even_with_same_file_size(source, fingerprint):
    checkpoint = AudioCheckpoint(source, fingerprint)
    checkpoint.record_success(segment(source), analysis(), "old result")
    source.write_bytes(b"X" * source.stat().st_size)
    assert AudioCheckpoint(source, fingerprint).get(segment(source)) is None


@pytest.mark.parametrize("field,value", [
    ("model", "another-model"), ("endpoint", "https://another.test/v1"),
    ("prompt", "A different prompt."), ("segment_seconds", 30),
])
def test_configuration_change_invalidates_checkpoint(source, fingerprint, field, value):
    checkpoint = AudioCheckpoint(source, fingerprint)
    checkpoint.record_success(segment(source), analysis(), "old result")
    assert AudioCheckpoint(source, fingerprint | {field: value}).get(segment(source)) is None


@pytest.mark.parametrize("damaged", ["{broken JSON", "null", "[]", '{"schema_version":1}',
                                      b"\xff\xfe"])
def test_damaged_or_incompatible_sidecar_is_a_cache_miss(source, fingerprint, damaged):
    checkpoint = AudioCheckpoint(source, fingerprint)
    checkpoint.path.write_bytes(damaged if isinstance(damaged, bytes) else damaged.encode("utf-8"))
    loaded = AudioCheckpoint(source, fingerprint)
    assert loaded.get(segment(source)) is None
    loaded.record_success(segment(source), analysis(), "replacement")
    assert AudioCheckpoint(source, fingerprint).get(segment(source))["raw_response"] == "replacement"


def test_malformed_segment_is_rejected_without_trusting_its_success(source, fingerprint):
    checkpoint = AudioCheckpoint(source, fingerprint)
    checkpoint.record_success(segment(source), analysis(), "good response")
    data = json.loads(checkpoint.path.read_text(encoding="utf-8"))
    stored = next(iter(data["segments"].values()))
    stored["duration_seconds"] = 50
    checkpoint.path.write_text(json.dumps(data), encoding="utf-8")
    assert AudioCheckpoint(source, fingerprint).get(segment(source)) is None


def test_configuration_values_are_not_stored_in_diagnostics(source, fingerprint):
    checkpoint = AudioCheckpoint(source, fingerprint | {"accidentally_supplied_secret": "secret-value"})
    checkpoint.record_failure(segment(source), "json_invalid", "response text only")
    saved = checkpoint.path.read_text(encoding="utf-8")
    assert "response text only" in saved
    assert "secret-value" not in saved
    assert "accidentally_supplied_secret" not in saved
    assert "https://example.test/v1" not in saved


def test_atomic_replace_leaves_valid_old_file_and_no_temp_files_on_error(source, fingerprint, monkeypatch):
    checkpoint = AudioCheckpoint(source, fingerprint)
    checkpoint.record_success(segment(source), analysis(), "first result")
    old_bytes = checkpoint.path.read_bytes()
    calls = []

    def failed_replace(temporary, destination):
        temporary, destination = Path(temporary), Path(destination)
        calls.append((temporary, destination))
        assert temporary.parent == destination.parent
        assert destination.read_bytes() == old_bytes
        pending = json.loads(temporary.read_text(encoding="utf-8"))
        stored = next(iter(pending["segments"].values()))
        assert len(stored["attempts"]) == 2
        raise OSError("simulated replace failure")

    monkeypatch.setattr(checkpoint_module.os, "replace", failed_replace)
    with pytest.raises(OSError, match="simulated replace failure"):
        checkpoint.record_failure(segment(source), "json_invalid", "new failed attempt")
    assert len(calls) == 1
    assert checkpoint.path.read_bytes() == old_bytes
    assert checkpoint.get(segment(source))["raw_response"] == "first result"
    assert list(source.parent.glob("*.tmp")) == []
    assert not calls[0][0].exists()


def test_non_json_success_cannot_corrupt_an_existing_checkpoint(source, fingerprint):
    checkpoint = AudioCheckpoint(source, fingerprint)
    checkpoint.record_success(segment(source), analysis(), "valid")
    old_bytes = checkpoint.path.read_bytes()
    with pytest.raises(ValueError):
        checkpoint.record_success(segment(source), {"invalid": float("nan")}, "invalid")
    assert checkpoint.path.read_bytes() == old_bytes
    assert checkpoint.get(segment(source))["raw_response"] == "valid"
    assert list(source.parent.glob("*.tmp")) == []


@pytest.mark.parametrize("start,duration", [(-1, 5), (0, 0), (0, -1), (float("inf"), 5), (0, float("nan"))])
def test_invalid_span_is_never_recorded(source, fingerprint, start, duration):
    checkpoint = AudioCheckpoint(source, fingerprint)
    with pytest.raises(ValueError):
        checkpoint.record_failure(segment(source, start, duration), "invalid", "response")
    assert not checkpoint.path.exists()
