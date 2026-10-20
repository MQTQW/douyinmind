"""Structural transcript checks. None of these can prove speech was not omitted."""
from __future__ import annotations

import math
import re


def _number(value) -> bool:
    return type(value) in (int, float) and math.isfinite(value)


def _gaps(spans: list[tuple[float, float]], duration: float, tolerance: float = .02) -> list[dict]:
    cursor = 0.0
    gaps = []
    for start, end in sorted(spans):
        if start > cursor + tolerance:
            gaps.append({"start_seconds": round(cursor, 3), "end_seconds": round(start, 3)})
        cursor = max(cursor, end)
    if cursor < duration - tolerance:
        gaps.append({"start_seconds": round(cursor, 3), "end_seconds": round(duration, 3)})
    return gaps


def listening_windows(duration: float, segment_seconds: float = 60) -> list[dict]:
    if not _number(duration) or duration <= 0:
        return []
    windows = []
    seen = set()
    def add(kind, start, end):
        start, end = round(max(0, start), 3), round(min(duration, end), 3)
        if end > start and (start, end) not in seen:
            seen.add((start, end))
            windows.append({"kind": kind, "start_seconds": start, "end_seconds": end,
                            "review_status": "pending", "heard_text": "", "notes": ""})
    add("开头", 0, 30)
    add("中间", duration / 2 - 15, duration / 2 + 15)
    add("结尾", duration - 30, duration)
    if _number(segment_seconds) and segment_seconds > 0:
        for index in range(1, min(1000, math.ceil(duration / segment_seconds))):
            boundary = index * segment_seconds
            if boundary < duration:
                add("分段交界", boundary - 5, boundary + 5)
    return windows


def audit_transcript(duration: float, analysis: dict, transcript_text: str) -> dict:
    result = {"automatic_checks_only": True, "omission_verdict": "需要回听原音频，不能仅凭结构判定没有漏听",
              "issues": [], "speech_gaps_to_listen": [], "repeated_text_to_listen": []}
    if not _number(duration) or duration <= 0:
        return {**result, "structural_status": "unavailable", "issues": ["尚无有效视频时长"],
                "request_coverage": None, "listening_windows": []}
    analysis = analysis if isinstance(analysis, dict) else {}
    roots = analysis.get("raw_results", [])
    request_spans = []
    invalid_roots = 0
    for item in roots if isinstance(roots, list) else []:
        if not isinstance(item, dict):
            invalid_roots += 1
            continue
        start, length = item.get("start_seconds"), item.get("duration_seconds")
        if (not _number(start) or not _number(length) or start < 0 or start >= duration or length <= 0
                or start + length > duration + .5 or not item.get("audio_response")):
            invalid_roots += 1
            continue
        request_spans.append((float(start), min(duration, start + length)))
    request_gaps = _gaps(request_spans, duration) if request_spans else []
    missing_seconds = sum(item["end_seconds"] - item["start_seconds"] for item in request_gaps)
    result["request_coverage"] = ({"ratio": max(0, 1 - missing_seconds / duration),
        "completed_root_segments": len(request_spans), "invalid_root_records": invalid_roots,
        "duplicate_root_records": len(request_spans) - len(set(request_spans)),
        "uncovered_ranges": request_gaps,
        "meaning": "有保存结果的音频请求时间覆盖；不是听写准确率或无遗漏证明"} if request_spans else None)
    if not request_spans:
        result["issues"].append("没有完整音频请求分段记录，无法自动核对送听范围")
    if request_gaps:
        result["issues"].append("音频请求记录存在未覆盖时段")
    if invalid_roots:
        result["issues"].append("有无效或缺少识别结果的音频分段记录")
    if len(set(request_spans)) != len(request_spans):
        result["issues"].append("音频根分段记录重复，需核对是否重复合并")

    speech = analysis.get("transcript_segments", [])
    valid, invalid, out_of_order = [], 0, 0
    previous_start = -1.0
    for item in speech if isinstance(speech, list) else []:
        if not isinstance(item, dict):
            invalid += 1
            continue
        start, end, text = item.get("start_seconds"), item.get("end_seconds"), item.get("text")
        if (not _number(start) or not _number(end) or start < 0 or start >= duration or end <= start
                or end > duration + .5 or not isinstance(text, str) or not text.strip()):
            invalid += 1
            continue
        out_of_order += start < previous_start
        previous_start = start
        valid.append(item)
    if invalid:
        result["issues"].append(f"{invalid} 条语句时间或文字无效")
    if out_of_order:
        result["issues"].append("语句时间顺序异常")
    normalized = lambda text: re.sub(r"\s+", "", text)
    if normalized(transcript_text or "") != normalized("".join(item["text"] for item in valid)):
        result["issues"].append("完整文字稿与逐句记录不一致")
    if not valid:
        result["issues"].append("没有可供核对的人声文字")
    result["valid_utterances"] = len(valid)
    result["speech_gaps_to_listen"] = [span for span in _gaps(
        [(item["start_seconds"], min(duration, item["end_seconds"])) for item in valid], duration)
        if span["end_seconds"] - span["start_seconds"] >= 10]
    for first, second in zip(valid, valid[1:]):
        if len(normalized(first["text"])) >= 15 and normalized(first["text"]) == normalized(second["text"]):
            result["repeated_text_to_listen"].append({"start_seconds": first["start_seconds"],
                "end_seconds": second["end_seconds"], "text": first["text"]})
    processing = analysis.get("audio_processing") or {}
    segment_seconds = processing.get("segment_seconds", 60) if isinstance(processing, dict) else 60
    result["listening_windows"] = listening_windows(duration, segment_seconds)
    result["structural_status"] = "passed" if not result["issues"] else "needs_check"
    result["listening_note"] = "无文字的时间可能是静音、音乐或操作等待；重复也可能是讲者真实复述，均只能列为回听线索。"
    return result
