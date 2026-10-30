"""Knowledge-tool checks use only temporary SQLite and fake retrieval."""
from hashlib import sha256
import sqlite3

import pytest
from sqlalchemy import create_engine, select, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from app.core.config import settings
from app.db.base import Base
from app.mention_agent.knowledge import LocalKnowledgeTools
from app.models.entities import ChatMessage, ChatSession, KnowledgeBase, KnowledgeBaseVideo, VideoCache
from app.services.knowledge_base_service import KnowledgeBaseNotFound
from app.services.retrieval_errors import RetrievalHits


class FakeRag:
    def __init__(self, hits=(), *, mode="hybrid", selected=None):
        self.hits = list(hits)
        self.mode = mode
        self.selected = selected
        self.calls = []

    def _dense_retrieve(self, question, scope):
        self.calls.append((question, set(scope)))
        assert scope is not None
        return RetrievalHits(self.hits, mode=self.mode)

    def _context_hits(self, hits, scope):
        assert scope is not None
        return hits if self.selected is None else self.selected


def hit(video="ready-A", chunk="chunk-1", body="语音文字稿：\n这是当前知识库的原始证据。", **extra):
    return {"platform_item_id": video, "chunk_id": chunk, "title": "当前库视频", "text": body, **extra}


@pytest.fixture
def database(tmp_path):
    path = tmp_path / "independent fixture #1.db"
    engine = create_engine("sqlite://", creator=lambda: sqlite3.connect(path))
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        db.add_all([
            KnowledgeBase(id=1, name="当前测试库", name_key="当前测试库"),
            KnowledgeBase(id=2, name="不能透露的另一个库", name_key="不能透露的另一个库"),
            KnowledgeBase(id=3, name="空库", name_key="空库"),
            VideoCache(platform_item_id="ready-A", title="当前库视频", status="done", transcript_text="当前库正文"),
            VideoCache(platform_item_id="pending-A", title="未完成", status="embedding"),
            VideoCache(platform_item_id="failed-A", title="失败", status="failed"),
            VideoCache(platform_item_id="ready-B", title="别的库秘密", status="done", transcript_text="别的库正文"),
        ])
        db.flush()
        db.add_all([
            KnowledgeBaseVideo(knowledge_base_id=1, platform_item_id="ready-A"),
            KnowledgeBaseVideo(knowledge_base_id=1, platform_item_id="pending-A"),
            KnowledgeBaseVideo(knowledge_base_id=1, platform_item_id="failed-A"),
            KnowledgeBaseVideo(knowledge_base_id=2, platform_item_id="ready-B"),
            ChatSession(id=1, title="桌面旧对话", knowledge_base_id=1),
        ])
        db.flush()
        db.add(ChatMessage(session_id=1, role="user", content="旧聊天私密内容不应读取"))
        db.commit()
    engine.dispose()
    return path


def test_describe_only_fixed_library_and_all_reads_preserve_business_bytes(database):
    before = sha256(database.read_bytes()).hexdigest()
    tools = LocalKnowledgeTools(database, 1, rag=FakeRag([hit()]))
    assert tools.describe() == {"id": 1, "name": "当前测试库", "total_count": 3, "ready_count": 1}
    result = tools.query("解释这个知识点")
    assert result["status"] == "ok"
    assert set(result) == {"status", "knowledge_base_id", "knowledge_base_name", "evidence", "retrieval_mode"}
    assert "不能透露" not in str(result)
    assert "旧聊天私密内容" not in str(result)
    assert sha256(database.read_bytes()).hexdigest() == before


def test_pre_migration_library_schema_remains_readable_without_writing(database):
    with sqlite3.connect(database) as db:
        db.execute("ALTER TABLE knowledge_bases DROP COLUMN deleted_at")
    before = database.read_bytes()
    assert LocalKnowledgeTools(database, 1, rag=FakeRag()).describe()["id"] == 1
    assert database.read_bytes() == before


def test_deleted_library_never_becomes_all_local_or_reaches_retrieval(database):
    with sqlite3.connect(database) as db:
        db.execute("UPDATE knowledge_bases SET deleted_at='2026-01-01' WHERE id=1")
    fake = FakeRag([hit("ready-B")])
    tools = LocalKnowledgeTools(database, 1, rag=fake)
    with pytest.raises(KnowledgeBaseNotFound):
        tools.describe()
    with pytest.raises(KnowledgeBaseNotFound):
        tools.query("合成已删除范围")
    assert fake.calls == []


def test_retrieval_never_receives_global_scope_and_rechecks_injected_hits(database):
    foreign = hit("ready-B", "foreign", body="外库私密正文")
    pending = hit("pending-A", "pending", body="未完成正文")
    fake = FakeRag([foreign, pending, hit()], selected=[foreign, pending, hit()])
    tools = LocalKnowledgeTools(database, 1, rag=fake)
    result = tools.query("  查正文  ")
    assert fake.calls == [("查正文", {"ready-A"})]
    assert [item["platform_item_id"] for item in result["evidence"]] == ["ready-A"]
    assert result["evidence"][0]["url"] == "https://www.douyin.com/video/ready-A"
    assert result["retrieval_mode"] == "hybrid"


def test_empty_library_short_circuits_before_retriever_or_chroma(database):
    fake = FakeRag([hit("ready-B")])
    result = LocalKnowledgeTools(database, 3, rag=fake).query("这个库有哪些依据")
    assert result["status"] == "empty"
    assert result["evidence"] == []
    assert result["retrieval_mode"] == "empty_scope"
    assert fake.calls == []


def test_title_only_and_empty_hits_are_not_presented_as_evidence(database):
    fake = FakeRag([hit(body="视频标题：当前库视频\n作者：测试作者"), hit(chunk="empty", body="")])
    result = LocalKnowledgeTools(database, 1, rag=fake).query("视频具体讲了什么")
    assert result["status"] == "no_evidence"
    assert result["evidence"] == []


def test_chunk_labels_are_unique_deduplicated_and_stable_across_tool_calls(database):
    first, second = hit(), hit(chunk="chunk-2", body="画面分析：\n另一条独立画面证据。")
    fake = FakeRag([first, first, second])
    tools = LocalKnowledgeTools(database, 1, rag=fake)
    evidence = tools.query("第一次查找")["evidence"]
    assert [item["source_id"] for item in evidence] == ["资料1", "资料2"]
    assert [item["source_type"] for item in evidence] == ["speech", "visual"]
    assert evidence[0]["text"] == first["text"]
    fake.hits = [second, first]
    assert [item["source_id"] for item in tools.query("换一个顺序")["evidence"]] == ["资料2", "资料1"]


def test_retriever_cannot_exceed_eight_hits_or_existing_context_budget(database, monkeypatch):
    monkeypatch.setattr(settings, "rag_context_count", 100)
    monkeypatch.setattr(settings, "rag_prompt_max_context_chars", 8000)
    fake = FakeRag([hit(chunk=f"c-{index}", body=f"第{index}条不重复的原始证据。") for index in range(30)])
    assert len(LocalKnowledgeTools(database, 1, rag=fake).query("查找")["evidence"]) == 8
    monkeypatch.setattr(settings, "rag_prompt_max_context_chars", 100)
    result = LocalKnowledgeTools(database, 1, rag=fake).query("查找")
    assert sum(len(item["text"]) + len(item["title"]) for item in result["evidence"]) <= 100


def test_malformed_hit_fields_are_not_forwarded_as_source_identifiers(database):
    malformed = [hit(chunk=19), hit(platform_item_id=19), hit(title=None), hit(body=3)]
    missing_title = hit()
    missing_title.pop("title")
    result = LocalKnowledgeTools(database, 1, rag=FakeRag([*malformed, missing_title, hit()])).query("查找")
    assert [item["chunk_id"] for item in result["evidence"]] == ["chunk-1"]


def test_nonpositive_context_budget_cannot_disable_the_tool_bound(database, monkeypatch):
    monkeypatch.setattr(settings, "rag_prompt_max_context_chars", 0)
    fake = FakeRag([hit()])
    with pytest.raises(ValueError, match="上限必须大于零"):
        LocalKnowledgeTools(database, 1, rag=fake).query("查找")
    assert fake.calls == []


def test_sqlite_connection_rejects_accidental_write_and_session_does_not_autoflush(database):
    before = database.read_bytes()
    tools = LocalKnowledgeTools(database, 1, rag=FakeRag())
    with tools._read_session() as db:
        assert db.autoflush is False
        db.add(KnowledgeBase(name="不应落盘", name_key="不应落盘"))
        assert db.scalar(select(KnowledgeBase.name).where(KnowledgeBase.id == 1)) == "当前测试库"
        with pytest.raises(OperationalError, match="readonly"):
            db.execute(text("UPDATE knowledge_bases SET name='不应落盘' WHERE id=1"))
    assert database.read_bytes() == before


def test_unknown_library_and_missing_file_do_not_create_or_fall_back(database, tmp_path):
    fake = FakeRag([hit()])
    with pytest.raises(KnowledgeBaseNotFound):
        LocalKnowledgeTools(database, 999, rag=fake).query("查找")
    missing = tmp_path / "missing.db"
    with pytest.raises(OperationalError):
        LocalKnowledgeTools(missing, 1, rag=fake).describe()
    assert not missing.exists()
    assert fake.calls == []


@pytest.mark.parametrize("value", [True, 0, -1, "1", None])
def test_library_id_is_fixed_positive_integer(database, value):
    with pytest.raises(ValueError):
        LocalKnowledgeTools(database, value, rag=FakeRag())


@pytest.mark.parametrize("question", ["", " \n ", None, 3])
def test_empty_or_nontext_question_never_runs_retrieval(database, question):
    fake = FakeRag([hit()])
    with pytest.raises(ValueError):
        LocalKnowledgeTools(database, 1, rag=fake).query(question)
    assert fake.calls == []


def test_retrieval_failure_is_not_mislabeled_as_no_evidence(database):
    class FailingRag(FakeRag):
        def _dense_retrieve(self, question, scope):
            raise RuntimeError("isolated synthetic retrieval failure")

    with pytest.raises(RuntimeError, match="synthetic retrieval failure"):
        LocalKnowledgeTools(database, 1, rag=FailingRag()).query("查找")
