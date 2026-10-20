"""Bounded, verbatim context assembly independent of retrieval and model calls.

Fixed-size indexed chunks may start or end halfway through a sentence.  The
helpers below only recover text from demonstrably overlapping adjacent chunks;
they never manufacture a bridge between unrelated passages.
"""
from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass
from typing import Callable


_SOURCE_HEADER = re.compile(
    r"^(语音文字稿(?:（来自视频声音）)?|画面分析(?:（来自视频画面）)?)[：:]\s*\n"
)
_TIMED_LINE = re.compile(
    r"(?m)^\[\d{1,2}:\d{2}(?::\d{2})?(?:[.，,]\d+)?\s*[–—-]\s*"
    r"\d{1,2}:\d{2}(?::\d{2})?(?:[.，,]\d+)?\]\s*"
)
_SENTENCE_END = re.compile(r"[。！？!?]+[’”\"』」）)]*|\.(?=\s|$)")
_LIMIT_MARKER = "\n[此片段超过长度限制，后文未提供]"
_MIN_OVERLAP = 32


def _value(item: dict, key: str):
    if key in item:
        return item[key]
    metadata = item.get("metadata")
    return metadata.get(key) if isinstance(metadata, dict) else None


def _source_kind(value: object) -> str | None:
    text = str(value or "").strip().lower()
    if text in {"speech", "audio", "transcript", "audio_transcript", "语音", "声音", "语音文字稿", "语音文字稿（来自视频声音）"}:
        return "speech"
    if text in {"visual", "vision", "visual_analysis", "画面", "画面分析", "画面分析（来自视频画面）"}:
        return "visual"
    return None


@dataclass(frozen=True)
class _Document:
    chunk_id: str
    video_id: str
    index: int
    kind: str
    prefix: str
    body: str


def _parts(item: dict) -> tuple[str, str, str] | None:
    text = item.get("text", item.get("document", ""))
    if not isinstance(text, str) or not text.strip():
        return None
    header = _SOURCE_HEADER.match(text)
    declared = _source_kind(_value(item, "source_type"))
    inferred = _source_kind(header.group(1)) if header else None
    if declared and inferred and declared != inferred:
        return None
    kind = inferred or declared
    if not kind:
        return None
    prefix = text[:header.end()] if header else ""
    body = text[len(prefix):].strip()
    # A title/author catalogue entry cannot become evidence via a neighbour.
    lines = [line.strip() for line in body.splitlines() if line.strip()]
    if not lines or body == str(_value(item, "title") or "").strip():
        return None
    if all(line.startswith(("视频标题：", "作者：")) for line in lines):
        return None
    if body.startswith("视频标题："):
        return None
    return prefix, body, kind


def _document(item: dict) -> _Document | None:
    parts = _parts(item)
    video_id = str(_value(item, "platform_item_id") or "")
    chunk_id = str(_value(item, "chunk_id") or "")
    index = _value(item, "chunk_index")
    if index is None and chunk_id:
        tail = chunk_id.rsplit(":", 1)[-1]
        index = int(tail) if tail.isdigit() else None
    if isinstance(index, str) and index.isdigit():
        index = int(index)
    if not parts or not video_id or not chunk_id or isinstance(index, bool) or not isinstance(index, int) or index < 0:
        return None
    prefix, body, kind = parts
    return _Document(chunk_id, video_id, index, kind, prefix, body)


def _overlap(left: str, right: str) -> int:
    """Find a substantial, unique exact overlap, rather than guess adjacency."""
    for count in range(min(len(left), len(right)), _MIN_OVERLAP - 1, -1):
        overlap = left[-count:]
        if right.startswith(overlap):
            if left.find(overlap) == len(left) - count and right.rfind(overlap) == 0:
                return count
            # Repeated speech does not identify a unique joining position.
            return 0
    return 0


def _boundaries(text: str) -> set[int]:
    """Offsets after complete sentences/lines, with timestamps kept attached."""
    positions = {match.end() for match in _SENTENCE_END.finditer(text)}
    positions.update(match.end() for match in re.finditer(r"\n", text))
    positions.update(match.start() for match in _TIMED_LINE.finditer(text))
    # Whitespace immediately following a boundary is part of the separator.
    for position in tuple(positions):
        end = position
        while end < len(text) and text[end].isspace():
            end += 1
        positions.add(end)
    return positions


def expand_hit_context(
    hits: list[dict], documents: list[dict], *, max_extra_chars: int = 350,
) -> list[dict]:
    """Complete boundary sentences using same-video, same-source neighbours.

    ``documents`` contains the original indexed chunks, with ``text`` (or
    ``document``), ``chunk_id`` and ``platform_item_id``.  ``chunk_index`` may be
    supplied directly or encoded in the final colon-separated chunk-id field.
    Missing source labels, duplicate positions, changed hit text and uncertain
    overlaps all fail closed: the hit is returned unchanged.
    """
    if max_extra_chars <= 0:
        return [dict(hit) for hit in hits]
    by_id: dict[tuple[str, str], list[_Document]] = defaultdict(list)
    by_position: dict[tuple[str, str, int], list[_Document]] = defaultdict(list)
    for item in documents:
        source = _document(item)
        if source:
            by_id[source.video_id, source.chunk_id].append(source)
            by_position[source.video_id, source.kind, source.index].append(source)

    result: list[dict] = []
    for hit in hits:
        expanded = dict(hit)
        parts = _parts(hit)
        key = (str(_value(hit, "platform_item_id") or ""), str(_value(hit, "chunk_id") or ""))
        matches = by_id.get(key, [])
        if not parts or len(matches) != 1:
            result.append(expanded)
            continue
        source = matches[0]
        prefix, body, kind = parts
        if body != source.body or kind != source.kind or len(by_position[source.video_id, kind, source.index]) != 1:
            result.append(expanded)
            continue

        before = by_position.get((source.video_id, kind, source.index - 1), [])
        after = by_position.get((source.video_id, kind, source.index + 1), [])
        previous = before[0] if len(before) == 1 else None
        following = after[0] if len(after) == 1 else None
        leading = _overlap(previous.body, body) if previous else 0
        trailing = _overlap(body, following.body) if following else 0
        joined = body
        start, end = 0, len(body)
        if leading:
            joined = previous.body + body[leading:]
            start = len(previous.body) - leading
            end = start + len(body)
        if trailing:
            joined += following.body[trailing:]

        boundaries = _boundaries(joined)
        starts = [offset for offset in boundaries if max(0, start - max_extra_chars) <= offset <= start]
        ends = [offset for offset in boundaries if end <= offset <= min(len(joined), end + max_extra_chars)]
        new_start = max(starts) if leading and starts else start
        new_end = min(ends) if trailing and ends else end
        # One bounded envelope, never arbitrary character truncation.  Prefer
        # the cheaper repair if completing both edges would exceed the cap.
        if (start - new_start) + (new_end - end) > max_extra_chars:
            if start - new_start <= new_end - end:
                new_end = end
            else:
                new_start = start
        if new_start != start or new_end != end:
            expanded["text"] = prefix + joined[new_start:new_end]
            provenance = []
            if new_start < start:
                provenance.append(previous.chunk_id)
            provenance.append(source.chunk_id)
            if new_end > end:
                provenance.append(following.chunk_id)
            expanded["expanded_from_chunk_ids"] = provenance
        result.append(expanded)
    return result


def _body_key(hit: dict) -> tuple[str, str, str]:
    parts = _parts(hit)
    text = str(hit.get("text") or "").strip()
    kind = ""
    if parts:
        _, text, kind = parts
    return str(hit.get("platform_item_id") or ""), kind, text


def _bounded_first_hit(hit: dict, max_chars: int, format_blocks: Callable) -> dict | None:
    text = str(hit.get("text") or "")
    parts = _parts(hit)
    prefix = parts[0] if parts else ""
    body = text[len(prefix):]
    # Only complete sentence/line boundaries are eligible; an unpunctuated
    # oversize line is skipped instead of being presented as a complete claim.
    for end in sorted(_boundaries(body), reverse=True):
        if end <= 0 or not body[:end].strip():
            continue
        limited = {**hit, "text": prefix + body[:end] + _LIMIT_MARKER}
        if len(format_blocks([limited])) <= max_chars:
            return limited
    return None


def select_context_hits(
    hits: list[dict], *, max_count: int, max_chars: int,
    format_blocks: Callable, has_source_text: Callable,
    scope_ids: set[str] | None = None,
) -> list[dict]:
    """Pack reranked verbatim hits without stopping at the first oversize one."""
    selected: list[dict] = []
    seen_ids: set[tuple[str, str]] = set()
    seen_bodies: list[tuple[str, str, str]] = []
    if max_count <= 0:
        return selected
    for original in hits:
        if len(selected) >= max_count:
            break
        video_id = str(original.get("platform_item_id") or "")
        if scope_ids is not None and video_id not in scope_ids:
            continue
        if not has_source_text(original):
            continue
        identity = (video_id, str(original.get("chunk_id") or ""))
        body_key = _body_key(original)
        if (identity[1] and identity in seen_ids) or any(
            body_key[0:2] == seen[0:2] and body_key[2] in seen[2]
            for seen in seen_bodies
        ):
            continue
        hit = dict(original)
        if max_chars > 0 and len(format_blocks(selected + [hit])) > max_chars:
            if selected:
                continue
            hit = _bounded_first_hit(hit, max_chars, format_blocks)
            if hit is None:
                continue
        selected.append(hit)
        seen_ids.add(identity)
        seen_bodies.append(_body_key(hit))
    return selected
