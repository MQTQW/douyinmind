"""The retrieval exam only reads isolated fixtures; no model/network calls."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path

import pytest
from sqlalchemy import create_engine, select, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.db.base import Base
from app.models.entities import KnowledgeBase, KnowledgeBaseVideo, VideoCache
from scripts import evaluate_retrieval as exam


def case(**overrides):
    result = {"id": "q1", "question": "工具应该怎样安装？", "scope": {"knowledge_base_id": 1, "label": "工具"},
              "answerable": True, "expected_route": "vector", "category": "步骤", "expected_answer": "先打开设置，然后输入命令。",
              "evidence": [{"video_id": "ready", "title": "安装工具", "start_seconds": 0, "end_seconds": 5,
                            "quote": "先打开设置，然后输入命令。"}]}
    result.update(overrides)
    return result


def hit(video_id="ready", body="先打开设置，然后输入命令。", **overrides):
    result = {"platform_item_id": video_id, "text": body, "title": "安装工具", "chunk_id": video_id + ":1", "score": .8}
    result.update(overrides)
    return result


@pytest.fixture
def db():
    engine = create_engine("sqlite://", poolclass=StaticPool)
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add_all([KnowledgeBase(id=1, name="工具", name_key="工具"), KnowledgeBase(id=2, name="空库", name_key="空库"),
                         VideoCache(platform_item_id="ready", title="安装工具", status="done", transcript_text="先打开设置，然后输入命令。再按保存。",
                                    analysis_json=json.dumps({"transcript_segments": [
                                        {"start_seconds": 0, "end_seconds": 5, "text": "先打开设置，然后输入命令。"},
                                        {"start_seconds": 5, "end_seconds": 8, "text": "最后点击保存按钮。"}]})),
                         VideoCache(platform_item_id="outside", title="其他视频", status="done", transcript_text="知识库外的内容"),
                         VideoCache(platform_item_id="failed", title="失败视频", status="failed")])
        session.flush()
        session.add_all([KnowledgeBaseVideo(knowledge_base_id=1, platform_item_id="ready"),
                         KnowledgeBaseVideo(knowledge_base_id=1, platform_item_id="failed")])
        session.commit()
        yield session
    engine.dispose()


def test_validates_original_segment_quote_and_done_scope(db):
    assert exam.validate_case(db, case()) == ([], {"ready"})
    assert exam.resolve_scope(db, None) == {"ready", "outside"}
    assert exam.resolve_scope(db, 2) == set()
    assert exam.validate_case(db, case(scope={"knowledge_base_id": 999, "label": "不存在"}))[0]


@pytest.mark.parametrize("change", [
    {"answerable": "true"}, {"answerable": 1}, {"question": ""}, {"expected_route": "direct"},
    {"scope": {"knowledge_base_id": True, "label": "工具"}},
    {"scope": {"knowledge_base_id": "1", "label": "工具"}},
    {"scope": {"knowledge_base_id": 1, "label": "工具", "extra": 1}},
    {"evidence": []}, {"answerable": False}, {"unknown": "reject"},
])
def test_strict_case_schema_rejects_ambiguous_input(db, change):
    assert exam.validate_case(db, case(**change))[0]


@pytest.mark.parametrize("change, expected", [
    ({"quote": "原话里没有这句话"}, "quote is absent"),
    ({"start_seconds": 5, "end_seconds": 8}, "quote is absent"),
    ({"start_seconds": 0, "end_seconds": 99}, "interval extends"),
    ({"video_id": "outside", "title": "其他视频"}, "outside the selected scope"),
    ({"video_id": "failed"}, "not done"),
    ({"title": "错误标题"}, "title differs"),
    ({"start_seconds": True}, "finite times"),
    ({"end_seconds": float("nan")}, "finite times"),
    ({"quote": "!!!。"}, "non-punctuation"),
])
def test_bad_evidence_is_not_scored(db, change, expected):
    item = case()
    item["evidence"][0].update(change)
    errors, _ = exam.validate_case(db, item)
    assert any(expected in message for message in errors)


def test_cross_segment_quote_must_be_inside_annotated_interval(db):
    item = case()
    item["evidence"][0].update(end_seconds=8, quote="然后输入命令。最后点击保存按钮。")
    assert exam.validate_case(db, item)[0] == []
    item["evidence"][0]["end_seconds"] = 5
    assert exam.validate_case(db, item)[0]


def test_evidence_needs_both_correct_video_and_original_chunk_text():
    item = case()
    results = [hit(body="此处只有标题", title=item["evidence"][0]["quote"]),
               hit("outside"), hit(body="先打开设置 然后输入命令！")]
    ranked, metrics = exam.score_hits(item, results)
    assert [row["evidence_match"] for row in ranked] == [False, False, True]
    assert metrics["hit_video_at_5"] is True
    assert metrics["hit_evidence_at_5"] is True
    assert metrics["mrr_evidence"] == pytest.approx(1 / 3)
    assert ranked[-1]["matched_evidence_indices"] == [0]


def test_rank_six_hits_eight_but_not_five_and_multi_evidence_is_counted_once():
    item = case()
    item["evidence"].append(item["evidence"][0] | {"quote": "最后点击保存按钮。"})
    results = [hit(body="无关的段落") for _ in range(4)] + [hit(), hit(body="最后点击保存按钮。"), hit()]
    ranked, metrics = exam.score_hits(item, results)
    assert metrics["hit_evidence_at_5"] is True
    assert metrics["evidence_coverage_at_5"] == .5
    assert metrics["evidence_coverage_at_8"] == 1
    assert metrics["all_evidence_at_5"] is False
    assert metrics["all_evidence_at_8"] is True
    assert metrics["distinct_video_at_5"] == 1
    only_late = exam.score_hits(case(), [hit(body="无关") for _ in range(5)] + [hit()])[1]
    assert only_late["hit_evidence_at_5"] is False
    assert only_late["hit_evidence_at_8"] is True
    assert only_late["mrr_evidence"] == pytest.approx(1 / 6)


def test_metadata_only_chunk_cannot_match_even_when_quote_appears_in_its_title():
    quote = case()["evidence"][0]["quote"]
    ranked, metrics = exam.score_hits(case(), [hit(body="视频标题：" + quote + "\n作者：某人", title=quote)])
    assert ranked[0]["metadata_only"] is True
    assert metrics["hit_video_at_5"] is True
    assert metrics["hit_evidence_at_5"] is False


def test_negative_cases_do_not_turn_returned_chunks_into_answerability_scores():
    ranked, metrics = exam.score_hits(case(answerable=False, evidence=[]), [hit()])
    assert metrics["returned_evidence_count"] == 1
    assert metrics["needs_manual_no_answer_review"] is True
    for key, value in metrics.items():
        if key.startswith(("hit_", "mrr_", "all_", "evidence_coverage")):
            assert value is None


def test_validation_default_never_calls_embedding_or_retriever(db, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("validation must not call an external model or retriever")
    from app.services import rag_service
    monkeypatch.setattr(rag_service.embedding_client, "embed_text", forbidden)
    report = exam.evaluate_dataset(db, {"cases": [case()]}, retriever=forbidden)
    assert report["cases"][0]["status"] == "validated"
    assert report["summary"]["dense_metrics"]["hit_evidence_at_5"] is None


def test_router_bypass_and_direct_dense_probe_are_separate(db, monkeypatch):
    from app.services import rag_service
    monkeypatch.setattr(rag_service.llm_client, "chat", lambda *a, **k: pytest.fail("no answer or compression call"))
    # Inject a bypass to exercise evaluator accounting independently of router
    # fixes. "hi" inside Whisper is no longer a real production bypass.
    monkeypatch.setattr(rag_service.RagService, "_route",
                        lambda self, question, has_data: "direct" if question.startswith("Whisper") else "vector")
    calls = []
    def retrieve(question, *, scope_ids, top_k):
        calls.append((question, scope_ids, top_k))
        return [hit()]
    data = {"cases": [case(question="Whisper 的安装方法是什么？"), case(id="q2")]}
    report = exam.evaluate_dataset(db, data, run=True, retriever=retrieve, chroma_count=3)
    first, second = report["cases"]
    assert first["production_route"] == "direct"
    assert first["router_bypass"] is True
    assert first["dense_metrics"]["hit_evidence_at_5"] is True
    assert first["production_context_candidates_top5"] == []
    assert first["production_metrics"] is None
    assert second["production_route"] == "vector"
    assert second["production_metrics"]["hit_evidence_at_5"] is True
    assert len(calls) == 2 and all(call[1] == {"ready"} for call in calls)
    assert report["summary"]["production_vector_evidence_hit_rate_at_5"] == .5
    assert report["summary"]["dense_metrics"]["hit_evidence_at_5"] == 1


def test_invalid_cases_do_not_execute_and_duplicate_ids_invalidate_both(db):
    calls = []
    invalid = case(id="bad", answerable="true")
    report = exam.evaluate_dataset(db, {"cases": [case(), deepcopy(case()), invalid, case(id="valid")]},
                                   run=True, retriever=lambda *a, **k: calls.append(k) or [hit()], chroma_count=3)
    assert [row["status"] for row in report["cases"]] == ["invalid", "invalid", "invalid", "evaluated"]
    assert len(calls) == 1


def test_empty_library_negative_never_falls_back_or_embeds(db):
    def forbidden(*args, **kwargs):
        pytest.fail("empty scopes must short circuit")
    report = exam.evaluate_dataset(db, {"cases": [case(answerable=False, evidence=[], scope={"knowledge_base_id": 2, "label": "空库"})]},
                                   run=True, retriever=forbidden, chroma_count=3)
    row = report["cases"][0]
    assert row["production_route"] == "direct"
    assert row["dense_metrics"]["returned_evidence_count"] == 0
    assert row["dense_metrics"]["hit_evidence_at_5"] is None


def test_global_dense_uses_only_done_ids_and_rejects_scope_leak(db):
    scopes = []
    report = exam.evaluate_dataset(db, {"cases": [case(scope={"knowledge_base_id": None, "label": "全库"})]},
                                   run=True, retriever=lambda *a, **k: scopes.append(k["scope_ids"]) or [hit("failed")], chroma_count=3)
    assert scopes == [{"ready", "outside"}]
    assert report["cases"][0]["retrieval_error"] == "scope_violation"
    assert report["summary"]["evaluated_answerable_count"] == 0


def test_exceptions_do_not_leak_sensitive_details(db):
    def fail(*args, **kwargs):
        raise RuntimeError("credential-must-not-appear")
    report = exam.evaluate_dataset(db, {"cases": [case()]}, run=True, retriever=fail, chroma_count=3)
    assert report["cases"][0]["retrieval_error"] == "RuntimeError"
    assert "credential-must-not-appear" not in json.dumps(report)


def test_actual_context_uses_production_filter_and_budget_not_top_five_candidates(db, monkeypatch):
    from app.core.config import settings
    from app.services.rag_service import RagService
    monkeypatch.setattr(settings, "rag_context_count", 1)
    monkeypatch.setattr(settings, "rag_prompt_max_context_chars", 8000)
    hits = [hit(body="视频标题：安装工具\n作者：某人", chunk_id="ready:0"),
            hit(body="这里没有所问的安装步骤。", chunk_id="ready:1"),
            hit(chunk_id="ready:2")]
    report = exam.evaluate_dataset(db, {"cases": [case()]}, run=True,
                                   retriever=lambda *a, **k: hits, chroma_count=3)
    row = report["cases"][0]
    selected = RagService()._context_hits(hits, {"ready"})
    assert row["dense_metrics"]["hit_evidence_at_5"] is True
    assert row["actual_context_metrics"]["hit_evidence"] is False
    assert row["actual_context_metrics"]["selected_chunk_count"] == 1
    assert row["actual_context_results"][0]["chunk_id"] == selected[0]["chunk_id"] == "ready:1"
    assert row["actual_context_metrics"]["context_chars"] == len(RagService._format_context_blocks(selected))
    assert report["summary"]["actual_context_metrics"]["hit_evidence"] == 0


def test_actual_context_scores_all_selected_blocks_without_an_eight_result_cutoff():
    from app.services.rag_service import RagService
    hits = [hit(body="无关内容", chunk_id=f"ready:{index}") for index in range(9)] + [hit(chunk_id="ready:9")]
    ranked, metrics = exam.score_actual_context(case(), hits, RagService._format_context_blocks(hits))
    assert len(ranked) == metrics["selected_chunk_count"] == 10
    assert metrics["all_evidence"] is True
    assert ranked[-1]["rank"] == 10
    _, negative = exam.score_actual_context(case(answerable=False, evidence=[]), hits, "")
    assert negative["hit_evidence"] is None
    assert negative["needs_manual_no_answer_review"] is True


def test_aggregate_treats_legacy_actual_context_as_unmeasured(db):
    report = exam.evaluate_dataset(db, {"cases": [case()]}, run=True,
                                   retriever=lambda *a, **k: [hit()], chroma_count=3)
    row = report["cases"][0]
    row.pop("actual_context_metrics")
    row.pop("actual_context_results")
    summary = exam._aggregate([row])
    assert summary["dense_metrics"]["hit_evidence_at_5"] == 1
    assert summary["actual_context_metrics"]["evaluated_case_count"] == 0
    assert summary["actual_context_metrics"]["hit_evidence"] is None


def test_corpus_hashes_change_with_content_or_scope_without_exporting_raw_output(db):
    original = exam.corpus_snapshot(db)
    assert original["done_video_count"] == 2
    assert {item["video_id"] for item in original["videos"]} == {"ready", "outside"}
    row = db.scalar(select(VideoCache).where(VideoCache.platform_item_id == "ready"))
    row.transcript_text += "内容有变动"
    db.flush()
    assert exam.corpus_snapshot(db)["sha256"] != original["sha256"]
    assert "先打开设置" not in json.dumps(original, ensure_ascii=False)
    assert "api_key" not in json.dumps(exam.config_snapshot())
    assert {"retrieval_hybrid_enabled", "retrieval_candidate_k", "retrieval_neighbor_chars"} <= exam.config_snapshot().keys()


def test_load_dataset_checks_top_schema_duplicate_keys_and_hash(tmp_path):
    path = tmp_path / "exam.json"
    path.write_text(json.dumps({"schema_version": 1, "cases": [case()]}), encoding="utf-8")
    loaded, digest = exam.load_dataset(path)
    assert loaded["cases"][0]["id"] == "q1"
    assert digest == hashlib.sha256(path.read_bytes()).hexdigest()
    for value in ('{"schema_version": 1, "schema_version": 1, "cases": []}',
                  '{"schema_version": true, "cases": []}',
                  '{"schema_version": 1, "cases": [], "extra": 1}',
                  '{"schema_version": 1, "cases": [NaN]}'):
        path.write_text(value, encoding="utf-8")
        with pytest.raises(ValueError):
            exam.load_dataset(path)


def test_readonly_sqlite_denies_writes_and_preserves_source(tmp_path):
    path = tmp_path / "original.db"
    engine = create_engine("sqlite:///" + path.as_posix())
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE sample (value INTEGER)"))
        connection.execute(text("INSERT INTO sample VALUES (1)"))
    engine.dispose()
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    with exam.readonly_database("sqlite:///" + path.as_posix()) as session:
        assert session.scalar(text("SELECT value FROM sample")) == 1
        with pytest.raises(OperationalError):
            session.execute(text("UPDATE sample SET value=2"))
    assert hashlib.sha256(path.read_bytes()).hexdigest() == digest
    with pytest.raises(FileNotFoundError):
        with exam.readonly_database("sqlite:///" + (tmp_path / "missing.db").as_posix()):
            pass
    assert not (tmp_path / "missing.db").exists()


def test_chroma_snapshot_reuses_search_without_modifying_live_index(tmp_path):
    import chromadb
    from chromadb.config import Settings
    from app.services.chroma_service import COLLECTION_NAME
    source = tmp_path / "live-index"
    client = chromadb.PersistentClient(path=str(source), settings=Settings(anonymized_telemetry=False))
    collection = client.create_collection(COLLECTION_NAME, metadata={"hnsw:space": "cosine"})
    collection.upsert(ids=["ready:0", "outside:0"], embeddings=[[1., 0.], [0., 1.]],
                      documents=["先打开设置，然后输入命令。", "其他资料"],
                      metadatas=[{"platform_item_id": "ready", "title": "安装工具"}, {"platform_item_id": "outside", "title": "其他视频"}])
    client._system.stop()
    original = {str(path.relative_to(source)): hashlib.sha256(path.read_bytes()).hexdigest()
                for path in source.rglob("*") if path.is_file() and not path.name.endswith("-shm")}
    with exam.snapshot_chroma(str(source)) as isolated:
        assert isolated.count() == 2
        result = isolated.search([1., 0.], top_k=8, allowed_video_ids={"ready"})
        assert [item["platform_item_id"] for item in result] == ["ready"]
        assert result[0]["text"] == "先打开设置，然后输入命令。"
    after = {str(path.relative_to(source)): hashlib.sha256(path.read_bytes()).hexdigest()
             for path in source.rglob("*") if path.is_file() and not path.name.endswith("-shm")}
    assert original == after


def test_sql_snapshot_releases_live_lock_before_evaluation(tmp_path):
    path = tmp_path / "live.db"
    engine = create_engine("sqlite:///" + path.as_posix(), connect_args={"timeout": .1})
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE sample (value INTEGER)"))
        connection.execute(text("INSERT INTO sample VALUES (1)"))
    with exam.readonly_database("sqlite:///" + path.as_posix()) as snapshot:
        assert snapshot.scalar(text("SELECT count(*) FROM sample")) == 1
        # A simulated website write must succeed while evaluation is alive.
        with engine.begin() as connection:
            connection.execute(text("INSERT INTO sample VALUES (2)"))
        # The exam retains its frozen corpus even after the site changes.
        assert snapshot.scalar(text("SELECT count(*) FROM sample")) == 1
    with engine.connect() as connection:
        assert connection.scalar(text("SELECT count(*) FROM sample")) == 2
    engine.dispose()
