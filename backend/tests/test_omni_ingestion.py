"""Audio/visual ingestion, preservation, migration, and failure regression tests."""
import json
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, inspect, select, text
from sqlalchemy.orm import Session

from app.core.config import settings
from app.db.base import Base
from app.db.migrations import ensure_video_analysis_columns
from app.models.entities import VideoCache
from app.services import knowledge_service as knowledge_module
from app.services import omni_service as omni_module
from app.services.markdown_export import export_original
from app.services.omni_service import OmniService, OmniServiceError, SegmentAnalysis, VideoAnalysis
from app.services.video_media import VideoInfo, VideoSegment


class FakeStream:
    def __init__(self, result, finish="stop"):
        self.result, self.finish, self.closed = result, finish, False

    def __iter__(self):
        middle = len(self.result) // 2
        for part in (self.result[:middle], self.result[middle:]):
            yield SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content=part), finish_reason=None)])
        yield SimpleNamespace(choices=[])
        yield SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content=None), finish_reason=self.finish)])

    def close(self):
        self.closed = True


class FakeClient:
    def __init__(self, results, finish="stop"):
        self.results, self.calls, self.streams, self.finish = list(results), [], [], finish
        self.chat = SimpleNamespace(completions=self)

    def create(self, **kwargs):
        self.calls.append(kwargs)
        stream = FakeStream(self.results.pop(0), self.finish)
        self.streams.append(stream)
        return stream


@pytest.fixture
def configured(monkeypatch):
    monkeypatch.setattr(settings, "qwen_omni_api_key", "unit-test-key")
    monkeypatch.setattr(settings, "video_segment_seconds", 5)


@pytest.fixture
def segment(tmp_path):
    video, audio = tmp_path / "video.mp4", tmp_path / "audio.wav"
    video.write_bytes(b"video test bytes")
    audio.write_bytes(b"audio test bytes")
    return VideoSegment(video, 0, 5, True, audio)


def speech(text_value="逐字保留最后一句"):
    return {"transcript_segments": [{"start_seconds": 0, "end_seconds": 4, "text": text_value}], "warnings": []}


def visual():
    return {"visual_segments": [{"start_seconds": 0, "end_seconds": 5,
        "description": "画面展示操作步骤", "visible_text": "屏幕文字"}], "warnings": []}


def test_audio_and_visual_are_separately_requested_and_full_text_is_preserved(configured, segment):
    client = FakeClient([json.dumps(speech()), json.dumps(visual())])
    service = OmniService()
    service._client = client
    result, raw = service._analyze_segment(segment)
    assert result.transcript_segments[0].text == "逐字保留最后一句"
    assert result.visual_segments[0].visible_text == "屏幕文字"
    types = [[item["type"] for item in call["messages"][0]["content"]] for call in client.calls]
    assert types == [["input_audio", "text"], ["video_url", "text"]]
    assert all(stream.closed for stream in client.streams)
    assert json.loads(raw)["audio_response"] == json.dumps(speech())


def test_video_without_sound_does_not_invent_or_request_speech(configured, segment):
    service = OmniService()
    service._client = FakeClient([json.dumps(visual())])
    silent = VideoSegment(segment.path, 0, 5, False)
    result, _ = service._analyze_segment(silent)
    assert result.transcript_segments == []
    assert len(service._client.calls) == 1


def test_missing_audio_fails_instead_of_accepting_visual_only(configured, segment):
    service = OmniService()
    service._client = FakeClient([])
    with pytest.raises(OmniServiceError, match="音轨缺失"):
        service._analyze_segment(VideoSegment(segment.path, 0, 5, True))
    assert not service._client.calls


@pytest.mark.parametrize("result", ["{broken JSON", json.dumps(speech() | {"transcript_segments": [{"start_seconds": 0, "end_seconds": 50, "text": "越界"}]})])
def test_incomplete_or_out_of_range_results_are_not_success(configured, segment, result):
    service = OmniService()
    service._client = FakeClient([result, json.dumps(visual())])
    with pytest.raises(OmniServiceError, match="不完整"):
        service._analyze_segment(segment)


def test_output_truncation_is_reported_and_stream_is_closed(configured):
    service = OmniService()
    service._client = FakeClient([json.dumps(speech())], finish="length")
    with pytest.raises(OmniServiceError, match="未完整返回"):
        service._request([{"type": "text", "text": "test"}])
    assert service._client.streams[0].closed


def test_long_video_keeps_last_segment_and_absolute_times(configured, segment, monkeypatch):
    @contextmanager
    def prepared(path):
        yield VideoInfo(7, True), iter([segment, VideoSegment(segment.path, 5, 2, True, segment.audio_path)])
    monkeypatch.setattr(omni_module, "prepare_video_segments", prepared)
    service = OmniService()
    def recognize(part):
        end = min(1, part.duration)
        return SegmentAnalysis.model_validate({
            "transcript_segments": [{"start_seconds": 0, "end_seconds": end, "text": f"原文片段{part.start}"}],
            "visual_segments": [{"start_seconds": 0, "end_seconds": end, "description": f"画面片段{part.start}", "visible_text": ""}],
        }), f"raw-{part.start}"
    monkeypatch.setattr(service, "_analyze_segment", recognize)
    result = service.analyze_video(segment.path)
    assert result.transcript_text == "原文片段0\n原文片段5"
    assert result.payload["transcript_segments"][1]["start_seconds"] == 5
    assert result.payload["visual_segments"][1]["end_seconds"] == 6
    assert result.payload["raw_results"][-1]["response"] == "raw-5"
    chunks = result.knowledge_chunks()
    assert any("语音文字稿" in chunk and "原文片段5" in chunk for chunk in chunks)
    assert any("画面分析" in chunk and "画面片段5" in chunk for chunk in chunks)


def test_database_upgrade_is_idempotent_and_preserves_old_transcript():
    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE video_cache (id INTEGER PRIMARY KEY, transcript_text TEXT)"))
        connection.execute(text("INSERT INTO video_cache VALUES (1, '旧的完整文字稿')"))
    ensure_video_analysis_columns(engine)
    ensure_video_analysis_columns(engine)
    assert "analysis_json" in {column["name"] for column in inspect(engine).get_columns("video_cache")}
    with engine.connect() as connection:
        row = connection.execute(text("SELECT transcript_text, visual_analysis_text, analysis_json FROM video_cache")).one()
        assert tuple(row) == ("旧的完整文字稿", "", "{}")
    engine.dispose()


@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as session:
        yield session
    engine.dispose()


def saved_result():
    return VideoAnalysis("第一句。\n最后一句完整保留。", "[00:00–00:05] 操作画面\n画面文字：保存",
        {"model": "qwen3.8-omni-flash", "duration_seconds": 5,
         "transcript_segments": [{"start_seconds": 0, "end_seconds": 5, "text": "第一句。最后一句完整保留。"}],
         "visual_segments": [{"start_seconds": 0, "end_seconds": 5, "description": "操作画面", "visible_text": "保存"}],
         "warnings": [], "raw_results": [{"response": "完整原始返回"}]})


@pytest.mark.parametrize("fail_indexing", [False, True])
def test_pipeline_saves_both_sources_even_when_indexing_fails(configured, db, tmp_path, monkeypatch, fail_indexing):
    monkeypatch.setattr(settings, "knowledge_analysis_mode", "audio_visual")
    source = tmp_path / "original.mp4"
    source.write_bytes(b"original media")
    cache = VideoCache(platform_item_id="test-video", title="测试视频")
    db.add(cache)
    db.commit()
    indexed = []
    monkeypatch.setattr(knowledge_module, "download_video", lambda *args: source)
    monkeypatch.setattr(knowledge_module.omni_service, "analyze_video", lambda *args: saved_result())
    monkeypatch.setattr(knowledge_module, "get_chroma_service", lambda: SimpleNamespace(upsert_video_chunks=lambda **kwargs: indexed.extend(kwargs["chunks"])))
    def embed(chunks):
        if fail_indexing:
            raise RuntimeError("模拟检索转换失败")
        return [[.1, .2] for _ in chunks]
    monkeypatch.setattr(knowledge_module.embedding_client, "embed_texts", embed)
    service = knowledge_module.KnowledgeService()
    if fail_indexing:
        with pytest.raises(RuntimeError, match="1 个失败"):
            service._process_videos(db, ["test-video"])
        assert source.exists()
        assert cache.status == "failed"
    else:
        service._process_videos(db, ["test-video"])
        assert cache.status == "done"
        assert cache.processed_at
        assert any("画面分析" in chunk for chunk in indexed)
    db.refresh(cache)
    assert cache.transcript_text.endswith("最后一句完整保留。")
    assert cache.visual_analysis_text
    assert json.loads(cache.analysis_json)["raw_results"][0]["response"] == "完整原始返回"


def test_original_export_contains_full_transcript_visuals_and_timestamps():
    result = saved_result()
    cache = VideoCache(platform_item_id="test", title="结果", transcript_text=result.transcript_text,
        visual_analysis_text=result.visual_analysis_text, analysis_json=json.dumps(result.payload), analysis_model="qwen3.8-omni-flash")
    document = export_original(cache)
    assert result.transcript_text in document
    assert result.visual_analysis_text in document
    assert "语音时间记录" in document
    assert "00:00–00:05" in document
    assert "qwen3.8-omni-flash" in document


def test_visual_only_video_is_exportable():
    cache = VideoCache(platform_item_id="silent", title="无声教程", transcript_text="",
        visual_analysis_text="鼠标点击保存按钮", analysis_json="{}", analysis_model="qwen3.8-omni-flash")
    result = export_original(cache)
    assert "没有可识别的语音" in result
    assert "鼠标点击保存按钮" in result
