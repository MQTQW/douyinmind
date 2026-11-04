"""Repeatable CPU-only lexical/dense fusion baseline over synthetic chunks.

PowerShell, from the project directory (no services or model keys needed)::

    $env:PYTHONHASHSEED='0'
    .\\backend\\.venv\\Scripts\\python.exe backend/scripts/benchmark_retrieval.py

The default matrix is 100/500/2000/10000 chunks, fixed seed, one warmup and five
repetitions per fixed query. No private DB, network, Chroma I/O or model is read.
Memory probes run separately from latency samples and include Python allocations
only. Output is immutable JSON; it does not represent end-to-end RAG accuracy.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import secrets
import sys

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.services.retrieval_benchmark import DEFAULT_SEED, DEFAULT_SIZES, benchmark_report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sizes", type=int, nargs="+", default=list(DEFAULT_SIZES))
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    parser.add_argument("--chunk-chars", type=int, default=384)
    parser.add_argument("--warmups", type=int, default=1)
    parser.add_argument("--repeats", type=int, default=5)
    parser.add_argument("--skip-memory", action="store_true")
    parser.add_argument("--label", default="baseline")
    parser.add_argument("--output-dir", type=Path,
        default=BACKEND_DIR.parent.parent / "douyinmind-12h-20261104" / "benchmarks")
    args = parser.parse_args()
    if re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", args.label) is None:
        parser.error("label must use 1..64 lowercase letters, digits, '_' or '-'")

    def progress(row):
        print(json.dumps({"chunks_completed": row["chunk_count"],
                          "correctness_passed": row["correctness_passed"]}), file=sys.stderr, flush=True)

    started = datetime.now(timezone.utc).isoformat()
    report = benchmark_report(args.sizes, seed=args.seed, chunk_chars=args.chunk_chars,
        warmups=args.warmups, repeats=args.repeats, measure_memory=not args.skip_memory,
        script_path=Path(__file__), on_size=progress)
    report.update(started_at_utc=started, finished_at_utc=datetime.now(timezone.utc).isoformat())
    args.output_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = args.output_dir / f"{args.label}-{stamp}-{secrets.token_hex(4)}.json"
    with path.open("x", encoding="utf-8") as stream:
        json.dump(report, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write("\n")
    print(json.dumps({"report": str(path.resolve()), "correctness_passed": report["correctness_passed"],
                      "source_stable": report["source_stable"]}, ensure_ascii=False))
    return 0 if report["correctness_passed"] and report["source_stable"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
