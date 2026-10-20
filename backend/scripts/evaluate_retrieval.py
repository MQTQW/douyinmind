"""Read-only retrieval exam. Validation is free; --run calls query embeddings only.

Run from any directory with the backend virtual environment::
    python backend/scripts/evaluate_retrieval.py --dataset exam.json --output report.json
    python backend/scripts/evaluate_retrieval.py --dataset exam.json --output report.json --run

This deliberately does not call RagService.chat, context compression, or an answer
model. The production router and dense retriever are reused without changing them.
The original SQL database is opened read-only, and Chroma searches use a temporary
copy: even Chroma client initialization cannot create collections in the live index.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from contextlib import closing, contextmanager
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import shutil
import sqlite3
import sys
import tempfile
import time
import unicodedata
from unittest.mock import patch

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

MAX_DATASET_BYTES = 10 * 1024 * 1024
MAX_CASES = 500
CASE_KEYS = {"id", "question", "scope", "answerable", "expected_route", "evidence", "expected_answer", "category"}
EVIDENCE_KEYS = {"video_id", "title", "start_seconds", "end_seconds", "quote"}


def normalize_evidence(value: str) -> str:
    """Ignore Unicode width, case, punctuation and whitespace, not actual words."""
    return "".join(
        char for char in unicodedata.normalize("NFKC", value).casefold()
        if not char.isspace() and not unicodedata.category(char).startswith("P")
    )


def _string(value, minimum: int, maximum: int) -> bool:
    return isinstance(value, str) and minimum <= len(value.strip()) <= maximum


def _number(value) -> bool:
    return type(value) in (int, float) and math.isfinite(value) and value >= 0


def structural_errors(case: object) -> list[str]:
    errors = []
    if not isinstance(case, dict):
        return ["case must be an object"]
    if set(case) != CASE_KEYS:
        errors.append("case keys must be exactly: " + ", ".join(sorted(CASE_KEYS)))
    for name, maximum in (("id", 128), ("question", 2000), ("expected_answer", 4000), ("category", 128)):
        if not _string(case.get(name), 1, maximum):
            errors.append(f"{name} must be a nonempty string of at most {maximum} characters")
    if type(case.get("answerable")) is not bool:
        errors.append("answerable must be a boolean")
    if case.get("expected_route") != "vector":
        errors.append("this exam supports expected_route=vector only")
    scope = case.get("scope")
    if not isinstance(scope, dict) or set(scope) != {"knowledge_base_id", "label"}:
        errors.append("scope must contain knowledge_base_id and label only")
    else:
        scope_id = scope["knowledge_base_id"]
        if scope_id is not None and (type(scope_id) is not int or scope_id < 1):
            errors.append("scope.knowledge_base_id must be a positive integer or null")
        if not _string(scope["label"], 1, 128):
            errors.append("scope.label must be a nonempty string of at most 128 characters")
    evidence = case.get("evidence")
    if not isinstance(evidence, list) or len(evidence) > 20:
        errors.append("evidence must be an array with at most 20 entries")
        return errors
    if case.get("answerable") is True and not evidence:
        errors.append("answerable cases require evidence")
    if case.get("answerable") is False and evidence:
        errors.append("negative cases must have empty evidence; no-answer still needs human review")
    for index, item in enumerate(evidence):
        prefix = f"evidence[{index}]"
        if not isinstance(item, dict) or set(item) != EVIDENCE_KEYS:
            errors.append(f"{prefix} has invalid keys")
            continue
        for name, minimum, maximum in (("video_id", 1, 64), ("title", 1, 512), ("quote", 4, 2000)):
            if not _string(item[name], minimum, maximum):
                errors.append(f"{prefix}.{name} must contain {minimum}..{maximum} characters")
        if isinstance(item.get("quote"), str) and len(normalize_evidence(item["quote"])) < 4:
            errors.append(f"{prefix}.quote needs at least four non-punctuation characters")
        if not _number(item["start_seconds"]) or not _number(item["end_seconds"]) or item["end_seconds"] <= item["start_seconds"]:
            errors.append(f"{prefix} needs finite times with 0 <= start_seconds < end_seconds")
    return errors


def load_dataset(path: Path) -> tuple[dict, str]:
    if path.stat().st_size > MAX_DATASET_BYTES:
        raise ValueError("dataset exceeds 10 MiB")
    raw = path.read_bytes()
    def no_duplicate_keys(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"duplicate JSON key: {key}")
            result[key] = value
        return result
    data = json.loads(raw.decode("utf-8-sig"), object_pairs_hook=no_duplicate_keys,
                      parse_constant=lambda value: (_ for _ in ()).throw(ValueError(f"non-finite JSON number: {value}")))
    if not isinstance(data, dict) or set(data) != {"schema_version", "cases"} or type(data.get("schema_version")) is not int or data["schema_version"] != 1:
        raise ValueError("dataset must contain schema_version=1 and cases only")
    if not isinstance(data["cases"], list) or not 1 <= len(data["cases"]) <= MAX_CASES:
        raise ValueError(f"dataset must contain 1..{MAX_CASES} cases")
    return data, hashlib.sha256(raw).hexdigest()


def resolve_scope(db, knowledge_base_id: int | None) -> set[str]:
    """Global is also explicit: never retrieve failed or unfinished videos."""
    from sqlalchemy import select
    from app.models.entities import KnowledgeBase, KnowledgeBaseVideo, VideoCache
    query = select(VideoCache.platform_item_id).where(VideoCache.status == "done")
    if knowledge_base_id is not None:
        if db.get(KnowledgeBase, knowledge_base_id) is None:
            raise ValueError("knowledge base does not exist")
        query = query.join(KnowledgeBaseVideo, KnowledgeBaseVideo.platform_item_id == VideoCache.platform_item_id).where(
            KnowledgeBaseVideo.knowledge_base_id == knowledge_base_id)
    return set(db.scalars(query).all())


def validate_case(db, case: object) -> tuple[list[str], set[str]]:
    errors = structural_errors(case)
    if errors:
        return errors, set()
    from sqlalchemy import select
    from app.models.entities import VideoCache
    try:
        scope_ids = resolve_scope(db, case["scope"]["knowledge_base_id"])
    except ValueError as error:
        return [str(error)], set()
    expected_ids = {item["video_id"] for item in case["evidence"]}
    rows = {row.platform_item_id: row for row in db.scalars(
        select(VideoCache).where(VideoCache.platform_item_id.in_(expected_ids))).all()} if expected_ids else {}
    for index, evidence in enumerate(case["evidence"]):
        prefix = f"evidence[{index}]"
        video = rows.get(evidence["video_id"])
        if video is None or video.status != "done":
            errors.append(f"{prefix} video does not exist or is not done")
            continue
        if video.platform_item_id not in scope_ids:
            errors.append(f"{prefix} video is outside the selected scope")
        if evidence["title"].strip() != video.title.strip():
            errors.append(f"{prefix} title differs from the current video title")
        try:
            analysis = json.loads(video.analysis_json)
            segments = analysis["transcript_segments"]
            if not isinstance(segments, list):
                raise ValueError()
        except (ValueError, TypeError, KeyError):
            errors.append(f"{prefix} has no valid transcript_segments")
            continue
        # Only segments overlapping the annotated interval count. Checking the
        # full transcript would silently accept an incorrect timestamp.
        relevant = [item for item in segments if isinstance(item, dict)
                    and _number(item.get("start_seconds")) and _number(item.get("end_seconds"))
                    and isinstance(item.get("text"), str)
                    and item["start_seconds"] < evidence["end_seconds"]
                    and item["end_seconds"] > evidence["start_seconds"]]
        relevant.sort(key=lambda item: item["start_seconds"])
        quote = normalize_evidence(evidence["quote"])
        if not relevant or quote not in normalize_evidence(" ".join(item["text"] for item in relevant)):
            errors.append(f"{prefix} quote is absent from transcript segments in the annotated interval")
        if relevant and (evidence["start_seconds"] < relevant[0]["start_seconds"] - .5
                         or evidence["end_seconds"] > max(item["end_seconds"] for item in relevant) + .5):
            errors.append(f"{prefix} interval extends beyond matching transcript segments")
    return errors, scope_ids


def score_hits(case: dict, hits: list[dict]) -> tuple[list[dict], dict]:
    """Evidence needs the expected video AND quote in chunk text, never title."""
    evidence = case["evidence"]
    expected_ids = {item["video_id"] for item in evidence}
    ranked = []
    for rank, hit in enumerate(hits[:8], 1):
        video_id = hit.get("platform_item_id", "")
        raw_body = hit.get("text", "")
        body = normalize_evidence(raw_body)
        lines = [line.strip() for line in raw_body.splitlines() if line.strip()]
        metadata_only = bool(lines) and (all(line.startswith(("视频标题：", "作者：")) for line in lines)
                                        or body == normalize_evidence(hit.get("title", "")))
        matching = [index for index, item in enumerate(evidence)
                    if not metadata_only and item["video_id"] == video_id and normalize_evidence(item["quote"]) in body]
        score = hit.get("score")
        ranked.append({"rank": rank, "video_id": video_id, "title": hit.get("title", ""),
                       "chunk_id": hit.get("chunk_id", ""), "text": hit.get("text", ""),
                       "score": score if type(score) in (int, float) and math.isfinite(score) else None,
                       "metadata_only": metadata_only, "evidence_match": bool(matching), "matched_evidence_indices": matching})
    positive = case["answerable"]
    first_rank = next((item["rank"] for item in ranked if item["evidence_match"]), None)
    matched_at_5 = {index for item in ranked[:5] for index in item["matched_evidence_indices"]}
    matched_at_8 = {index for item in ranked for index in item["matched_evidence_indices"]}
    metrics = {
        "hit_video_at_5": any(item["video_id"] in expected_ids for item in ranked[:5]) if positive else None,
        "hit_evidence_at_5": any(item["evidence_match"] for item in ranked[:5]) if positive else None,
        "hit_evidence_at_8": any(item["evidence_match"] for item in ranked) if positive else None,
        "mrr_evidence": (1 / first_rank if first_rank else 0.0) if positive else None,
        "evidence_coverage_at_5": len(matched_at_5) / len(evidence) if positive else None,
        "evidence_coverage_at_8": len(matched_at_8) / len(evidence) if positive else None,
        "all_evidence_at_5": len(matched_at_5) == len(evidence) if positive else None,
        "all_evidence_at_8": len(matched_at_8) == len(evidence) if positive else None,
        "distinct_video_at_5": len({item["video_id"] for item in ranked[:5]}),
        "returned_evidence_count": len(ranked),
        "needs_manual_no_answer_review": not positive,
    }
    return ranked, metrics


def score_actual_context(case: dict, hits: list[dict], formatted_context: str) -> tuple[list[dict], dict]:
    """Score only verbatim blocks actually selected under the production budget.

    Keep this separate from the historical top-five/eight candidate metrics.
    No gold evidence enters retrieval or production context selection.
    """
    ranked = []
    for rank, hit in enumerate(hits, 1):
        item = score_hits(case, [hit])[0][0]
        ranked.append({**item, "rank": rank})
    matched = {index for item in ranked for index in item["matched_evidence_indices"]}
    expected_ids = {item["video_id"] for item in case["evidence"]}
    positive = case["answerable"]
    return ranked, {
        "hit_video": any(item["video_id"] in expected_ids for item in ranked) if positive else None,
        "hit_evidence": bool(matched) if positive else None,
        "all_evidence": len(matched) == len(case["evidence"]) if positive else None,
        "evidence_coverage": len(matched) / len(case["evidence"]) if positive else None,
        "selected_chunk_count": len(ranked),
        "distinct_video_count": len({item["video_id"] for item in ranked}),
        "context_chars": len(formatted_context),
        "needs_manual_no_answer_review": not positive,
    }


def aggregate_actual_context(rows: list[dict]) -> dict:
    """Missing fields in historical artifacts are unmeasured, never failures."""
    def mean(values):
        values = list(values)
        return round(sum(values) / len(values), 6) if values else None
    actual_rows = [row for row in rows if row.get("status") in ("evaluated", "completed")
                   and isinstance(row.get("actual_context_metrics"), dict)]
    actual_positives = [row for row in actual_rows if row["answerable"]]
    return {
        "evaluated_case_count": len(actual_rows),
        "evaluated_answerable_count": len(actual_positives),
        **{name: mean(row["actual_context_metrics"][name] for row in actual_positives)
           for name in ("hit_video", "hit_evidence", "all_evidence", "evidence_coverage")},
        **{f"mean_{name}": mean(row["actual_context_metrics"][name] for row in actual_rows)
           for name in ("selected_chunk_count", "distinct_video_count", "context_chars")},
    }


def _aggregate(rows: list[dict]) -> dict:
    evaluated = [row for row in rows if row["status"] == "evaluated"]
    positives = [row for row in evaluated if row["answerable"]]
    vector_positives = [row for row in positives if not row["router_bypass"]]
    def mean(values):
        values = list(values)
        return round(sum(values) / len(values), 6) if values else None
    names = ("hit_video_at_5", "hit_evidence_at_5", "hit_evidence_at_8", "mrr_evidence",
             "evidence_coverage_at_5", "evidence_coverage_at_8", "all_evidence_at_5", "all_evidence_at_8")
    return {
        "case_count": len(rows), "status_counts": dict(Counter(row["status"] for row in rows)),
        "evaluated_answerable_count": len(positives),
        "negative_cases_requiring_human_review": sum(not row["answerable"] for row in evaluated),
        "router_bypass_count": sum(row["router_bypass"] for row in evaluated),
        "route_match_rate": mean(row["route_matches_expected"] for row in evaluated),
        "dense_metrics": {name: mean(row["dense_metrics"][name] for row in positives) for name in names},
        # Bypassed routes count as failures here: this is the end-to-end entry
        # into vector evidence retrieval, not a claim about answer correctness.
        "production_vector_evidence_hit_rate_at_5": mean(
            not row["router_bypass"] and row["production_metrics"]["hit_evidence_at_5"] for row in positives),
        "production_vector_only_metrics": {name: mean(row["production_metrics"][name] for row in vector_positives) for name in names},
        "actual_context_metrics": aggregate_actual_context(evaluated),
        "mean_dense_latency_ms": mean(row["dense_latency_ms"] for row in evaluated),
    }


def evaluate_dataset(db, data: dict, *, run: bool = False, retriever=None, chroma_count: int | None = None) -> dict:
    """Inject retriever in tests. All validation completes before paid requests."""
    from app.core.config import settings
    from app.services.rag_service import RagService, _normalize_query
    service = RagService()
    rows = []
    ids = Counter(case.get("id") for case in data["cases"] if isinstance(case, dict) and isinstance(case.get("id"), str))
    validated = []
    for index, case in enumerate(data["cases"]):
        errors, scope_ids = validate_case(db, case)
        if isinstance(case, dict) and isinstance(case.get("id"), str) and ids[case["id"]] > 1:
            errors.append("duplicate case id")
        row = {key: case.get(key) for key in ("id", "question", "scope", "category", "answerable", "expected_route")} if isinstance(case, dict) else {"id": f"invalid-{index + 1}"}
        row.update(status="invalid" if errors else "validated", validation_errors=errors,
                   production_route=None, router_bypass=None, route_matches_expected=None,
                   scope_done_video_count=len(scope_ids))
        rows.append(row)
        if not errors:
            validated.append((case, row, scope_ids))
    if run:
        retrieve = retriever or service._dense_retrieve
        for case, row, scope_ids in validated:
            normalized = _normalize_query(case["question"])
            route_started = time.perf_counter()
            # This matches production has_data semantics. Scope filtering below
            # intentionally tightens global retrieval to the done-only exam.
            has_data = bool(scope_ids) if case["scope"]["knowledge_base_id"] is not None else bool(chroma_count)
            route = service._route(normalized, has_data)
            row.update(production_route=route, router_bypass=route != "vector",
                       route_matches_expected=route == case["expected_route"],
                       route_latency_ms=round((time.perf_counter() - route_started) * 1000, 3))
            started = time.perf_counter()
            try:
                hits = retrieve(normalized, scope_ids=scope_ids, top_k=settings.retrieval_top_k) if scope_ids else []
                # The production method filters too; refuse a buggy/mock
                # retriever rather than scoring an accidentally leaked chunk.
                leaked = [hit for hit in hits if hit.get("platform_item_id") not in scope_ids]
                if leaked:
                    row.update(status="retrieval_error", retrieval_error="scope_violation", leaked_result_count=len(leaked))
                    continue
                ranked, metrics = score_hits(case, hits)
                context_count = min(5, max(0, settings.rag_context_count))
                candidates, production_metrics = score_hits(case, hits[:context_count])
                selected = service._context_hits(hits, scope_ids) if route == "vector" else []
                actual_results, actual_metrics = score_actual_context(
                    case, selected, service._format_context_blocks(selected))
                row.update(status="evaluated", dense_results_top8=ranked, dense_metrics=metrics,
                           production_context_candidates_top5=candidates if route == "vector" else [],
                           production_metrics=production_metrics if route == "vector" else None,
                           actual_context_results=actual_results, actual_context_metrics=actual_metrics,
                           dense_latency_ms=round((time.perf_counter() - started) * 1000, 3))
            except Exception as error:
                # Upstream exception strings can contain URLs/credentials.
                # Keep only a class name in artifacts; do not dump repr(error).
                row.update(status="retrieval_error", retrieval_error=type(error).__name__)
    by_scope = defaultdict(list)
    by_category = defaultdict(list)
    for row in rows:
        scope = row.get("scope")
        key = str(scope.get("knowledge_base_id")) if isinstance(scope, dict) else "invalid"
        by_scope[key].append(row)
        category = row.get("category")
        by_category[category if isinstance(category, str) else "invalid"].append(row)
    return {"cases": rows, "summary": _aggregate(rows),
            "by_scope": {key: _aggregate(items) for key, items in by_scope.items()},
            "by_category": {key: _aggregate(items) for key, items in by_category.items()}}


def config_snapshot() -> dict:
    from app.core.config import settings
    names = ("embedding_model", "llm_model", "retrieval_top_k", "retrieval_mmr_fetch_k", "retrieval_mmr_lambda",
             "retrieval_hybrid_enabled", "retrieval_candidate_k", "retrieval_neighbor_chars",
             "rag_context_count", "rag_prompt_max_context_chars", "chunk_size", "chunk_overlap",
             "query_embedding_timeout_seconds")
    return {name: getattr(settings, name) for name in names}


def corpus_snapshot(db) -> dict:
    """Hash the evidence corpus and library memberships, never export raw APIs."""
    from sqlalchemy import select
    from app.models.entities import KnowledgeBaseVideo, VideoCache
    videos = []
    for row in db.scalars(select(VideoCache).where(VideoCache.status == "done").order_by(VideoCache.platform_item_id)):
        try:
            analysis = json.loads(row.analysis_json)
            segments = analysis.get("transcript_segments", []) if isinstance(analysis, dict) else []
        except (ValueError, TypeError):
            segments = []
        encoded_segments = json.dumps(segments, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        videos.append({"video_id": row.platform_item_id, "title": row.title,
                       "transcript_text_sha256": hashlib.sha256((row.transcript_text or "").encode("utf-8")).hexdigest(),
                       "transcript_segments_sha256": hashlib.sha256(encoded_segments.encode("utf-8")).hexdigest(),
                       "visual_text_sha256": hashlib.sha256((row.visual_analysis_text or "").encode("utf-8")).hexdigest(),
                       "transcript_character_count": len(row.transcript_text or "")})
    memberships = [{"knowledge_base_id": knowledge_base_id, "video_id": video_id}
                   for knowledge_base_id, video_id in db.execute(select(KnowledgeBaseVideo.knowledge_base_id,
                       KnowledgeBaseVideo.platform_item_id).order_by(KnowledgeBaseVideo.knowledge_base_id,
                       KnowledgeBaseVideo.platform_item_id))]
    signature = json.dumps({"videos": videos, "memberships": memberships}, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return {"sha256": hashlib.sha256(signature.encode("utf-8")).hexdigest(),
            "done_video_count": len(videos), "videos": videos, "memberships": memberships}


@contextmanager
def readonly_database(database_url: str):
    from sqlalchemy import create_engine
    from sqlalchemy.engine import make_url
    from sqlalchemy.orm import Session
    url = make_url(database_url)
    if url.drivername != "sqlite" or not url.database or url.database == ":memory:":
        raise ValueError("this read-only CLI supports an existing SQLite file only")
    path = Path(url.database)
    if not path.is_absolute():
        path = BACKEND_DIR / path
    path = path.resolve(strict=True)
    # Release the live read connection before embeddings/network waits. In
    # SQLite DELETE journal mode a long reader would block the site's writes.
    with tempfile.TemporaryDirectory(prefix="douyinmind-exam-sql-") as temporary:
        snapshot_path = Path(temporary) / "exam.sqlite3"
        with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as source:
            with closing(sqlite3.connect(snapshot_path)) as target:
                source.backup(target)
        def connect():
            connection = sqlite3.connect(snapshot_path.as_uri() + "?mode=ro", uri=True)
            connection.execute("PRAGMA query_only=ON")
            connection.execute("BEGIN")
            return connection
        engine = create_engine("sqlite://", creator=connect)
        try:
            with Session(engine, autoflush=False) as db:
                yield db
        finally:
            engine.dispose()


@contextmanager
def snapshot_chroma(source_path: str):
    """Search only a disposable index; refuse absent/actively-changing indexes."""
    from app.services.chroma_service import ChromaService, COLLECTION_NAME
    import chromadb
    from chromadb.config import Settings as ChromaSettings
    source = Path(source_path)
    if not source.is_absolute():
        source = BACKEND_DIR / source
    source = source.resolve(strict=True)
    if not (source / "chroma.sqlite3").is_file():
        raise ValueError("existing Chroma index not found; no new collection was created")
    def manifest():
        result = {}
        for path in source.rglob("*"):
            if path.is_symlink():
                raise ValueError("index snapshot refuses symbolic links")
            if path.is_file():
                stat = path.stat()
                result[str(path.relative_to(source))] = (stat.st_size, stat.st_mtime_ns)
                if len(result) > 10000 or sum(item[0] for item in result.values()) > 2 * 1024 ** 3:
                    raise ValueError("index snapshot exceeds the 2 GiB / 10000 file safety limit")
        return result
    before = manifest()
    with tempfile.TemporaryDirectory(prefix="douyinmind-retrieval-") as temporary:
        destination = Path(temporary)
        for name in before:
            if name in {"chroma.sqlite3", "chroma.sqlite3-wal", "chroma.sqlite3-shm"}:
                continue
            target = destination / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source / name, target)
        with closing(sqlite3.connect((source / "chroma.sqlite3").as_uri() + "?mode=ro", uri=True)) as source_db:
            with closing(sqlite3.connect(destination / "chroma.sqlite3")) as target_db:
                source_db.backup(target_db)
        if before != manifest():
            raise ValueError("index changed during snapshot; wait for ingestion to finish and retry")
        client = chromadb.PersistentClient(path=str(destination), settings=ChromaSettings(anonymized_telemetry=False))
        try:
            service = ChromaService.__new__(ChromaService)
            service._client = client
            service._collection = client.get_collection(COLLECTION_NAME)
            yield service
        finally:
            # Chroma 1.x exposes no public close method. Stop the isolated
            # system before Windows removes its temporary database and HNSW files.
            client._system.stop()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dataset", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--run", action="store_true", help="Call query embeddings and search an isolated Chroma snapshot; never call answer models")
    args = parser.parse_args(argv)
    dataset_path, output_path = args.dataset.resolve(), args.output.resolve()
    if dataset_path == output_path:
        parser.error("--output must not overwrite --dataset")
    if output_path.suffix.lower() != ".json":
        parser.error("--output must be a .json report")
    try:
        data, digest = load_dataset(dataset_path)
        # Settings resolves backend/.env regardless of the caller's directory.
        import os
        os.chdir(BACKEND_DIR)
        from app.core.config import settings
        chroma_path = Path(settings.chroma_persist_dir)
        chroma_path = (chroma_path if chroma_path.is_absolute() else BACKEND_DIR / chroma_path).resolve()
        if output_path.is_relative_to(chroma_path):
            parser.error("--output must be outside the live Chroma directory")
        report = {"schema_version": 1, "generated_at": datetime.now(timezone.utc).isoformat(),
                  "mode": "retrieval_only" if args.run else "validation_only", "dataset_sha256": digest,
                  "configuration": config_snapshot(),
                  "limitations": [
                      "No answer model or compression model is called; answer quality is not evaluated.",
                      "Historical top-five/eight candidate metrics are retained separately from budget-selected actual_context_metrics.",
                      "Actual context uses production scope filtering, text selection, deduplication and formatted character budget; no answer model is called.",
                      "Global retrieval is restricted to current done videos; production global scope otherwise permits any indexed video.",
                      "Negative cases require human no-answer review; retrieval has no relevance cutoff and is not an automatic answerability decision.",
                      "MRR uses the first matching evidence among the recorded first eight results; any-one hits, all-evidence hits, and multi-evidence coverage are reported separately.",
                  ]}
        with readonly_database(settings.database_url) as db:
            report["corpus"] = corpus_snapshot(db)
            if args.run:
                with snapshot_chroma(settings.chroma_persist_dir) as chroma:
                    with patch("app.services.rag_service.get_chroma_service", return_value=chroma):
                        report.update(evaluate_dataset(db, data, run=True, chroma_count=chroma.count()))
            else:
                report.update(evaluate_dataset(db, data))
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
        print(json.dumps({"mode": report["mode"], "summary": report["summary"], "output": str(output_path)}, ensure_ascii=True))
        statuses = report["summary"]["status_counts"]
        return 2 if statuses.get("invalid", 0) or statuses.get("retrieval_error", 0) else 0
    except Exception as error:
        # Never render arbitrary upstream exception text or local secrets.
        print(f"Evaluation stopped ({type(error).__name__}); check dataset, existing local stores, and service configuration.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
