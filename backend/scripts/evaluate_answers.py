"""Generate one answer per retrieval-exam question through the real streaming path.

Validation is the default and never calls a model.  ``--run`` calls the existing
retriever, compression model and final answer model unchanged.  All chat writes
and Chroma activity take place in disposable copies, never in the live stores.
This captures evidence for human review; it deliberately assigns no quality score.
"""
from __future__ import annotations

import argparse
from collections import Counter
from contextlib import closing, contextmanager
from datetime import datetime, timezone
import hashlib
import inspect
import json
import logging
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import time
from unittest.mock import patch

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from scripts import evaluate_retrieval as retrieval


def sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def atomic_json(path: Path, value: dict) -> None:
    """A reviewer may read the last complete result while the exam continues."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    try:
        temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
        for attempt in range(3):
            try:
                os.replace(temporary, path)
                break
            except PermissionError:
                # Windows readers/virus scanners may briefly hold a report.
                # Retry only the local atomic rename, never any model request.
                if attempt == 2:
                    raise
                time.sleep(.05 * (attempt + 1))
    finally:
        temporary.unlink(missing_ok=True)


def database_path(database_url: str) -> Path:
    from sqlalchemy.engine import make_url
    url = make_url(database_url)
    if url.drivername != "sqlite" or not url.database or url.database == ":memory:":
        raise ValueError("an existing SQLite file is required")
    path = Path(url.database)
    return (path if path.is_absolute() else BACKEND_DIR / path).resolve(strict=True)


@contextmanager
def writable_database_snapshot(database_url: str):
    """Close the read-only live handle before yielding a writable temporary DB."""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session
    source_path = database_path(database_url)
    with tempfile.TemporaryDirectory(prefix="douyinmind-answer-sql-") as temporary:
        destination = Path(temporary) / "exam.sqlite3"
        with closing(sqlite3.connect(source_path.as_uri() + "?mode=ro", uri=True)) as source:
            with closing(sqlite3.connect(destination)) as target:
                source.backup(target)
        engine = create_engine("sqlite:///" + destination.as_posix())
        try:
            with Session(engine) as db:
                yield db
        finally:
            engine.dispose()


def live_chat_counts(database_url: str) -> dict:
    """Counts only: never export any existing user conversations."""
    with closing(sqlite3.connect(database_path(database_url).as_uri() + "?mode=ro", uri=True)) as db:
        return {table: db.execute("SELECT COUNT(*) FROM " + table).fetchone()[0]
                for table in ("chat_sessions", "chat_messages")}


def answer_configuration() -> dict:
    from app.core.config import settings
    from app.services.llm_service import LLMClient
    def defaults(method):
        signature = inspect.signature(method)
        return {name: signature.parameters[name].default for name in ("temperature", "max_tokens", "timeout")}
    return {"llm_model": settings.llm_model,
            "llm_thinking_enabled": settings.llm_thinking_enabled,
            "thinking_field_applies": settings.llm_base_url.startswith("https://api.deepseek.com"),
            "llm_base_url_sha256": sha256(settings.llm_base_url.encode()),
            "embedding_base_url_sha256": sha256(settings.embedding_base_url.encode()),
            "final_answer_defaults": defaults(LLMClient.stream_chat),
            "compression_parameters": {"temperature": 0.1, "max_tokens": 200,
                                       "timeout": inspect.signature(LLMClient.chat).parameters["timeout"].default},
            "session_id": None, "collection_id": None}


def source_fingerprints() -> dict:
    names = ("app/services/rag_service.py", "app/services/llm_service.py",
             "app/services/chroma_service.py", "app/services/knowledge_base_service.py",
             "app/services/retrieval_fusion.py", "app/services/retrieval_context.py",
             "app/services/query_intent.py", "app/services/conversation_query.py",
             "app/services/retrieval_errors.py",
             "app/services/chat_errors.py", "app/services/retrieval_observation.py",
             "app/services/text_processing.py",
             "app/models/entities.py", "app/core/config.py", "scripts/evaluate_retrieval.py",
             "scripts/evaluate_answers.py")
    return {name: sha256((BACKEND_DIR / name).read_bytes()) for name in names}


def index_fingerprint(chroma) -> dict:
    """Hash the actual copied records including vectors without exporting them."""
    records = chroma._collection.get(include=["documents", "metadatas", "embeddings"])
    embeddings = records.get("embeddings")
    if hasattr(embeddings, "tolist"):
        embeddings = embeddings.tolist()
    rows = sorted((identifier, records["documents"][i], records["metadatas"][i],
                   embeddings[i] if embeddings is not None else None)
                  for i, identifier in enumerate(records["ids"]))
    encoded = json.dumps(rows, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    return {"sha256": sha256(encoded), "chunk_count": len(rows)}


def validate_reference(reference: dict, data: dict, digest: str, corpus: dict, configuration: dict) -> None:
    if reference.get("mode") != "retrieval_only" or reference.get("dataset_sha256") != digest:
        raise ValueError("reference must be the same completed retrieval exam")
    if reference.get("corpus") != corpus or reference.get("configuration") != configuration:
        raise ValueError("corpus or retrieval configuration differs from reference")
    expected = {case["id"]: case for case in data["cases"]}
    rows = reference.get("cases", [])
    if len(rows) != len(expected) or {row.get("id") for row in rows} != set(expected):
        raise ValueError("reference cases differ from dataset")
    for row in rows:
        case = expected[row["id"]]
        if row.get("status") != "evaluated" or any(row.get(key) != case[key] for key in ("question", "scope", "answerable")):
            raise ValueError("reference contains an incomplete or changed case")


def validate_dataset(db, data: dict) -> list[dict]:
    report = retrieval.evaluate_dataset(db, data, run=False)
    if report["summary"]["status_counts"].get("invalid"):
        raise ValueError("all cases must validate before any model call")
    return report["cases"]


def _redactor():
    from app.core.config import settings
    secrets = [getattr(settings, key, "") for key in
               ("deepseek_api_key", "dashscope_api_key", "qwen_omni_api_key")]
    def redact(value):
        if isinstance(value, str):
            for secret in secrets:
                if secret:
                    value = value.replace(secret, "[REDACTED]")
            return value
        if isinstance(value, list):
            return [redact(item) for item in value]
        if isinstance(value, tuple):
            return [redact(item) for item in value]
        if isinstance(value, dict):
            return {key: redact(item) for key, item in value.items()}
        return value
    return redact


def execute_case(db, case: dict, service=None) -> dict:
    """Only question and scope enter production; gold evidence stays outside."""
    from app.services.rag_service import RagService, llm_client
    service = service or RagService()
    redact = _redactor()
    row = {key: case[key] for key in ("id", "question", "scope", "category", "answerable")}
    row.update(status="running", answer="", raw_answer="", production_route=None,
               sources=[], trace=None, dense_hits=[], actual_context_results=[],
               events=[], model_calls=[], first_delta_ms=None)
    original_chat, original_stream = llm_client.chat, llm_client.stream_chat
    original_dense, original_route = service._dense_retrieve, service._route
    original_context_hits = service._context_hits
    actual_hits = []
    def record_call(kind, method, args, kwargs):
        bound = inspect.signature(method).bind(*args, **kwargs)
        bound.apply_defaults()
        item = {"kind": kind, **{key: bound.arguments[key] for key in
                ("system_prompt", "user_prompt", "temperature", "max_tokens", "timeout")},
                "status": "running", "output": ""}
        row["model_calls"].append(item)
        return item
    def chat(*args, **kwargs):
        item = record_call("compression", original_chat, args, kwargs)
        try:
            output = original_chat(*args, **kwargs)
            item.update(status="completed", output=output)
            return output
        except Exception as error:
            item.update(status="error", error_type=type(error).__name__)
            raise
    def stream(*args, **kwargs):
        item = record_call("answer", original_stream, args, kwargs)
        parts = []
        try:
            for delta in original_stream(*args, **kwargs):
                parts.append(delta)
                yield delta
            item.update(status="completed", output="".join(parts))
        except Exception as error:
            item.update(status="error", output="".join(parts), error_type=type(error).__name__)
            raise
        finally:
            row["raw_answer"] = "".join(parts)
    def dense(*args, **kwargs):
        hits = original_dense(*args, **kwargs)
        row["dense_hits"] = hits
        return hits
    def route(*args, **kwargs):
        result = original_route(*args, **kwargs)
        row["production_route"] = result
        return result
    def context_hits(*args, **kwargs):
        nonlocal actual_hits
        selected = original_context_hits(*args, **kwargs)
        # Selection may be called repeatedly during prompt construction. Keep
        # the final call, including an empty selection; never alter its input.
        actual_hits = [dict(hit) for hit in selected]
        return selected
    started = time.perf_counter()
    error_event = False
    meta = None
    done = False
    try:
        with patch.object(llm_client, "chat", side_effect=chat), patch.object(llm_client, "stream_chat", side_effect=stream), \
                patch.object(service, "_dense_retrieve", side_effect=dense), patch.object(service, "_route", side_effect=route), \
                patch.object(service, "_context_hits", side_effect=context_hits):
            for event, payload in service.answer_stream(db=db, query=case["question"], session_id=None,
                                                       knowledge_base_id=case["scope"]["knowledge_base_id"]):
                if event == "delta" and payload.get("text") and row["first_delta_ms"] is None:
                    row["first_delta_ms"] = round((time.perf_counter() - started) * 1000, 3)
                if event == "error":
                    error_event = True
                    # An upstream exception may contain a URL or credential.
                    payload = {"message": "Production stream emitted an error; upstream text omitted."}
                if event == "meta":
                    meta = payload
                if event == "done":
                    done = payload.get("ok") is True
                row["events"].append({"event": event, "data": payload})
        if meta is not None:
            row.update(answer=meta.get("answer", ""), sources=meta.get("sources", []), trace=meta.get("trace"),
                       production_route=meta.get("route_type"), isolated_session_id=meta.get("session_id"))
        completed = (meta is not None and done and not error_event and bool(row["answer"].strip())
                     and len([item for item in row["model_calls"] if item["kind"] == "answer"]) == 1
                     and all(item["status"] == "completed" for item in row["model_calls"] if item["kind"] == "answer"))
        row["status"] = "completed" if completed else "error"
        if not completed:
            row["error_type"] = "StreamError" if error_event else "IncompleteStream"
    except Exception as error:
        db.rollback()
        row.update(status="error", error_type=type(error).__name__)
    row["latency_ms"] = round((time.perf_counter() - started) * 1000, 3)
    row["actual_context_results"], row["actual_context_metrics"] = retrieval.score_actual_context(
        case, actual_hits, service._format_context_blocks(actual_hits))
    row["compression_error_count"] = sum(item["kind"] == "compression" and item["status"] == "error" for item in row["model_calls"])
    return redact(row)


def resume_rows(previous: dict, fingerprint: dict, validated_rows: list[dict]) -> list[dict]:
    if previous.get("mode") != "answer_generation" or previous.get("fingerprint") != fingerprint:
        raise ValueError("resume requires identical dataset, reference, corpus, configuration and code")
    rows = previous.get("cases")
    if not isinstance(rows, list) or [row.get("id") for row in rows] != [row["id"] for row in validated_rows]:
        raise ValueError("resume case order differs")
    for old, new in zip(rows, validated_rows):
        if any(old.get(key) != new.get(key) for key in ("question", "scope", "answerable", "category")):
            raise ValueError("resume case contents differ")
        if old.get("status") not in ("validated", "completed", "error"):
            raise ValueError("an interrupted case cannot be regenerated automatically")
    return rows


def update_summary(report: dict) -> None:
    report["summary"] = {"case_count": len(report["cases"]),
                         "status_counts": dict(Counter(row["status"] for row in report["cases"])),
                         "actual_context_metrics": retrieval.aggregate_actual_context(report["cases"]),
                         "quality_grading": "pending_human_review"}
    report["updated_at"] = datetime.now(timezone.utc).isoformat()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dataset", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--reference", required=True, type=Path)
    parser.add_argument("--run", action="store_true", help="Call production models once per question using isolated stores")
    parser.add_argument("--resume", action="store_true", help="Keep completed/error rows; run only untouched validated rows")
    args = parser.parse_args(argv)
    dataset_path, output_path, reference_path = args.dataset.resolve(), args.output.resolve(), args.reference.resolve()
    if output_path in (dataset_path, reference_path) or output_path.suffix.lower() != ".json":
        parser.error("output must be a separate .json artifact")
    if output_path.exists() and not args.resume:
        parser.error("output already exists; choose a new filename or use --resume")
    if args.resume and (not args.run or not output_path.is_file()):
        parser.error("--resume needs --run and an existing output")
    report = None
    settings = None
    exit_status = 1
    runner_error_type = None
    persistable_report = False
    try:
        os.chdir(BACKEND_DIR)
        # Production logger.exception can include arbitrary upstream error text.
        logging.disable(logging.CRITICAL)
        from app.core.config import settings
        data, digest = retrieval.load_dataset(dataset_path)
        reference_bytes = reference_path.read_bytes()
        reference = json.loads(reference_bytes)
        chroma_path = Path(settings.chroma_persist_dir)
        chroma_path = (chroma_path if chroma_path.is_absolute() else BACKEND_DIR / chroma_path).resolve()
        if output_path.is_relative_to(chroma_path) or output_path == database_path(settings.database_url):
            raise ValueError("output must not target live stores")
        report = {"schema_version": 1, "mode": "answer_generation" if args.run else "validation_only",
                  "created_at": datetime.now(timezone.utc).isoformat(), "dataset_sha256": digest,
                  "limitations": ["One production answer per question, with ordinary compression and postprocessing; no alternative answers are selected.",
                                  "Gold answers and annotated evidence are never passed to any model.",
                                  "This artifact records answers and prompts; accuracy and no-answer behavior require a separate human review.",
                                  "All session/message writes and Chroma activity use temporary copies; original conversations are not exported.",
                                  "Compression failures use the normal production fallback and are recorded; final stream failures are errors, never passes."]}
        report["live_chat_counts_before"] = live_chat_counts(settings.database_url)
        with writable_database_snapshot(settings.database_url) as db:
            corpus = retrieval.corpus_snapshot(db)
            configuration = retrieval.config_snapshot()
            rows = validate_dataset(db, data)
            validate_reference(reference, data, digest, corpus, configuration)
            fingerprint = {"dataset_sha256": digest, "reference_sha256": sha256(reference_bytes),
                           "corpus_sha256": corpus["sha256"], "retrieval_configuration": configuration,
                           "answer_configuration": answer_configuration(), "source_code_sha256": source_fingerprints()}
            report.update(corpus=corpus, fingerprint=fingerprint, cases=rows)
            if args.run:
                with retrieval.snapshot_chroma(settings.chroma_persist_dir) as chroma:
                    fingerprint["index"] = index_fingerprint(chroma)
                    if args.resume:
                        previous = json.loads(output_path.read_text(encoding="utf-8"))
                        report["cases"] = resume_rows(previous, fingerprint, rows)
                        report["created_at"] = previous["created_at"]
                        report["live_chat_counts_before"] = previous["live_chat_counts_before"]
                    persistable_report = True
                    update_summary(report)
                    atomic_json(output_path, report)
                    with patch("app.services.rag_service.get_chroma_service", return_value=chroma):
                        for index, case in enumerate(data["cases"]):
                            if report["cases"][index]["status"] in ("completed", "error"):
                                continue
                            report["cases"][index]["status"] = "running"
                            update_summary(report)
                            atomic_json(output_path, report)
                            report["cases"][index] = execute_case(db, case)
                            update_summary(report)
                            atomic_json(output_path, report)
                            print(json.dumps({"case": case["id"], "status": report["cases"][index]["status"],
                                              "completed": index + 1, "total": len(data["cases"])}, ensure_ascii=True), flush=True)
        persistable_report = True
        update_summary(report)
        exit_status = 2 if report["summary"]["status_counts"].get("error") else 0
    except Exception as error:
        runner_error_type = type(error).__name__
        print(f"Answer evaluation stopped ({type(error).__name__}); no upstream exception text was exported.", file=sys.stderr)
    finally:
        # A Windows temporary-index cleanup error can occur after all answers
        # were atomically saved. Complete the independent live-history audit
        # even then, preserving every case and a failing runner exit status.
        if persistable_report and report is not None and settings is not None and "cases" in report:
            if runner_error_type:
                report["runner_error_type"] = runner_error_type
            try:
                report["live_chat_counts_after"] = live_chat_counts(settings.database_url)
            except Exception as error:
                report["finalization_error_type"] = type(error).__name__
                exit_status = 1
            report["runner_status"] = "completed" if exit_status in (0, 2) and not runner_error_type else "failed"
            update_summary(report)
            try:
                atomic_json(output_path, report)
            except Exception as error:
                print(f"Answer evaluation report finalization failed ({type(error).__name__}).", file=sys.stderr)
                exit_status = 1
    if report is not None and "summary" in report:
        print(json.dumps({"mode": report["mode"], "summary": report["summary"], "output": str(output_path),
                          "runner_status": report.get("runner_status", "failed")}, ensure_ascii=True))
    return exit_status


if __name__ == "__main__":
    raise SystemExit(main())
