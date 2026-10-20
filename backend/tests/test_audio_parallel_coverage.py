"""Missing or overlapping audio roots must never become a complete transcript."""
from contextlib import contextmanager

import pytest

from app.core.config import settings
from app.services import omni_service as omni_module
from app.services.audio_media import AudioSegment
from app.services.omni_service import AudioAnalysis, OmniService, OmniServiceError


@pytest.mark.parametrize("spans", [
    [],                           # No extracted audio at all.
    [(0, 5), (5, 5)],             # Lost tail.
    [(0, 5), (6, 4), (10, 5)],   # Gap in the middle.
    [(0, 5), (4, 6), (10, 5)],   # Duplicate overlap.
    [(1, 4), (5, 5), (10, 5)],   # Lost opening.
    [(0, 5), (5, 5), (10, 6)],   # Beyond the source duration.
    [(0, 5), (5, 5), (10, 4)],   # Correct root count but short tail.
    [(0, 5), (5, 5), (10, 5), (15, 1)],  # Unexpected extra root.
])
def test_incomplete_root_coverage_is_rejected(tmp_path, monkeypatch, spans):
    source = tmp_path / "source.wav"
    source.write_bytes(b"local fake source")
    monkeypatch.setattr(settings, "qwen_omni_api_key", "offline-test")
    monkeypatch.setattr(settings, "video_segment_seconds", 5)
    monkeypatch.setattr(settings, "audio_transcription_concurrency", 3)

    @contextmanager
    def prepared(path):
        yield 15, iter(AudioSegment(source, start, duration) for start, duration in spans)

    monkeypatch.setattr(omni_module, "prepare_audio_segments", prepared)
    service = OmniService()
    monkeypatch.setattr(service, "_analyze_audio_segment", lambda segment: (
        AudioAnalysis.model_validate({"transcript_segments": [{
            "start_seconds": 0, "end_seconds": segment.duration, "text": "实际片段。",
        }]}), "raw-source",
    ))
    with pytest.raises(OmniServiceError) as caught:
        service.analyze_audio(source)
    assert caught.value.code == "segment_coverage"
