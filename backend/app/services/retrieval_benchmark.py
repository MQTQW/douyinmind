"""Synthetic CPU-only probes of the production lexical/dense fusion function.

No settings, SQL, Chroma, provider client or private corpus is imported. Dense
inputs are deliberately synthetic rank lists, never actual embedding results.
This measures algorithm cost and controlled fixture expectations, not accuracy
on a user's library or the latency of an end-to-end answer.
"""
from __future__ import annotations

from collections import Counter
import hashlib
import json
import math
import os
import itertools
from pathlib import Path
import random
import statistics
import struct
import sys
import time
import tracemalloc
from types import ModuleType


DEFAULT_SIZES = (100, 500, 2000, 10000)
DEFAULT_SEED = 20261104
FUSION_PATH = Path(__file__).with_name("retrieval_fusion.py")
_MODULE_IDS = itertools.count()
_ANCHORS = (
    ("合成Python环境", "Python 3.12 的虚拟环境使用 python -m venv 创建；激活后安装的依赖与其他项目隔离。"),
    ("合成SQLite并发", "SQLite WAL 模式把新写入放进预写日志，读事务读取快照；写事务仍须串行协调，不能同时无限写入。"),
    ("合成咖啡萃取", "咖啡萃取的水温和研磨度共同影响风味；偏苦时分别检查水温是否过高、研磨是否过细。"),
    ("合成跑步心率", "跑步训练需要区分心率区间，低强度阶段维持轻松呼吸；高强度训练不能占满每一天。"),
    ("合成休息恢复", "休息恢复要考虑睡眠与训练间隔；高强度跑步后留出恢复日，再根据实际疲劳调整下一次安排。"),
    ("合成Git冲突", "Git rebase 遇到冲突时先修正冲突文件，再执行 git add，最后运行 git rebase --continue。"),
)
_NOISE = (
    "材料按主题分类，先回看原话，再核对示例，保存操作前后的观察记录。",
    "这一段介绍植物观察，叶片变化与光照相关，浇水应依据土壤实际状态。",
    "制作模型时先测量尺寸，再核对连接位置，装配完成后检查固定是否牢靠。",
    "讨论阅读方法时保留原有段落，不把随手笔记当作原文，也不省略重要条件。",
    "日常摄影需要观察画面边缘与背景，让主体和周围物体形成清楚的空间关系。",
    "课程安排以已经完成的练习为基础，每次检查一个步骤，并保留失败的具体位置。",
    "整理桌面时让常用物品靠近使用位置，分类完成后检查标签与实际物品一致。",
    "这段合成资料用于本机算法测量，不包含任何实际用户、账号、平台素材或服务调用。",
)


def fingerprint(value):
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _identity(hit):
    return str(hit.get("platform_item_id", "")), str(hit.get("chunk_id", ""))


def load_fusion_module():
    """Load an independent module, so profiling never patches a live service."""
    module = ModuleType(f"douyinmind_cpu_benchmark_fusion_{next(_MODULE_IDS)}")
    module.__file__ = str(FUSION_PATH)
    source = FUSION_PATH.read_text(encoding="utf-8")
    # Dataclasses consult sys.modules when resolving annotations. Register a
    # unique clone, never the live app module, and let the benchmark unload it.
    sys.modules[module.__name__] = module
    try:
        exec(compile(source, str(FUSION_PATH), "exec"), module.__dict__)
    except BaseException:
        sys.modules.pop(module.__name__, None)
        raise
    return module


def source_fingerprints(*, script_path=None):
    files = {"retrieval_fusion.py": FUSION_PATH, "retrieval_benchmark.py": Path(__file__)}
    prepared = FUSION_PATH.with_name("prepared_lexical.py")
    if prepared.is_file():
        files["prepared_lexical.py"] = prepared
    if script_path is not None:
        files["benchmark_retrieval.py"] = Path(script_path)
    return {name: hashlib.sha256(path.read_bytes()).hexdigest() for name, path in sorted(files.items())}


def environment():
    # Avoid processor/WMI probes, external commands and environment dumps.
    return {"python_version": sys.version, "implementation": sys.implementation.name,
            "platform": sys.platform, "pointer_bits": struct.calcsize("P") * 8,
            "logical_cpu_count": os.cpu_count(), "python_hash_seed": os.environ.get("PYTHONHASHSEED", "random"),
            "wall_clock_resolution_seconds": time.get_clock_info("perf_counter").resolution,
            "cpu_clock_resolution_seconds": time.get_clock_info("process_time").resolution}


def build_dataset(size, *, seed=DEFAULT_SEED, chunk_chars=384, dense_count=32):
    if (type(size) is not int or not 16 <= size <= 100000
            or type(seed) is not int or type(chunk_chars) is not int or not 128 <= chunk_chars <= 2000
            or type(dense_count) is not int or not 1 <= dense_count <= 128):
        raise ValueError("invalid_retrieval_benchmark_dataset")
    rng = random.Random(seed)
    documents = []
    for index in range(size):
        body = f"[00:{index % 60:02d}] "
        if index < len(_ANCHORS):
            title, anchor = _ANCHORS[index]
            body += anchor
        else:
            title = f"合成原文-{index:06d}"
        while len(body) < chunk_chars:
            body += rng.choice(_NOISE)
        documents.append({"platform_item_id": f"799000000000{index:07d}", "chunk_id": f"synthetic-{index:06d}",
                          "title": title, "text": body[:chunk_chars], "source": "audio",
                          "start_ms": index * 1000, "end_ms": (index + 1) * 1000})
    # Metadata is part of the supplied scope but must never count as raw text.
    documents.append({"platform_item_id": "7998888888888888888", "chunk_id": "synthetic-metadata-only",
        "title": "合成目录资料", "text": "视频标题：合成目录资料\n作者：合成作者", "metadata_only": True})
    scenarios = (
        ("python_environment", "Python 3.12 的虚拟环境如何隔离依赖？", [0], "lexical_recovery"),
        ("sqlite_wal", "SQLite WAL 模式如何处理读写并发？", [1], "lexical_recovery"),
        ("coffee", "怎么调整咖啡萃取的水温与研磨度？", [2], "lexical_recovery"),
        ("running_facets", "跑步训练中，心率区间和休息恢复怎么安排？", [3, 4], "multiple_fixture_evidence"),
        ("dense_fixture_only", "latent_concept_fixture_86531", [5], "synthetic_dense_only"),
        ("no_match", "zzzxqv_without_any_known_term_927461", [], "empty_lexical_scope"),
    )
    cases = []
    for number, (key, query, positions, mode) in enumerate(scenarios):
        expected = [_identity(documents[index]) for index in positions]
        if mode == "empty_lexical_scope":
            dense = []
        else:
            choices = [index for index in range(len(_ANCHORS), size)]
            random.Random(seed + number + 1).shuffle(choices)
            candidates = [documents[index] for index in choices[:dense_count]]
            if mode == "synthetic_dense_only":
                candidates.insert(0, documents[positions[0]])
            dense = [{**hit, "score": round(.9 - index * .003, 6)} for index, hit in enumerate(candidates[:dense_count])]
            dense.insert(0, {"platform_item_id": "7999999999999999999", "chunk_id": "outside-authorized-scope",
                "title": "范围外合成干扰项", "text": query + " 范围外内容绝不能进入结果。", "score": 1.0})
            dense.append({**documents[-1], "score": 1.0})
        cases.append({"id": key, "query": query, "expected_ids": expected,
                      "expect_empty": mode == "empty_lexical_scope", "mode": mode, "dense_hits": dense})
    return documents, cases


def assess_result(result, case, documents, *, top_k=8):
    authoritative = {_identity(hit): hit for hit in documents if not hit.get("metadata_only")}
    identities = [_identity(hit) for hit in result]
    missing = sorted(set(map(tuple, case["expected_ids"])) - set(identities))
    out_of_scope = [identity for identity in identities if identity not in authoritative]
    originals_unchanged = all(identity in authoritative and all(hit.get(key) == value
        for key, value in authoritative[identity].items()) for identity, hit in zip(identities, result))
    passed = (not missing and not out_of_scope and originals_unchanged and len(identities) == len(set(identities))
              and len(identities) <= top_k and (not case["expect_empty"] or not result))
    return {"passed": passed, "missing_fixture_ids": missing, "out_of_scope_ids": out_of_scope,
            "original_fields_preserved": originals_unchanged, "result_ids": identities,
            "returned_count": len(result), "expected_fixture_count": len(case["expected_ids"])}


def profile_fusion(module, query, dense_hits, documents, *, top_k=8):
    original = module._terms
    term_wall = term_cpu = term_calls = 0
    def measured_terms(*args, **kwargs):
        nonlocal term_wall, term_cpu, term_calls
        wall, cpu = time.perf_counter_ns(), time.process_time_ns()
        try:
            return original(*args, **kwargs)
        finally:
            term_cpu += time.process_time_ns() - cpu
            term_wall += time.perf_counter_ns() - wall
            term_calls += 1
    module._terms = measured_terms
    wall, cpu = time.perf_counter_ns(), time.process_time_ns()
    try:
        result = module.rerank_chunks(query, dense_hits, documents, top_k)
    finally:
        total_cpu = time.process_time_ns() - cpu
        total_wall = time.perf_counter_ns() - wall
        module._terms = original
    return result, {"total_wall_ms": total_wall / 1e6, "total_cpu_ms": total_cpu / 1e6,
        "tokenization_wall_ms": term_wall / 1e6, "tokenization_cpu_ms": term_cpu / 1e6,
        "remaining_wall_ms": max(0, total_wall - term_wall) / 1e6,
        "remaining_cpu_ms": max(0, total_cpu - term_cpu) / 1e6, "tokenization_calls": term_calls}


def summarize(samples):
    result = {}
    for field in ("total_wall_ms", "total_cpu_ms", "tokenization_wall_ms", "tokenization_cpu_ms",
                  "remaining_wall_ms", "remaining_cpu_ms"):
        values = sorted(item[field] for item in samples)
        result[field] = {"median": statistics.median(values), "p95": values[max(0, math.ceil(.95 * len(values)) - 1)],
                         "min": values[0], "max": values[-1]}
    result["sample_count"] = len(samples)
    return result


def benchmark_size(size, *, seed=DEFAULT_SEED, chunk_chars=384, warmups=1, repeats=5, measure_memory=True):
    if (type(warmups) is not int or not 0 <= warmups <= 10
            or type(repeats) is not int or not 1 <= repeats <= 30 or type(measure_memory) is not bool):
        raise ValueError("invalid_retrieval_benchmark_repetitions")
    documents, cases = build_dataset(size, seed=seed, chunk_chars=chunk_chars)
    input_digest = fingerprint(documents)
    module = load_fusion_module()
    measurements = []
    for case in cases:
        observed = []
        warmup_samples = []
        samples = []
        for round_number in range(warmups + repeats):
            hits, sample = profile_fusion(module, case["query"], case["dense_hits"], documents)
            observed.append(assess_result(hits, case, documents))
            (warmup_samples if round_number < warmups else samples).append(sample)
        memory = None
        if measure_memory:
            if tracemalloc.is_tracing():
                raise ValueError("benchmark_memory_probe_requires_own_tracer")
            memory_module = load_fusion_module()
            tracemalloc.start()
            try:
                hits, _ = profile_fusion(memory_module, case["query"], case["dense_hits"], documents)
                retained, peak = tracemalloc.get_traced_memory()
            finally:
                tracemalloc.stop()
                prepared_cache = getattr(memory_module, "_PREPARED_LEXICAL_CACHE", None)
                if prepared_cache is not None:
                    prepared_cache.clear()
                sys.modules.pop(memory_module.__name__, None)
            observed.append(assess_result(hits, case, documents))
            memory = {"peak_python_alloc_bytes": peak, "retained_python_alloc_bytes": retained,
                      "corpus_prebuilt": True, "fresh_ranker_module": True,
                      "latency_probe_excluded": True, "native_or_rss_memory_included": False}
        stable = all(item["result_ids"] == observed[0]["result_ids"] for item in observed)
        measurements.append({"case_id": case["id"], "query": case["query"], "mode": case["mode"],
            "expected_ids": case["expected_ids"], "dense_input_sha256": fingerprint(case["dense_hits"]),
            "dense_input_count": len(case["dense_hits"]), "timing": summarize(samples),
            "samples": samples, "warmup_samples": warmup_samples, "memory": memory,
            "correctness": {"passed": all(item["passed"] for item in observed) and stable,
                            "stable_across_runs": stable, "observations": observed}})
    unchanged = fingerprint(documents) == input_digest
    prepared_cache = getattr(module, "_PREPARED_LEXICAL_CACHE", None)
    cache_stats = prepared_cache.stats() if prepared_cache is not None else None
    if prepared_cache is not None:
        prepared_cache.clear()
    sys.modules.pop(module.__name__, None)
    return {"chunk_count": size, "source_chunk_count": size, "input_document_count": len(documents),
            "metadata_only_count": 1, "chunk_chars": chunk_chars, "seed": seed,
            "corpus_utf8_bytes": sum(len(hit["text"].encode("utf-8")) for hit in documents),
            "corpus_sha256": input_digest, "corpus_unchanged": unchanged,
            "query_manifest_sha256": fingerprint([{key: value for key, value in case.items() if key != "dense_hits"} for case in cases]),
            "prepared_source_cache": cache_stats,
            "correctness_passed": unchanged and all(item["correctness"]["passed"] for item in measurements),
            "queries": measurements}


def benchmark_report(sizes=DEFAULT_SIZES, *, seed=DEFAULT_SEED, chunk_chars=384, warmups=1, repeats=5,
                     measure_memory=True, script_path=None, on_size=None):
    if type(sizes) not in {tuple, list} or not sizes or len(sizes) != len(set(sizes)):
        raise ValueError("invalid_retrieval_benchmark_sizes")
    before = source_fingerprints(script_path=script_path)
    rows = []
    for size in sizes:
        row = benchmark_size(size, seed=seed, chunk_chars=chunk_chars, warmups=warmups,
                             repeats=repeats, measure_memory=measure_memory)
        rows.append(row)
        if on_size is not None:
            on_size(row)
    after = source_fingerprints(script_path=script_path)
    return {"schema_version": 1, "benchmark": "synthetic_retrieval_fusion_cpu", "environment": environment(),
            "parameters": {"sizes": list(sizes), "seed": seed, "chunk_chars": chunk_chars,
                           "warmups": warmups, "repeats": repeats, "top_k": 8, "measure_memory": measure_memory},
            "source_sha256_before": before, "source_sha256_after": after, "source_stable": before == after,
            "scope": {"synthetic_data_only": True, "network": False, "sql_io": False, "chroma_io": False,
                      "embedding": False, "llm": False, "private_data": False,
                      "dense_inputs": "fixed synthetic ranks; no embedding accuracy claim",
                      "time": "instrumented fusion call; per-_terms cumulative time; remaining includes instrumentation overhead",
                      "memory": "separate fresh-module tracemalloc probe; Python allocations only, prebuilt corpus excluded",
                      "limits": "controlled fixture matches, not real-library accuracy, total service latency, native memory or cost"},
            "correctness_passed": all(row["correctness_passed"] for row in rows), "rows": rows}
