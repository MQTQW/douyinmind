"""Deterministic, scope-preserving lexical/dense retrieval fusion.

Only original source chunks are ranked here.  No model, answer key, external
corpus, or generated summary participates in scoring.  ``documents`` is the
authoritative, already scope-filtered collection; dense hits outside it are
ignored.  Work is linear in source text size plus a bounded candidate rerank.
"""
from __future__ import annotations

import math
import re
from collections import Counter


_CJK = re.compile(r"[\u3400-\u9fff]+")
_LATIN = re.compile(r"[a-z][a-z0-9_+.-]*|\d+(?:\.\d+)?", re.I)
_TIMESTAMP = re.compile(r"\[?\b\d{1,2}:\d{2}(?::\d{2})?(?:\.\d+)?\b\]?")
_QUESTION_FILLERS = re.compile(
    "|".join(sorted((
        "请帮我", "帮我", "请问", "告诉我", "根据", "这个视频", "该视频", "视频中",
        "视频里", "里面", "其中", "关于", "如何", "为什么", "是什么", "哪些",
        "什么", "怎么", "怎样", "能不能", "可不可以", "是否", "分别", "介绍一下",
        "总结一下", "讲一下", "说明一下", "具体来说", "提到了", "提到", "讲了",
        "说明了", "介绍了", "有没有", "以及", "并且", "同时", "还有", "视频", "请",
    ), key=len, reverse=True))
)
_STOP = frozenset((
    "我们", "你们", "他们", "这个", "那个", "一个", "一些", "一种", "这样", "那样",
    "可以", "就是", "所以", "因为", "但是", "然后", "对于", "通过", "进行", "需要",
    "哪些", "什么", "是否", "如何", "视频", "内容", "里面", "其中", "关于", "请问",
    "分别", "时候", "还有", "以及", "并且", "同时", "the", "a", "an", "of", "to",
    "in", "on", "and", "or", "is", "are", "was", "were", "what", "how", "why",
    "does", "do", "did", "please", "video", "with", "for", "it", "its", "that", "this",
))
_FACET_SPLIT = re.compile(r"[，,。；;？！?!\n、]|以及|并且|同时|还有|分别|和|与|及|而")
_MAX_QUERY_FEATURES = 256
_MAX_DENSE_CANDIDATES = 128
_MAX_LEXICAL_CANDIDATES = 64
_MAX_CANDIDATES = _MAX_DENSE_CANDIDATES + _MAX_LEXICAL_CANDIDATES


def _source_text(hit: dict) -> bool:
    """Reject the title/author chunks used only for cataloguing videos."""
    text = str(hit.get("text") or "").strip()
    title = str(hit.get("title") or "").strip()
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines or all(line.startswith(("视频标题：", "作者：")) for line in lines):
        return False
    if text.startswith("视频标题："):
        declared, separator, author = text[len("视频标题："):].rpartition("\n作者：")
        if not separator:
            declared = text[len("视频标题："):]
        matches_title = declared.strip() == title or (
            len(title) == 500 and declared.strip().startswith(title)
        )
        if matches_title and (not separator or "\n" not in author.strip()):
            return False
    return text != title


def _terms(text: str, *, query: bool = False) -> Counter[str]:
    text = _TIMESTAMP.sub(" ", text.lower())
    if query:
        text = _QUESTION_FILLERS.sub(" ", text)
    tokens: Counter[str] = Counter()
    for run in _CJK.findall(text):
        for size in (2, 3):
            tokens.update(run[i:i + size] for i in range(len(run) - size + 1)
                          if run[i:i + size] not in _STOP)
    tokens.update(word for word in _LATIN.findall(text) if word not in _STOP)
    return tokens


def _normal_title(text: str) -> str:
    # Hashtags are decorations, not a reliable part of a spoken video name.
    return re.sub(r"[^\w\u3400-\u9fff]", "", text.split("#", 1)[0].lower())


def _mentioned_video(query: str, documents: list[dict]) -> str | None:
    """Find a unique explicitly named title, otherwise apply no title bias."""
    names = re.findall(r"[《“\"]([^》”\"\n]{3,150})[》”\"]", query)
    names += re.findall(r"(?:^|[，,。；;？！?!])\s*([^，,。；;？！?!]{3,100}?)视频", query)
    titles: dict[str, str] = {}
    for hit in documents:
        titles[str(hit.get("platform_item_id", ""))] = _normal_title(str(hit.get("title") or ""))
    matched: set[str] = set()
    for raw_name in names:
        name = _normal_title(re.sub(r"^(?:请问|根据|在|这个|关于)+", "", raw_name))
        if len(name) < 3:
            continue
        found = {
            video_id for video_id, title in titles.items()
            if title and (name == title or (
                len(name) >= 4 and name in title and len(name) / len(title) >= 0.4
            ))
        }
        if len(found) == 1:
            matched.update(found)
    return next(iter(matched)) if len(matched) == 1 else None


def _identity(hit: dict) -> tuple[str, str]:
    return (str(hit.get("platform_item_id") or ""), str(hit.get("chunk_id") or hit.get("text") or ""))


def rerank_chunks(
    query: str, dense_hits: list[dict], documents: list[dict], top_k: int,
) -> list[dict]:
    """Fuse BM25 and dense ranks, then reward new query evidence coverage.

    A lightweight greedy pass discourages near-identical excerpts and rewards
    coverage of additional question clauses.  It cannot certify that an answer
    exists: the answering layer must still ground every claim in returned text.
    Original dictionaries are copied without changing any original field.
    """
    if top_k <= 0 or not documents:
        return []
    docs_by_id: dict[tuple[str, str], dict] = {}
    for hit in documents:
        if _source_text(hit):
            docs_by_id.setdefault(_identity(hit), hit)
    if not docs_by_id:
        return []

    # Preserve the original dense hit (and its score) where one exists, while
    # deriving lexical evidence only from the authoritative scoped documents.
    dense_by_id: dict[tuple[str, str], dict] = {}
    dense_rank: dict[tuple[str, str], int] = {}
    for hit in dense_hits[:_MAX_DENSE_CANDIDATES]:
        key = _identity(hit)
        if key in docs_by_id and key not in dense_rank and _source_text(hit):
            dense_rank[key] = len(dense_rank) + 1
            dense_by_id[key] = hit

    query_terms = _terms(query, query=True)
    # Bound pathological queries without privileging their repeated words.
    query_keys = set(list(query_terms)[:_MAX_QUERY_FEATURES])
    term_counts: dict[tuple[str, str], Counter[str]] = {}
    doc_frequency: Counter[str] = Counter()
    lengths: dict[tuple[str, str], int] = {}
    for key, hit in docs_by_id.items():
        terms = _terms(str(hit.get("text") or ""))
        lengths[key] = max(1, sum(terms.values()))
        matching = Counter({term: terms[term] for term in query_keys if terms[term]})
        term_counts[key] = matching
        doc_frequency.update(matching.keys())
    average_length = sum(lengths.values()) / len(lengths)
    n_docs = len(docs_by_id)
    idf = {term: math.log1p((n_docs - doc_frequency[term] + 0.5) /
                          (doc_frequency[term] + 0.5)) for term in query_keys}
    # Bigram/trigram overlap represents one phrase; reduce the shorter token's
    # influence rather than allowing repeated boilerplate to dominate ranking.
    weights = {term: idf[term] * (0.65 if len(term) == 2 and _CJK.fullmatch(term) else 1.0)
               for term in query_keys}
    lexical_scores: dict[tuple[str, str], float] = {}
    for key, counts in term_counts.items():
        normalizer = 1.2 * (0.25 + 0.75 * lengths[key] / average_length)
        lexical_scores[key] = sum(
            weights[term] * min(count, 5) * 2.2 / (min(count, 5) + normalizer)
            for term, count in counts.items()
        )
    lexical_order = sorted((key for key in docs_by_id if lexical_scores[key] > 0),
                           key=lambda key: (-lexical_scores[key], key))
    lexical_rank = {key: rank for rank, key in enumerate(lexical_order, 1)}
    # Reserve the whole lexical allowance independently of the configured
    # dense candidate count.  The default 32 dense + 64 lexical behavior is
    # unchanged, while a larger dense pool cannot evict lexical-only evidence.
    candidate_keys = list(dict.fromkeys(
        list(dense_rank) + lexical_order[:_MAX_LEXICAL_CANDIDATES]
    ))[:_MAX_CANDIDATES]
    if not candidate_keys:
        return []
    max_lexical = max(lexical_scores.values(), default=0.0) or 1.0
    max_coverage = max((sum(weights[t] for t in term_counts[key]) for key in candidate_keys), default=0.0) or 1.0
    named_video = _mentioned_video(query, list(docs_by_id.values()))

    facets = []
    for clause in _FACET_SPLIT.split(query):
        facet = set(_terms(clause, query=True)) & query_keys
        if facet and facet not in facets:
            facets.append(facet)
    facet_weights = [sum(weights[t] for t in facet) or 1.0 for facet in facets]
    facet_coverage = {
        key: [sum(weights[t] for t in (set(term_counts[key]) & facet)) / total
              for facet, total in zip(facets, facet_weights)]
        for key in candidate_keys
    }
    base_scores = {}
    rrf_scores = {}
    for key in candidate_keys:
        rrf = sum(1.0 / (40 + rank) for rank in (dense_rank.get(key), lexical_rank.get(key)) if rank)
        normalized_rrf = rrf / (2.0 / 41)
        title_boost = 0.12 if named_video and key[0] == named_video else 0.0
        rrf_scores[key] = rrf
        base_scores[key] = 0.70 * normalized_rrf + 0.30 * lexical_scores[key] / max_lexical + title_boost

    # Text signatures are generated only for the bounded pool, never every
    # pair of documents.  Ignore timestamp tags when detecting overlap.
    signatures = {}
    for key in candidate_keys:
        text = re.sub(r"\s+", "", _TIMESTAMP.sub("", str(docs_by_id[key].get("text") or "")))[:6000]
        signatures[key] = {text[i:i + 3] for i in range(max(1, len(text) - 2))}
    selected: list[tuple[str, str]] = []
    covered: set[str] = set()
    facet_best = [0.0] * len(facets)
    remaining = set(candidate_keys)
    reasons: dict[tuple[str, str], tuple[float, float, float]] = {}
    while remaining and len(selected) < min(top_k, _MAX_CANDIDATES):
        best = None
        best_value = -math.inf
        best_details = (0.0, 0.0, 0.0)
        for key in sorted(remaining):
            novel = sum(weights[t] for t in set(term_counts[key]) - covered) / max_coverage
            facet_gain = sum(max(0.0, score - old) for score, old in zip(facet_coverage[key], facet_best)) / max(1, len(facets))
            redundancy = 0.0
            for old in selected:
                union = signatures[key] | signatures[old]
                similarity = len(signatures[key] & signatures[old]) / len(union) if union else 0.0
                redundancy = max(redundancy, similarity)
            value = base_scores[key] if not selected else (
                0.64 * base_scores[key] + 0.30 * novel + 0.16 * facet_gain - 0.42 * redundancy
            )
            if value > best_value:
                best, best_value, best_details = key, value, (novel, facet_gain, redundancy)
        if best is None:
            break
        selected.append(best)
        remaining.remove(best)
        covered.update(term_counts[best])
        facet_best = [max(old, score) for old, score in zip(facet_best, facet_coverage[best])]
        reasons[best] = best_details

    result = []
    for key in selected:
        hit = dict(dense_by_id.get(key, docs_by_id[key]))
        novel, facet_gain, redundancy = reasons[key]
        hit["retrieval_scores"] = {
            "dense_rank": dense_rank.get(key), "lexical_rank": lexical_rank.get(key),
            "bm25": round(lexical_scores[key], 6), "rrf": round(rrf_scores[key], 6),
            "new_term_coverage": round(novel, 6), "new_clause_coverage": round(facet_gain, 6),
            "redundancy": round(redundancy, 6),
            "explicit_title_match": bool(named_video and key[0] == named_video),
        }
        result.append(hit)
    return result
