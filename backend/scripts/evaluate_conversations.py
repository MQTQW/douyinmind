"""Run fixed multi-turn RAG scenarios in disposable SQL/index copies.

Without --run this validates gold only and never initializes model/index clients.
Each scenario has its own session. Outputs are append-progress artifacts; existing
outputs are refused and failed turns are never automatically regenerated.
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import json
import logging
import os
from pathlib import Path
import sys
import time
from unittest.mock import patch

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from scripts import evaluate_answers as answers
from scripts import evaluate_retrieval as retrieval


def strict_case(scenario: dict, turn: dict) -> dict:
    return {key: turn[key] for key in
            ("id", "question", "scope", "answerable", "evidence", "expected_answer")} | {
                "expected_route": "vector", "category": scenario["category"]}


def load_scenarios(path: Path) -> tuple[dict, str]:
    raw = path.read_bytes()
    if len(raw) > retrieval.MAX_DATASET_BYTES:
        raise ValueError("dataset exceeds size limit")
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate JSON key")
            result[key] = value
        return result
    data = json.loads(raw.decode("utf-8-sig"), object_pairs_hook=unique,
                      parse_constant=lambda value: (_ for _ in ()).throw(ValueError("non-finite JSON")))
    if not isinstance(data, dict) or data.get("schema_version") != 1:
        raise ValueError("schema_version=1 required")
    scenarios = data.get("scenarios")
    if not isinstance(scenarios, list) or not 1 <= len(scenarios) <= 100:
        raise ValueError("1..100 scenarios required")
    identifiers = set()
    for scenario in scenarios:
        if not isinstance(scenario, dict) or not isinstance(scenario.get("id"), str):
            raise ValueError("scenario needs an id")
        if scenario["id"] in identifiers or scenario.get("reuse_session") is not True:
            raise ValueError("scenario ids must be unique and reuse_session true")
        identifiers.add(scenario["id"])
        if not isinstance(scenario.get("category"), str) or not scenario["category"].strip():
            raise ValueError("scenario category required")
        turns = scenario.get("turns")
        if not isinstance(turns, list) or not 1 <= len(turns) <= 20:
            raise ValueError("1..20 turns required")
        previous_scope = turns[0].get("scope")
        for index, turn in enumerate(turns):
            case = strict_case(scenario, turn)
            if retrieval.structural_errors(case) or case["id"] in identifiers:
                raise ValueError("invalid or duplicate turn")
            identifiers.add(case["id"])
            if not isinstance(turn.get("rubric_points"), list) or not turn["rubric_points"]:
                raise ValueError("fixed rubric_points required")
            action = turn.get("session_action", "continue")
            if action not in ("continue", "new_session", "expect_scope_error"):
                raise ValueError("invalid session_action")
            if action == "expect_scope_error":
                if index == 0 or previous_scope == turn["scope"] or turn.get("expected_behavior") != "scope_error":
                    raise ValueError("scope rejection requires an existing different-scope session")
            else:
                if index and turn["scope"] != previous_scope and action != "new_session":
                    raise ValueError("scope changes require explicit new_session or expect_scope_error")
                previous_scope = turn["scope"]
        seeds = scenario.get("seed_history", [])
        if not isinstance(seeds, list) or len(seeds) > 20:
            raise ValueError("invalid seed history")
        for seed in seeds:
            if (not isinstance(seed, dict) or set(seed) != {"role", "content"}
                    or seed["role"] not in ("user", "assistant")
                    or not isinstance(seed["content"], str) or not 1 <= len(seed["content"]) <= 4000):
                raise ValueError("seed must be a bounded user/assistant message")
    if sum(len(s["turns"]) for s in scenarios) > retrieval.MAX_CASES:
        raise ValueError("too many turns")
    return data, answers.sha256(raw)


def clear_snapshot_history(db) -> dict:
    """Call only inside writable_database_snapshot; never copies live text out."""
    from app.models.entities import ChatMessage, ChatSession
    from sqlalchemy import delete
    counts = snapshot_chat_counts(db)
    db.execute(delete(ChatMessage))
    db.execute(delete(ChatSession))
    db.commit()
    db.expunge_all()
    return counts


def snapshot_chat_counts(db) -> dict:
    from sqlalchemy import func, select
    from app.models.entities import ChatMessage, ChatSession
    return {"chat_sessions": db.scalar(select(func.count()).select_from(ChatSession)),
            "chat_messages": db.scalar(select(func.count()).select_from(ChatMessage))}


def seed_session(db, scenario: dict) -> int | None:
    seeds = scenario.get("seed_history", [])
    if not seeds:
        return None
    from app.models.entities import ChatMessage, ChatSession
    session = ChatSession(title="isolated scenario " + scenario["id"],
                          knowledge_base_id=scenario["turns"][0]["scope"]["knowledge_base_id"])
    db.add(session)
    db.flush()
    for item in seeds:
        db.add(ChatMessage(session_id=session.id, role=item["role"], content=item["content"],
                           route_type="direct", retrieved_video_ids="[]", retrieved_chunk_ids="[]"))
    db.commit()
    return session.id


def execute_scoped_case(db, case: dict, session_id: int | None, service=None) -> dict:
    """Reuse answer recorder while changing only the session_id it supplies."""
    from app.services.rag_service import RagService
    service = service or RagService()
    original = service.answer_stream
    def with_session(*args, **kwargs):
        kwargs["session_id"] = session_id
        return original(*args, **kwargs)
    with patch.object(service, "answer_stream", side_effect=with_session):
        row = answers.execute_case(db, case, service=service)
    row["input_session_id"] = session_id
    return row


def execute_scope_rejection(db, case: dict, session_id: int | None, service=None) -> dict:
    from app.services.rag_service import RagService, llm_client, embedding_client
    service = service or RagService()
    before = snapshot_chat_counts(db)
    calls = []
    def forbid(*args, **kwargs):
        calls.append(True)
        raise AssertionError("model calls forbidden during scope rejection")
    started = time.perf_counter()
    events = []
    error_type = None
    try:
        with patch.object(llm_client, "chat", side_effect=forbid), \
                patch.object(llm_client, "stream_chat", side_effect=forbid), \
                patch.object(embedding_client, "embed_text", side_effect=forbid):
            events = [{"event": event, "data": payload} for event, payload in service.answer_stream(
                db=db, query=case["question"], session_id=session_id,
                knowledge_base_id=case["scope"]["knowledge_base_id"])]
    except Exception as error:
        db.rollback()
        error_type = type(error).__name__
    after = snapshot_chat_counts(db)
    passed = (session_id is not None and not calls and before == after and len(events) == 1
              and events[0]["event"] == "error"
              and "另一个知识库" in events[0]["data"].get("message", ""))
    row = {key: case[key] for key in ("id", "question", "scope", "category", "answerable")}
    row.update(status="expected_scope_error_passed" if passed else "error", answer="", raw_answer="",
               input_session_id=session_id, isolated_session_id=session_id, events=events,
               model_calls=[], forbidden_model_call_count=len(calls),
               isolated_counts_before=before, isolated_counts_after=after,
               actual_context_results=[], first_delta_ms=None,
               latency_ms=round((time.perf_counter()-started)*1000, 3))
    if not passed:
        row["error_type"] = error_type or "ScopeIsolationFailure"
    return answers._redactor()(row)


def run_scenarios(db, data: dict, *, on_row=None, service_factory=None) -> list[dict]:
    from app.services.rag_service import RagService
    rows = []
    for scenario in data["scenarios"]:
        sid = seed_session(db, scenario)
        blocked = False
        service = (service_factory or RagService)()
        for turn_index, turn in enumerate(scenario["turns"]):
            action = turn.get("session_action", "continue")
            case = strict_case(scenario, turn)
            if action == "new_session":
                sid, blocked = None, False
            if blocked:
                row = {key: case[key] for key in ("id", "question", "scope", "category", "answerable")}
                row.update(status="skipped_dependency_error", input_session_id=sid,
                           reason="Earlier turn failed; dependent history is unavailable.")
            elif action == "expect_scope_error":
                row = execute_scope_rejection(db, case, sid, service)
                blocked = row["status"] != "expected_scope_error_passed"
            else:
                row = execute_scoped_case(db, case, sid, service)
                if row["status"] == "completed" and row.get("isolated_session_id") is not None:
                    sid = row["isolated_session_id"]
                else:
                    blocked = True
            row.update(scenario_id=scenario["id"], turn_index=turn_index+1,
                       session_action=action, expected_behavior=turn.get("expected_behavior", "answer"))
            rows.append(row)
            if on_row:
                on_row(row)
    return rows


def update_summary(report):
    rows = report["cases"]
    report["summary"] = {"scenario_count": len(report["scenario_ids"]), "turn_count": len(rows),
                         "status_counts": dict(Counter(r["status"] for r in rows)),
                         "actual_context_metrics": retrieval.aggregate_actual_context(rows),
                         "quality_grading": "pending_fixed_rubric_review"}
    report["updated_at"] = datetime.now(timezone.utc).isoformat()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--run", action="store_true")
    args = parser.parse_args(argv)
    dataset, output = args.dataset.resolve(), args.output.resolve()
    if output == dataset or output.suffix.lower() != ".json" or output.exists():
        parser.error("output must be a new separate .json file; automatic reruns/resume are disabled")
    report, settings, persistable = None, None, False
    status = 1
    try:
        os.chdir(BACKEND_DIR)
        logging.disable(logging.CRITICAL)
        from app.core.config import settings
        data, digest = load_scenarios(dataset)
        index_path = Path(settings.chroma_persist_dir).resolve()
        if output.is_relative_to(index_path) or output == answers.database_path(settings.database_url):
            raise ValueError("output must not target live stores")
        report = {"schema_version": 1, "mode": "conversation_generation" if args.run else "validation_only",
                  "created_at": datetime.now(timezone.utc).isoformat(), "dataset_sha256": digest,
                  "scenario_ids": [s["id"] for s in data["scenarios"]], "cases": [],
                  "limitations": ["Gold and rubrics never enter model prompts.",
                                  "New isolated session per scenario; failures are retained, dependent turns skipped.",
                                  "Intentional false assistant seeds exist only in disposable SQL copy.",
                                  "No accuracy score is assigned automatically."]}
        report["live_chat_counts_before"] = answers.live_chat_counts(settings.database_url)
        with answers.writable_database_snapshot(settings.database_url) as db:
            corpus = retrieval.corpus_snapshot(db)
            flat = {"schema_version": 1, "cases": [strict_case(s, t) for s in data["scenarios"] for t in s["turns"]]}
            validated = answers.validate_dataset(db, flat)
            report.update(corpus=corpus, cases=validated,
                          fingerprint={"dataset_sha256": digest, "corpus_sha256": corpus["sha256"],
                                       "retrieval_configuration": retrieval.config_snapshot(),
                                       "answer_configuration": answers.answer_configuration(),
                                       "source_code_sha256": answers.source_fingerprints() | {
                                           "scripts/evaluate_conversations.py": answers.sha256(Path(__file__).read_bytes())},
                                       "index": {"status": "not_loaded_in_dry_run"}})
            persistable = True
            if args.run:
                report["isolated_original_history_counts_cleared"] = clear_snapshot_history(db)
                with retrieval.snapshot_chroma(settings.chroma_persist_dir) as chroma:
                    report["fingerprint"]["index"] = answers.index_fingerprint(chroma)
                    update_summary(report)
                    answers.atomic_json(output, report)
                    positions = {row["id"]: i for i, row in enumerate(report["cases"])}
                    def saved(row):
                        report["cases"][positions[row["id"]]] = row
                        update_summary(report)
                        answers.atomic_json(output, report)
                        print(json.dumps({"turn": row["id"], "status": row["status"]}), flush=True)
                    with patch("app.services.rag_service.get_chroma_service", return_value=chroma):
                        run_scenarios(db, data, on_row=saved)
                report["isolated_final_chat_counts"] = snapshot_chat_counts(db)
        status = 2 if any(r["status"] in ("error", "skipped_dependency_error") for r in report["cases"]) else 0
    except Exception as error:
        if report is not None:
            report["runner_error_type"] = type(error).__name__
        print(f"Conversation evaluation stopped ({type(error).__name__}); upstream text omitted.", file=sys.stderr)
    finally:
        if persistable and report is not None and settings is not None:
            try:
                report["live_chat_counts_after"] = answers.live_chat_counts(settings.database_url)
                report["live_history_count_change"] = {key: report["live_chat_counts_after"][key] - value
                                                        for key, value in report["live_chat_counts_before"].items()}
            except Exception as error:
                report["finalization_error_type"] = type(error).__name__
                status = 1
            report["runner_status"] = "completed" if status in (0, 2) and "runner_error_type" not in report else "failed"
            update_summary(report)
            try:
                answers.atomic_json(output, report)
            except Exception as error:
                print(f"Report save failed ({type(error).__name__}).", file=sys.stderr)
                status = 1
    return status


if __name__ == '__main__':
    raise SystemExit(main())
