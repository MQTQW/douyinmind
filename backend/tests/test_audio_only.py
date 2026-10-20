import json
import wave
from contextlib import contextmanager
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.core.config import settings
from app.db.base import Base
from app.models.entities import VideoCache
from app.services import audio_media, knowledge_service as knowledge_module, omni_service as omni_module
from app.services.audio_media import AudioSegment, prepare_audio_segments
from app.services.media_service import MediaPipelineError, _resolve_ffmpeg_path
from app.services.omni_service import OmniService, OmniServiceError, VideoAnalysis
from tests.test_omni_ingestion import FakeClient


def audio_payload(text_value, end=1):
    return {"transcript_segments": [{"start_seconds": 0, "end_seconds": end, "text": text_value}], "warnings": []}


def test_audio_only_makes_one_audio_request_per_segment_and_retains_the_tail(tmp_path, monkeypatch):
    path = tmp_path / "source.wav"
    path.write_bytes(b"unit audio")
    monkeypatch.setattr(settings, "qwen_omni_api_key", "unit-test-key")
    monkeypatch.setattr(settings, "video_segment_seconds", 5)
    # This response-list fake models the supported sequential mode. Concurrent
    # response ordering is exercised separately with segment-bound fakes.
    monkeypatch.setattr(settings, "audio_transcription_concurrency", 1)
    @contextmanager
    def prepared(source):
        yield 7, iter([AudioSegment(path, 0, 5), AudioSegment(path, 5, 2)])
    monkeypatch.setattr(omni_module, "prepare_audio_segments", prepared)
    service = OmniService()
    service._client = FakeClient([json.dumps(audio_payload("第一段完整原话")), json.dumps(audio_payload("最后一句原话"))])
    result = service.analyze_audio(path)
    assert result.transcript_text == "第一段完整原话\n最后一句原话"
    assert result.payload["analysis_mode"] == "audio"
    assert result.visual_analysis_text == ""
    assert result.payload["visual_segments"] == []
    assert result.payload["transcript_segments"][-1]["start_seconds"] == 5
    assert len(service._client.calls) == 2
    assert all([part["type"] for part in call["messages"][0]["content"]] == ["input_audio", "text"] for call in service._client.calls)
    assert json.loads(result.payload["raw_results"][-1]["audio_response"])["transcript_segments"][0]["text"] == "最后一句原话"


def test_audio_only_rejects_out_of_range_times(tmp_path, monkeypatch):
    path = tmp_path / "source.wav"
    path.write_bytes(b"unit audio")
    monkeypatch.setattr(settings, "qwen_omni_api_key", "unit-test-key")
    service = OmniService()
    service._client = FakeClient([json.dumps(audio_payload("不能成功保存", end=20))])
    with pytest.raises(OmniServiceError, match="不完整"):
        service._analyze_audio_segment(AudioSegment(path, 0, 5))


@pytest.mark.parametrize("retain_old_visual", [False, True])
def test_audio_release_does_not_download_video_or_call_visual_analysis(tmp_path, monkeypatch, retain_old_visual):
    monkeypatch.setattr(settings, "knowledge_analysis_mode", "audio")
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    source = tmp_path / "audio.wav"
    source.write_bytes(b"unit soundtrack")
    def unexpected(*args, **kwargs):
        raise AssertionError("Audio-only release must not use visual processing")
    monkeypatch.setattr(knowledge_module, "download_video", unexpected)
    monkeypatch.setattr(knowledge_module.omni_service, "analyze_video", unexpected)
    monkeypatch.setattr(knowledge_module, "download_audio", lambda *args: source)
    result = VideoAnalysis("完整原话保留。", "", {
        "analysis_mode": "audio", "model": "qwen3.8-omni-flash", "duration_seconds": 4,
        "transcript_segments": [{"start_seconds": 0, "end_seconds": 4, "text": "完整原话保留。"}],
        "visual_segments": [], "raw_results": [{"audio_response": "完整原始返回"}], "warnings": [],
    })
    monkeypatch.setattr(knowledge_module.omni_service, "analyze_audio", lambda *args: result)
    monkeypatch.setattr(knowledge_module.embedding_client, "embed_texts", lambda chunks: [[.1, .2] for _ in chunks])
    indexed = []
    monkeypatch.setattr(knowledge_module, "get_chroma_service", lambda: SimpleNamespace(upsert_video_chunks=lambda **kwargs: indexed.extend(kwargs["chunks"])))
    with Session(engine, expire_on_commit=False) as db:
        cache = VideoCache(platform_item_id="audio-release", title="音频首版",
            visual_analysis_text="以前保存的画面观察" if retain_old_visual else "",
            analysis_json=json.dumps({"visual_segments": [{"start_seconds": 0, "end_seconds": 6, "description": "旧观察", "visible_text": ""}], "raw_results": ["old-result"]}),
            analysis_model="older-run", analysis_duration_seconds=6)
        db.add(cache)
        db.commit()
        knowledge_module.KnowledgeService()._process_videos(db, [cache.platform_item_id])
        assert cache.status == "done"
        assert cache.transcript_text == "完整原话保留。"
        assert json.loads(cache.analysis_json)["analysis_mode"] == "audio"
        if retain_old_visual:
            assert cache.visual_analysis_text == "以前保存的画面观察"
            assert json.loads(cache.analysis_json)["retained_visual_analysis"] is True
        else:
            assert not cache.visual_analysis_text
            assert not any("画面分析" in chunk for chunk in indexed)
    engine.dispose()


def test_real_audio_segments_cover_last_seconds_without_visual_encoding(tmp_path, monkeypatch):
    try:
        ffmpeg = _resolve_ffmpeg_path()
    except MediaPipelineError:
        pytest.skip("FFmpeg is not installed")
    monkeypatch.setattr(audio_media, "_resolve_ffmpeg_path", lambda: ffmpeg)
    monkeypatch.setattr(settings, "audio_cache_dir", str(tmp_path / "prepared"))
    monkeypatch.setattr(settings, "video_segment_seconds", 5)
    source = tmp_path / "original.wav"
    with wave.open(str(source), "wb") as recording:
        recording.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
        recording.writeframes(b"\x01\x00" * (7 * 16000))
    spans, paths = [], []
    with prepare_audio_segments(source) as (duration, segments):
        assert duration == 7
        for segment in segments:
            spans.append((segment.start, segment.duration))
            paths.append(segment.path)
            with wave.open(str(segment.path)) as recording:
                assert recording.getframerate() == 16000
                assert recording.getnframes() / 16000 == pytest.approx(segment.duration, abs=.05)
    assert spans == [(0, 5), (5, 2)]
    assert all(not path.exists() for path in paths)
    assert source.exists()
