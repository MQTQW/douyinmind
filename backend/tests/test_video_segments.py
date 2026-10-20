"""Verify real media preparation covers the complete source and keeps its audio."""
import subprocess
import wave

import pytest

from app.core.config import settings
from app.services.media_service import MediaPipelineError, _resolve_ffmpeg_path
from app.services.video_media import prepare_video_segments, probe_video


def test_segments_cover_tail_and_preserve_separate_audio(tmp_path, monkeypatch):
    try:
        ffmpeg = _resolve_ffmpeg_path()
    except MediaPipelineError:
        pytest.skip("FFmpeg is not installed")
    monkeypatch.setattr(settings, "video_cache_dir", str(tmp_path / "prepared"))
    monkeypatch.setattr(settings, "video_segment_seconds", 5)
    original = tmp_path / "source.mp4"
    subprocess.run([ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
        "-f", "lavfi", "-i", "color=c=blue:s=64x64:r=4",
        "-f", "lavfi", "-i", "sine=frequency=500:sample_rate=16000",
        "-t", "7", "-c:v", "libx264", "-c:a", "aac", str(original)], check=True, timeout=30)
    periods, paths = [], []
    with prepare_video_segments(original) as (info, segments):
        assert info.has_audio
        for segment in segments:
            periods.append((segment.start, segment.duration))
            paths += [segment.path, segment.audio_path]
            assert segment.path.is_file()
            assert not probe_video(segment.path).has_audio
            with wave.open(str(segment.audio_path)) as recording:
                assert recording.getframerate() == 16000
                assert recording.getnchannels() == 1
                assert recording.getnframes() / 16000 == pytest.approx(segment.duration, abs=.15)
    assert periods == [(0, 5), (5, 2)]
    assert all(not path.exists() for path in paths)
    assert original.exists()
