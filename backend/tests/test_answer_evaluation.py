"""Offline tests: real production pipeline, fake model I/O, disposable stores."""
from copy import deepcopy
import json

import pytest
from sqlalchemy import create_engine, select, func
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.db.base import Base
from app.models.entities import ChatMessage, ChatSession, KnowledgeBase, KnowledgeBaseVideo, VideoCache
from scripts import evaluate_answers as exam
from scripts import evaluate_retrieval as retrieval


def question(**overrides):
    result = {"id": "q1", "question": "工具应该怎样安装？", "scope": {"knowledge_base_id": 1, "label": "工具"},
              "answerable": True, "expected_route": "vector", "category": "步骤", "expected_answer": "GOLD_EXPECTED_NEVER_IN_PROMPT",
              "evidence": [{"video_id": "ready", "title": "安装工具", "start_seconds": 0, "end_seconds": 5,
                            "quote": "先打开设置，然后输入命令。"}]}
    result.update(overrides)
    return result


@pytest.fixture
def db():
    engine = create_engine("sqlite://", poolclass=StaticPool)
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add_all([KnowledgeBase(id=1, name="工具", name_key="工具"),
                         VideoCache(platform_item_id="ready", title="安装工具", status="done",
                                    transcript_text="先打开设置，然后输入命令。",
                                    analysis_json=json.dumps({"transcript_segments": [
                                        {"start_seconds": 0, "end_seconds": 5, "text": "先打开设置，然后输入命令。"}]}))])
        session.flush()
        session.add(KnowledgeBaseVideo(knowledge_base_id=1, platform_item_id="ready"))
        session.commit()
        yield session
    engine.dispose()


@pytest.fixture
def fake_models(monkeypatch):
    from app.services import rag_service as production
    calls = []
    def compress(system_prompt, user_prompt, temperature=.6, max_tokens=2048, timeout=60.):
        calls.append(("compression", system_prompt, user_prompt, temperature, max_tokens))
        return "先打开设置。然后输入命令。"
    def answer(system_prompt, user_prompt, temperature=.6, max_tokens=2048, timeout=60.):
        calls.append(("answer", system_prompt, user_prompt, temperature, max_tokens))
        yield "## 操作方法\n"
        yield "先打开设置，然后输入命令。[来源: 资料1]"
    monkeypatch.setattr(production.llm_client, "chat", compress)
    monkeypatch.setattr(production.llm_client, "stream_chat", answer)
    def dense(self, query, scope_ids=None, top_k=None):
        assert scope_ids == {"ready"}
        return [{"platform_item_id": "ready", "title": "安装工具", "text": "检索原文 " + str(index),
                 "chunk_id": str(index), "score": .8} for index in range(4)]
    monkeypatch.setattr(production.RagService, "_dense_retrieve", dense)
    return calls


def test_real_pipeline_records_verbatim_context_final_prompts_and_postprocessing(db, fake_models):
    row = exam.execute_case(db, question())
    assert row["status"] == "completed"
    assert row["production_route"] == "vector"
    assert len(row["dense_hits"]) == 4
    assert len(row["actual_context_results"]) == row["actual_context_metrics"]["selected_chunk_count"] == 4
    assert [call["kind"] for call in row["model_calls"]] == ["answer"]
    assert [call[0] for call in fake_models] == ["answer"]
    assert row["model_calls"][-1]["temperature"] == .6
    assert row["model_calls"][-1]["max_tokens"] == 2048
    for index in range(4):
        assert f"检索原文 {index}" in row["model_calls"][-1]["user_prompt"]
    assert row["raw_answer"].startswith("##")
    assert "##" not in row["answer"]
    assert "先打开设置，然后输入命令。" in row["answer"]
    assert "[来源: 安装工具]" in row["answer"]
    assert row["sources"][0]["url"] == "https://www.douyin.com/video/ready"
    assert row["sources"][0]["platform_item_id"] == "ready"
    assert row["events"][-1] == {"event": "done", "data": {"ok": True}}
    assert row["trace"]["route"] == "vector"
    assert 0 <= row["first_delta_ms"] <= row["latency_ms"]
    assert "GOLD_EXPECTED_NEVER_IN_PROMPT" not in json.dumps(row["model_calls"])
    assert db.scalar(select(func.count()).select_from(ChatSession)) == 1
    assert db.scalar(select(func.count()).select_from(ChatMessage)) == 2


def test_answer_records_final_budget_selection_and_empty_context(db, fake_models, monkeypatch):
    from app.core.config import settings
    from app.services.rag_service import RagService
    monkeypatch.setattr(settings, "rag_context_count", 2)
    first = exam.execute_case(db, question())
    assert len(first["dense_hits"]) == 4
    assert [row["chunk_id"] for row in first["actual_context_results"]] == ["0", "1"]
    assert "检索原文 2" not in first["model_calls"][-1]["user_prompt"]
    assert first["actual_context_metrics"]["context_chars"] == len(RagService._format_context_blocks(first["dense_hits"][:2]))
    monkeypatch.setattr(RagService, "_dense_retrieve", lambda *args, **kwargs: [])
    second = exam.execute_case(db, question(id="q2"))
    assert second["actual_context_results"] == []
    assert second["actual_context_metrics"]["selected_chunk_count"] == 0
    assert second["actual_context_metrics"]["context_chars"] == 0
    assert "GOLD_EXPECTED_NEVER_IN_PROMPT" not in json.dumps(second["model_calls"])


def test_each_question_has_a_new_session_and_never_injects_previous_answer(db, fake_models):
    first = exam.execute_case(db, question())
    second = exam.execute_case(db, question(id="q2"))
    assert first["isolated_session_id"] != second["isolated_session_id"]
    assert "最近对话历史" not in second["model_calls"][-1]["user_prompt"]
    assert db.scalar(select(func.count()).select_from(ChatSession)) == 2


def test_final_stream_failure_is_an_error_not_a_pass_and_message_is_redacted(db, fake_models, monkeypatch):
    from app.services import rag_service as production
    def fail(system_prompt, user_prompt, temperature=.6, max_tokens=2048, timeout=60.):
        yield "部分回答"
        raise RuntimeError("never export upstream token=private-value")
    monkeypatch.setattr(production.llm_client, "stream_chat", fail)
    row = exam.execute_case(db, question())
    assert row["status"] == "error"
    assert row["raw_answer"] == "部分回答"
    assert row["answer"] == ""
    assert row["model_calls"][-1]["error_type"] == "RuntimeError"
    assert "private-value" not in json.dumps(row)
    assert db.scalar(select(func.count()).select_from(ChatSession)) == 0


def test_removed_compression_cannot_invent_or_truncate_evidence(db, fake_models, monkeypatch):
    from app.services import rag_service as production
    def fail(system_prompt, user_prompt, temperature=.6, max_tokens=2048, timeout=60.):
        raise RuntimeError("private upstream exception")
    monkeypatch.setattr(production.llm_client, "chat", fail)
    row = exam.execute_case(db, question())
    assert row["status"] == "completed"
    assert row["compression_error_count"] == 0
    assert [call["kind"] for call in row["model_calls"]] == ["answer"]
    assert "检索原文 0" in row["model_calls"][-1]["user_prompt"]
    assert "private upstream exception" not in json.dumps(row)


def test_writable_copy_never_changes_live_sessions_and_releases_handles(tmp_path):
    live = tmp_path / "live.sqlite3"
    engine = create_engine("sqlite:///" + live.as_posix())
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        db.add(ChatSession(title="existing private chat"))
        db.commit()
    engine.dispose()
    url = "sqlite:///" + live.as_posix()
    assert exam.live_chat_counts(url) == {"chat_sessions": 1, "chat_messages": 0}
    with exam.writable_database_snapshot(url) as snapshot:
        snapshot.add(ChatSession(title="exam only"))
        snapshot.commit()
        assert snapshot.scalar(select(func.count()).select_from(ChatSession)) == 2
        # A live write remains possible while the exam snapshot is open.
        live_engine = create_engine(url)
        with Session(live_engine) as writable_live:
            writable_live.add(ChatSession(title="user can continue using site"))
            writable_live.commit()
        live_engine.dispose()
    assert exam.live_chat_counts(url) == {"chat_sessions": 2, "chat_messages": 0}
    live.rename(tmp_path / "released.sqlite3")


def reference_for(data, digest, corpus, configuration):
    return {"mode": "retrieval_only", "dataset_sha256": digest, "corpus": corpus,
            "configuration": configuration,
            "cases": [{**{key: case[key] for key in ("id", "question", "scope", "answerable")}, "status": "evaluated"}
                      for case in data["cases"]]}


@pytest.mark.parametrize("change", ["dataset", "corpus", "configuration", "question", "scope", "incomplete"])
def test_reference_rejects_comparisons_with_changed_inputs(change):
    data, corpus, config = {"cases": [question()]}, {"sha256": "same"}, {"retrieval_top_k": 8}
    reference = reference_for(data, "digest", corpus, config)
    exam.validate_reference(reference, data, "digest", corpus, config)
    reference = deepcopy(reference)
    if change == "dataset":
        reference["dataset_sha256"] = "changed"
    elif change in ("corpus", "configuration"):
        reference[change] = {"changed": True}
    elif change == "incomplete":
        reference["cases"][0]["status"] = "validated"
    else:
        reference["cases"][0][change] = "changed"
    with pytest.raises(ValueError):
        exam.validate_reference(reference, data, "digest", corpus, config)


def test_resume_keeps_completed_and_error_results_without_regenerating():
    rows = [{key: question()[key] for key in ("id", "question", "scope", "category", "answerable")} | {"status": "validated"}]
    fingerprint = {"dataset_sha256": "fixed", "configuration": {"max_tokens": 2048}}
    previous = {"mode": "answer_generation", "fingerprint": fingerprint, "cases": deepcopy(rows)}
    for status in ("completed", "error", "validated"):
        previous["cases"][0]["status"] = status
        assert exam.resume_rows(previous, fingerprint, rows)[0]["status"] == status
    previous["cases"][0]["status"] = "running"
    with pytest.raises(ValueError):
        exam.resume_rows(previous, fingerprint, rows)
    previous["cases"][0]["status"] = "completed"
    with pytest.raises(ValueError):
        exam.resume_rows(previous, {"dataset_sha256": "changed"}, rows)


def test_default_validation_does_not_call_models_or_chroma(tmp_path, db, monkeypatch):
    from contextlib import contextmanager
    from app.services import rag_service as production
    def forbidden(*args, **kwargs):
        pytest.fail("validation called a model or opened Chroma")
    monkeypatch.setattr(production.llm_client, "chat", forbidden)
    monkeypatch.setattr(production.llm_client, "stream_chat", forbidden)
    monkeypatch.setattr(production.embedding_client, "embed_text", forbidden)
    monkeypatch.setattr(retrieval, "snapshot_chroma", forbidden)
    @contextmanager
    def snapshot(*args):
        yield db
    monkeypatch.setattr(exam, "writable_database_snapshot", snapshot)
    monkeypatch.setattr(exam, "live_chat_counts", lambda *args: {"chat_sessions": 0, "chat_messages": 0})
    monkeypatch.setattr(exam, "database_path", lambda *args: tmp_path / "live.sqlite3")
    dataset_path, reference_path, output_path = [tmp_path / name for name in ("dataset.json", "reference.json", "output.json")]
    dataset_path.write_text(json.dumps({"schema_version": 1, "cases": [question()]}), encoding="utf-8")
    data, digest = retrieval.load_dataset(dataset_path)
    reference = reference_for(data, digest, retrieval.corpus_snapshot(db), retrieval.config_snapshot())
    reference_path.write_text(json.dumps(reference), encoding="utf-8")
    assert exam.main(["--dataset", str(dataset_path), "--reference", str(reference_path), "--output", str(output_path)]) == 0
    report = json.loads(output_path.read_text(encoding="utf-8"))
    assert report["mode"] == "validation_only"
    assert report["summary"]["status_counts"] == {"validated": 1}
    assert report["summary"]["quality_grading"] == "pending_human_review"


def test_atomic_report_is_parseable_and_replaces_only_own_temporary_file(tmp_path):
    path = tmp_path / "results.json"
    exam.atomic_json(path, {"cases": []})
    exam.atomic_json(path, {"cases": [{"status": "completed", "answer": "文字"}]})
    assert json.loads(path.read_text(encoding="utf-8"))["cases"][0]["answer"] == "文字"
    assert not path.with_name(path.name + ".tmp").exists()


def test_atomic_report_retries_only_a_short_windows_rename_lock(tmp_path, monkeypatch):
    path = tmp_path / "results.json"
    exam.atomic_json(path, {"old": True})
    original_replace = exam.os.replace
    attempts, sleeps = [], []
    def locked_then_replace(source, destination):
        attempts.append((source, destination))
        if len(attempts) < 3:
            raise PermissionError("transient reader lock")
        return original_replace(source, destination)
    monkeypatch.setattr(exam.os, "replace", locked_then_replace)
    monkeypatch.setattr(exam.time, "sleep", sleeps.append)
    exam.atomic_json(path, {"new": True})
    assert len(attempts) == 3
    assert sleeps == [.05, .1]
    assert json.loads(path.read_text(encoding="utf-8")) == {"new": True}
    monkeypatch.setattr(exam.os, "replace", lambda *args: (_ for _ in ()).throw(PermissionError("still locked")))
    with pytest.raises(PermissionError):
        exam.atomic_json(path, {"lost": True})
    assert json.loads(path.read_text(encoding="utf-8")) == {"new": True}
    assert not path.with_name(path.name + ".tmp").exists()


def test_cleanup_failure_preserves_twenty_four_answers_final_audit_and_failed_exit(tmp_path, db, monkeypatch):
    from contextlib import contextmanager
    from app.core.config import settings
    live = tmp_path / "live.sqlite3"
    engine = create_engine("sqlite:///" + live.as_posix())
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(ChatSession(title="private user history"))
        session.commit()
    engine.dispose()
    monkeypatch.setattr(settings, "database_url", "sqlite:///" + live.as_posix())
    before = exam.live_chat_counts(settings.database_url)
    @contextmanager
    def snapshot(*args):
        yield db
    class Chroma:
        pass
    @contextmanager
    def locked_cleanup(*args):
        yield Chroma()
        raise PermissionError("temporary Chroma file still held on Windows")
    monkeypatch.setattr(exam, "writable_database_snapshot", snapshot)
    monkeypatch.setattr(retrieval, "snapshot_chroma", locked_cleanup)
    monkeypatch.setattr(exam, "index_fingerprint", lambda *args: {"sha256": "fixed", "chunk_count": 1})
    calls = []
    def execute(db, case):
        calls.append(case["id"])
        # Exercise writes in the isolated test database, never in the live DB.
        db.add(ChatSession(title="isolated exam"))
        db.commit()
        return {key: case[key] for key in ("id", "question", "scope", "category", "answerable")} | {
            "status": "completed", "answer": "one saved answer", "model_calls": []}
    monkeypatch.setattr(exam, "execute_case", execute)
    paths = [tmp_path / name for name in ("dataset.json", "reference.json", "output.json")]
    dataset_path, reference_path, output_path = paths
    dataset_path.write_text(json.dumps({"schema_version": 1, "cases": [question(id=f"q{i}") for i in range(24)]}), encoding="utf-8")
    data, digest = retrieval.load_dataset(dataset_path)
    reference_path.write_text(json.dumps(reference_for(data, digest, retrieval.corpus_snapshot(db), retrieval.config_snapshot())), encoding="utf-8")
    result = exam.main(["--dataset", str(dataset_path), "--reference", str(reference_path),
                        "--output", str(output_path), "--run"])
    assert result == 1
    report = json.loads(output_path.read_text(encoding="utf-8"))
    assert calls == [f"q{i}" for i in range(24)]
    assert len(report["cases"]) == 24
    assert report["summary"]["status_counts"] == {"completed": 24}
    assert report["runner_status"] == "failed"
    assert report["runner_error_type"] == "PermissionError"
    assert report["live_chat_counts_after"] == report["live_chat_counts_before"] == before
    assert exam.live_chat_counts(settings.database_url) == before
    assert "temporary Chroma file" not in output_path.read_text(encoding="utf-8")
    original_bytes = output_path.read_bytes()
    # A refused resume must not let finalization replace the existing answers
    # with freshly validated rows from a different source-code fingerprint.
    monkeypatch.setattr(exam, "source_fingerprints", lambda: {"changed": "code"})
    result = exam.main(["--dataset", str(dataset_path), "--reference", str(reference_path),
                        "--output", str(output_path), "--run", "--resume"])
    assert result == 1
    assert output_path.read_bytes() == original_bytes
    assert calls == [f"q{i}" for i in range(24)]


def test_source_fingerprints_include_ranking_expansion_and_chunk_processing():
    fingerprint = exam.source_fingerprints()
    assert {"app/services/retrieval_fusion.py", "app/services/retrieval_context.py",
            "app/services/text_processing.py"} <= fingerprint.keys()
    assert all(len(value) == 64 for value in fingerprint.values())
