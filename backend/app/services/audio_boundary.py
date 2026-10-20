"""Conservative, evidence-preserving proposals for isolated ASR cut artifacts.

These checks bound an edit; they do not establish acoustic correctness. Full
provider outputs and the original records remain in the caller's audit payload.
"""
from __future__ import annotations

from copy import deepcopy
from difflib import SequenceMatcher
import math
import re
import unicodedata

STRATEGY_VERSION = "unique-anchor-center-v3"
_NUMBERS = re.compile(r"[0-9]+(?:[.,:/%％+-][0-9]*)*|[零〇一二两三四五六七八九十百千万亿点几半负]+")
_NEGATION = frozenset("不没无未非否勿别莫")
_UNITS = frozenset("亿万千百十年月日时分秒元角米克斤吨升度倍成岁％%")
_PROTECTED_SYMBOLS = frozenset("%％‰℃℉°+-−±=<>≤≥÷×/．")


def _symbols(text: str) -> list[str]:
    return [char for char in text if char in _PROTECTED_SYMBOLS
            or unicodedata.category(char) in {"Sm", "Sc", "So"}]


def _ordered(parts: list[dict]) -> bool:
    previous = 0.0
    for part in parts:
        start, end = part["start_seconds"], part["end_seconds"]
        if not math.isfinite(start) or not math.isfinite(end) or start < previous or end <= start:
            return False
        previous = end
    return True


def _normalized(text: str) -> tuple[str, list[int]]:
    indexes = [i for i, char in enumerate(text) if char.isalnum()]
    return "".join(text[i] for i in indexes), indexes


def _joined(parts: list[dict]) -> tuple[str, list[tuple[int, int]]]:
    texts, ranges, position = [], [], 0
    for part in parts:
        text = part["text"]
        texts.append(text)
        ranges.append((position, position + len(text)))
        position += len(text) + 1
    return "\n".join(texts), ranges


def select_boundary_candidates(root_groups: list[dict], max_windows: int = 4) -> list[dict]:
    """Reserve capacity for distinct textual symptoms, then keep source order.

    Root membership is supplied by the recognizer; model timestamps are not
    treated as word-level alignments. No specific video or second is encoded.
    """
    choices = []
    for index in range(len(root_groups) - 1):
        left, right = root_groups[index:index + 2]
        if not left["parts"] or not right["parts"]:
            continue
        tail = _normalized(left["parts"][-1]["text"])[0]
        head = _normalized(right["parts"][0]["text"])[0]
        symptoms = []
        if 0 < len(tail) <= 4:
            symptoms.append("short_trailing_fragment")
        if head and head[0] in "的地得" and len(head) <= 16:
            symptoms.append("orphan_particle")
        if tail and head and tail[-1] == head[0] and "\u4e00" <= head[0] <= "\u9fff":
            symptoms.append("repeated_boundary_character")
        if symptoms:
            choices.append({"left_root_index": index, "boundary_seconds": right["start_seconds"],
                            "symptoms": symptoms, "head_length": len(head)})
    selected = []
    for category in ("short_trailing_fragment", "orphan_particle"):
        matches = [c for c in choices if category in c["symptoms"] and c not in selected]
        if category == "orphan_particle":
            matches.sort(key=lambda c: (c["head_length"], c["boundary_seconds"]))
        if matches and len(selected) < max_windows:
            selected.append(matches[0])
    for choice in choices:
        if len(selected) >= max_windows:
            break
        if choice not in selected:
            selected.append(choice)
    return sorted(selected, key=lambda c: c["boundary_seconds"])


def _propose_boundary_splice(left_parts: list[dict], right_parts: list[dict], review_parts: list[dict], *, allow_duplicate=False) -> dict:
    """Return a proposal using only two unique exact anchors around a tiny gap.

    The caller must also reject overlapping record ranges across proposals.
    Times on merged records are the original record envelope, never claimed as
    precise times for individual repaired words.
    """
    parts = deepcopy(left_parts + right_parts)
    decision = {"accepted": False, "reason": "missing_context", "before_parts": parts,
                "after_parts": deepcopy(parts), "acoustic_status": "unverified_acoustic"}
    if not left_parts or not right_parts or not review_parts:
        return decision
    if not _ordered(parts):
        decision["reason"] = "unordered_original_records"
        return decision
    if not _ordered(review_parts):
        decision["reason"] = "unordered_or_overlapping_review_records"
        return decision
    left_raw, _ = _joined(left_parts)
    raw, ranges = _joined(parts)
    heard_raw, _ = _joined(review_parts)
    old, old_map = _normalized(raw)
    heard, heard_map = _normalized(heard_raw)
    cut = len(_normalized(left_raw)[0])
    candidates = []
    for left_end in range(cut - 3, max(11, cut - 25), -1):
        left_anchor = old[left_end - 12:left_end]
        if len(left_anchor) != 12 or old.find(left_anchor) != old.rfind(left_anchor):
            continue
        hp = heard.find(left_anchor)
        if hp < 0 or hp != heard.rfind(left_anchor):
            continue
        for right_start in range(cut + 3, min(len(old) - 11, cut + 25)):
            right_anchor = old[right_start:right_start + 12]
            if len(right_anchor) != 12 or old.find(right_anchor) != old.rfind(right_anchor):
                continue
            hq = heard.find(right_anchor)
            if hq < hp + 12 or hq != heard.rfind(right_anchor):
                continue
            old_gap, new_gap = old[left_end:right_start], heard[hp + 12:hq]
            if len(old_gap) > 12 or len(new_gap) > 12:
                continue
            start, end = old_map[left_end - 1] + 1, old_map[right_start]
            heard_start, heard_end = heard_map[hp + 11] + 1, heard_map[hq]
            replacement = heard_raw[heard_start:heard_end]
            proposed = raw[:start] + replacement + raw[end:]
            proposed_normal = _normalized(proposed)[0]
            duplicate = bool(allow_duplicate and 0 < cut < len(old)
                and old[cut - 1] == old[cut] and '\u4e00' <= old[cut] <= '\u9fff'
                and old[cut] not in _NEGATION and old[cut] not in _UNITS
                and proposed_normal == old[:cut] + old[cut + 1:]
                and re.findall(r"[A-Za-z0-9]+(?:['’][A-Za-z0-9]+)*", raw)
                    == re.findall(r"[A-Za-z0-9]+(?:['’][A-Za-z0-9]+)*", proposed))
            changes = [op for op in SequenceMatcher(None, old_gap, new_gap, autojunk=False).get_opcodes() if op[0] != "equal"]
            rejection = None
            if not duplicate and (_NUMBERS.findall(raw) != _NUMBERS.findall(proposed)
                    or [char for char in raw if char.isnumeric()] != [char for char in proposed if char.isnumeric()]):
                rejection = "numeric_change"
            elif _symbols(raw[start:end]) != _symbols(replacement):
                rejection = "protected_symbol_change"
            elif not duplicate and raw[start:end] != replacement and any(char.isnumeric() for char in raw[start:end] + replacement):
                rejection = "numeric_context_change"
            elif not duplicate and raw[start:end] != replacement and any(char in _UNITS for char in raw[start:end] + replacement):
                rejection = "unit_context_change"
            elif re.findall(r"摄氏|华氏", _normalized(raw)[0]) != re.findall(r"摄氏|华氏", _normalized(proposed)[0]):
                rejection = "temperature_scale_change"
            elif raw[start:end] != replacement and any(char in _NEGATION for char in (
                    raw[max(0, start - 3):end + 3] + heard_raw[max(0, heard_start - 3):heard_end + 3])):
                rejection = "negation_context_change"
            elif not duplicate and raw[start:end] != replacement and any(char.isascii() and char.isalnum()
                                                      for char in raw[start:end] + replacement):
                rejection = "ascii_context_change"
            elif any(any(char in _NEGATION for char in old_gap[a:b] + new_gap[c:d])
                     for _, a, b, c, d in changes):
                rejection = "negation_change"
            elif any(any(char.isascii() and char.isalnum() or char in _UNITS
                         for char in old_gap[a:b] + new_gap[c:d]) for _, a, b, c, d in changes):
                rejection = "protected_symbol_or_unit_change"
            elif not duplicate and any(tag == "delete" or d - c < b - a for tag, a, b, c, d in changes):
                rejection = "deletion_or_shortening"
            elif sum(max(b-a, d-c) for _, a, b, c, d in changes) > 4:
                rejection = "edit_too_large"
            elif proposed == raw:
                rejection = "no_change"
            first = next(i for i, (_, finish) in enumerate(ranges) if finish >= start)
            last = next(i for i, (begin, _) in enumerate(ranges) if begin <= end < ranges[i][1])
            candidates.append({"left_anchor": left_anchor, "right_anchor": right_anchor,
                "original_center": raw[start:end], "review_center": replacement,
                "old_normalized_center": old_gap, "new_normalized_center": new_gap,
                "raw_start": start, "raw_end": end, "record_start": first, "record_end": last,
                "proposed": proposed, "rejection": rejection, "cross_boundary_duplicate": duplicate})
    if not candidates:
        decision["reason"] = "no_unique_two_sided_anchors"
        return decision
    proposals = {_normalized(item["proposed"])[0] for item in candidates}
    if len(proposals) != 1:
        decision["reason"] = "ambiguous_center"
        return decision
    valid = [c for c in candidates if c["rejection"] is None]
    chosen = min(valid or candidates, key=lambda c: (len(c["old_normalized_center"]), len(c["review_center"])))
    decision["anchors"] = {k: v for k, v in chosen.items() if k not in {"proposed", "rejection"}}
    if not valid:
        decision["reason"] = chosen["rejection"]
        return decision
    first, last = chosen["record_start"], chosen["record_end"]
    record_begin, record_end = ranges[first][0], ranges[last][1]
    replacement = (raw[record_begin:chosen["raw_start"]] + chosen["review_center"]
                   + raw[chosen["raw_end"]:record_end])
    merged = {"start_seconds": parts[first]["start_seconds"],
              "end_seconds": max(p["end_seconds"] for p in parts[first:last + 1]), "text": replacement}
    decision.update(accepted=True, reason="bounded_exact_anchor_splice",
                    after_parts=parts[:first] + [merged] + parts[last + 1:])
    decision['cross_boundary_duplicate'] = chosen['cross_boundary_duplicate']
    return decision


def propose_boundary_splice(left_parts: list[dict], right_parts: list[dict], review_parts: list[dict],
                            *, confirmation_parts: list[dict] | None = None) -> dict:
    ordinary = _propose_boundary_splice(left_parts, right_parts, review_parts)
    if ordinary['accepted'] or confirmation_parts is None:
        return ordinary
    primary = _propose_boundary_splice(left_parts, right_parts, review_parts, allow_duplicate=True)
    if not primary['accepted'] or not primary.get('cross_boundary_duplicate'):
        return ordinary
    corroborated = _propose_boundary_splice(left_parts, right_parts, confirmation_parts, allow_duplicate=True)
    join = lambda rows: _normalized('\n'.join(r['text'] for r in rows))[0]
    if (corroborated['accepted'] and corroborated.get('cross_boundary_duplicate')
            and join(primary['after_parts']) == join(corroborated['after_parts'])):
        primary.update(reason='two_window_confirmed_boundary_duplicate',
                       confirmation_anchors=corroborated['anchors'],
                       confirmation_required=True, confirmation_agreed=True)
        return primary
    ordinary['confirmation_agreed'] = False
    return ordinary
