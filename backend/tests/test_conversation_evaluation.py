"""Offline conversation runner tests; production models are replaced locally."""
from copy import deepcopy
import json

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.db.base import Base
from app.models.entities import ChatMessage, ChatSession, KnowledgeBase, KnowledgeBaseVideo, VideoCache
from scripts import evaluate_answers as answers
from scripts import evaluate_conversations as exam


def turn(identifier, **overrides):
    result = {"id": identifier, "question": "这个工具怎样安装？",
              "scope": {"knowledge_base_id": 1, "label": "工具"}, "answerable": True,
              "evidence": [{"video_id": "ready", "title": "安装工具", "start_seconds": 0,
                            "end_seconds": 5, "quote": "先打开设置，然后输入命令。"}],
              "expected_answer": "GOLD_NOT_MODEL_INPUT", "rubric_points": ["打开设置", "输入命令"],
              "session_action": "continue", "expected_behavior": "answer"}
    result.update(overrides)
    return result


def scenario(identifier="S1", turns=None, **extra):
    return {"id": identifier, "title": "测试", "category": "追问", "reuse_session": True,
            "turns": turns or [turn(identifier+"-1"), turn(identifier+"-2")], **extra}


def dataset(*scenarios):
    return {"schema_version": 1, "scenarios": list(scenarios or [scenario()])}


def populate(db):
    db.add_all([KnowledgeBase(id=1, name="工具", name_key="工具"),
                KnowledgeBase(id=2, name="空库", name_key="空库"),
                VideoCache(platform_item_id="ready", title="安装工具", status="done",
                           transcript_text="先打开设置，然后输入命令。",
                           analysis_json=json.dumps({"transcript_segments": [
                               {"start_seconds": 0, "end_seconds": 5, "text": "先打开设置，然后输入命令。"}]}))])
    db.flush()
    db.add(KnowledgeBaseVideo(knowledge_base_id=1, platform_item_id="ready"))
    db.commit()


@pytest.fixture
def db():
    engine = create_engine("sqlite://", poolclass=StaticPool)
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        populate(session)
        yield session
    engine.dispose()


@pytest.fixture
def fake_models(monkeypatch):
    from app.services import rag_service as production
    calls = []
    def answer(system_prompt, user_prompt, temperature=.6, max_tokens=2048, timeout=60.):
        calls.append(user_prompt)
        yield "先打开设置，然后输入命令。[来源: 资料1]"
    monkeypatch.setattr(production.llm_client, "stream_chat", answer)
    monkeypatch.setattr(production.llm_client, "chat", lambda *args, **kwargs: pytest.fail("no compression"))
    monkeypatch.setattr(production.embedding_client, "embed_text", lambda *args, **kwargs: pytest.fail("no real embedding"))
    def dense(self, query, scope_ids=None, top_k=None):
        if scope_ids == set():
            return []
        assert scope_ids == {"ready"}
        return [{"platform_item_id": "ready", "title": "安装工具", "text": "先打开设置，然后输入命令。",
                 "chunk_id": "r1", "score": .8}]
    monkeypatch.setattr(production.RagService, "_dense_retrieve", dense)
    return calls


def test_reuses_session_within_scenario_and_new_session_between_scenarios(db, fake_models):
    rows = exam.run_scenarios(db, dataset(scenario("A"), scenario("B")))
    assert [row["status"] for row in rows] == ["completed"]*4
    assert rows[0]["input_session_id"] is None
    assert rows[1]["input_session_id"] == rows[0]["isolated_session_id"]
    assert rows[0]["isolated_session_id"] != rows[2]["isolated_session_id"]
    assert "最近对话历史" not in fake_models[0]
    assert "最近对话历史" in fake_models[1]
    assert "最近对话历史" not in fake_models[2]
    assert all("GOLD_NOT_MODEL_INPUT" not in prompt for prompt in fake_models)
    assert exam.snapshot_chat_counts(db) == {"chat_sessions": 2, "chat_messages": 8}


def test_scope_rejection_never_calls_models_or_adds_messages(db, fake_models):
    first = exam.execute_scoped_case(db, exam.strict_case(scenario(), turn("A")), None)
    case = exam.strict_case(scenario(), turn("B", answerable=False, evidence=[],
                                            scope={"knowledge_base_id": 2, "label": "空库"}))
    before = exam.snapshot_chat_counts(db)
    row = exam.execute_scope_rejection(db, case, first["isolated_session_id"])
    assert row["status"] == "expected_scope_error_passed"
    assert row["forbidden_model_call_count"] == 0
    assert row["isolated_counts_before"] == row["isolated_counts_after"] == before
    assert len(fake_models) == 1


def test_scope_error_then_new_session_does_not_reuse_old_history(db, fake_models):
    other = {"knowledge_base_id": 2, "label": "空库"}
    data = dataset(scenario(turns=[turn("t1"),
        turn("t2", scope=other, evidence=[], answerable=False,
             session_action="expect_scope_error", expected_behavior="scope_error"),
        turn("t3", scope=other, evidence=[], answerable=False, session_action="new_session")]))
    rows = exam.run_scenarios(db, data)
    assert [r["status"] for r in rows] == ["completed", "expected_scope_error_passed", "completed"]
    assert rows[2]["input_session_id"] is None
    assert rows[0]["isolated_session_id"] != rows[2]["isolated_session_id"]
    assert "最近对话历史" not in fake_models[-1]


def test_seed_history_remains_in_disposable_copy_and_only_its_scenario(tmp_path, fake_models):
    live = tmp_path/"live.sqlite3"
    engine = create_engine("sqlite:///"+live.as_posix())
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        populate(db)
        session = ChatSession(title="PRIVATE_EXISTING_USER")
        db.add(session); db.flush()
        db.add(ChatMessage(session_id=session.id, role="user", content="PRIVATE_LIVE_TEXT"))
        db.commit()
    engine.dispose()
    url = "sqlite:///"+live.as_posix()
    before = answers.live_chat_counts(url)
    seeds = [{"role": "user", "content": "TEST_SEED_USER"},
             {"role": "assistant", "content": "TEST_SEED_FALSE_ASSISTANT"}]
    with answers.writable_database_snapshot(url) as isolated:
        exam.clear_snapshot_history(isolated)
        rows = exam.run_scenarios(isolated, dataset(scenario("A", seed_history=seeds), scenario("B")))
        assert rows[0]["input_session_id"] is not None
    assert "TEST_SEED_FALSE_ASSISTANT" in fake_models[0]
    assert "TEST_SEED_FALSE_ASSISTANT" not in fake_models[2]
    assert all("PRIVATE_LIVE_TEXT" not in prompt for prompt in fake_models)
    assert answers.live_chat_counts(url) == before
    engine = create_engine(url)
    with Session(engine) as original:
        assert original.scalar(select(ChatMessage.content)) == "PRIVATE_LIVE_TEXT"
    engine.dispose()


def test_failed_turn_blocks_dependent_turn_until_explicit_new_session(db, monkeypatch):
    calls = []
    def execute(db, case, session_id, service):
        calls.append((case["id"], session_id))
        return {"id": case["id"], "status": "error" if case["id"] == "a" else "completed",
                "isolated_session_id": 42}
    monkeypatch.setattr(exam, "execute_scoped_case", execute)
    rows = exam.run_scenarios(db, dataset(scenario(turns=[turn("a"), turn("b"),
                                                        turn("c", session_action="new_session")])))
    assert [r["status"] for r in rows] == ["error", "skipped_dependency_error", "completed"]
    assert calls == [("a", None), ("c", None)]


def test_loader_rejects_implicit_scope_change_and_duplicate_turn(tmp_path):
    path = tmp_path/"data.json"
    data = dataset(scenario())
    data["scenarios"][0]["turns"][1]["scope"] = {"knowledge_base_id": 2, "label": "空库"}
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(ValueError, match="scope changes"):
        exam.load_scenarios(path)
    data = dataset(scenario())
    data["scenarios"][0]["turns"][1]["id"] = data["scenarios"][0]["turns"][0]["id"]
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(ValueError, match="invalid or duplicate"):
        exam.load_scenarios(path)


def test_dry_run_validates_without_model_or_index_calls_and_refuses_overwrite(tmp_path, db, monkeypatch):
    from app.core.config import settings
    from app.services import rag_service as production
    live = tmp_path/"live.sqlite3"
    engine = create_engine("sqlite:///"+live.as_posix())
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        populate(session)
    engine.dispose()
    monkeypatch.setattr(settings, "database_url", "sqlite:///"+live.as_posix())
    def forbidden(*args, **kwargs):
        pytest.fail("dry run must not invoke model or Chroma snapshot")
    monkeypatch.setattr(production.llm_client, "chat", forbidden)
    monkeypatch.setattr(production.llm_client, "stream_chat", forbidden)
    monkeypatch.setattr(production.embedding_client, "embed_text", forbidden)
    monkeypatch.setattr(exam.retrieval, "snapshot_chroma", forbidden)
    path, output = tmp_path/"dataset.json", tmp_path/"report.json"
    path.write_text(json.dumps(dataset()), encoding="utf-8")
    assert exam.main(["--dataset", str(path), "--output", str(output)]) == 0
    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["summary"]["status_counts"] == {"validated": 2}
    assert report["fingerprint"]["index"] == {"status": "not_loaded_in_dry_run"}
    assert report["live_chat_counts_before"] == report["live_chat_counts_after"]
    original = output.read_bytes()
    with pytest.raises(SystemExit):
        exam.main(["--dataset", str(path), "--output", str(output)])
    assert output.read_bytes() == original


def test_invalid_gold_stops_before_paid_run(tmp_path, db, monkeypatch):
    from contextlib import contextmanager
    from app.core.config import settings
    @contextmanager
    def copy(*args):
        yield db
    monkeypatch.setattr(answers, "writable_database_snapshot", copy)
    monkeypatch.setattr(answers, "live_chat_counts", lambda *args: {})
    monkeypatch.setattr(answers, "database_path", lambda *args: tmp_path/"live.sqlite3")
    monkeypatch.setattr(exam.retrieval, "snapshot_chroma", lambda *args: pytest.fail("must validate first"))
    data = dataset()
    data["scenarios"][0]["turns"][1]["evidence"][0]["quote"] = "原稿不存在的金标准内容"
    path = tmp_path/"invalid.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    output = tmp_path/"invalid-output.json"
    assert exam.main(["--dataset", str(path), "--output", str(output), "--run"]) == 1
    assert not output.exists()
