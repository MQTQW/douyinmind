#!/usr/bin/env python3
"""Render a self-contained, Chinese retrieval exam and baseline report.

This tool only reads local JSON files. It never calls the application or a model.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import html
import json
import math
from pathlib import Path
from typing import Any


SUCCESS_STATUSES = {"evaluated"}


def route_label(value: Any) -> str:
    return {
        "vector": "检索视频原文（按意思查找）",
        "db_list": "只列视频标题",
        "db_content": "查找资料后归纳",
        "direct": "普通对话（没有检索原文）",
    }.get(value, str(value or "未记录"))


def esc(value: Any) -> str:
    return html.escape(str(value if value is not None else ""), quote=True)


def json_text(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2)


def boolean(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and value in (0, 1):
        return bool(value)
    return None


def number(value: Any) -> float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        result = float(value)
        return result if math.isfinite(result) else None
    return None


def timestamp(value: Any) -> str:
    seconds = number(value)
    if seconds is None or seconds < 0:
        return "时间未记录"
    minutes, remainder = divmod(seconds, 60)
    return f"{int(minutes):02d}:{remainder:05.2f}"


def status_ok(result: dict[str, Any] | None) -> bool:
    return bool(
        result
        and result.get("status") in SUCCESS_STATUSES
        and not result.get("validation_errors")
    )


def result_rows(document: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    if not document:
        return {}
    rows = document.get("cases", [])
    if isinstance(rows, dict):
        return {
            str(key): {"id": str(key), **value}
            for key, value in rows.items()
            if isinstance(value, dict)
        }
    return {
        str(row["id"]): row
        for row in rows
        if isinstance(row, dict) and "id" in row
    }


def metrics_for(result: dict[str, Any], kind: str) -> dict[str, Any]:
    value = result.get(f"{kind}_metrics")
    return value if isinstance(value, dict) else {}


def metric_value(result: dict[str, Any], kind: str, key: str) -> Any:
    value = metrics_for(result, kind).get(key)
    # An evaluated question sent to another route did not retrieve supporting
    # transcript text. Keep it in the end-to-end retrieval denominator as zero.
    # Other missing metrics remain unmeasured, including older result files.
    if (
        value is None
        and kind == "production"
        and status_ok(result)
        and boolean(result.get("router_bypass")) is True
    ):
        if key.startswith(("hit_", "all_evidence_")):
            return False
        if key.startswith("evidence_coverage_") or key in ("mrr_evidence", "distinct_video_at_5"):
            return 0.0
    return value


def aggregate(
    cases: list[dict[str, Any]],
    results: dict[str, dict[str, Any]],
    kind: str,
    key: str,
    numeric: bool = False,
) -> tuple[float, int]:
    values = []
    for case in cases:
        row = results.get(str(case.get("id")))
        if not case.get("answerable") or not status_ok(row):
            continue
        raw = metric_value(row, kind, key)
        value = number(raw) if numeric else boolean(raw)
        if value is not None:
            values.append(float(value))
    return sum(values), len(values)


def ratio(total: float, count: int, numeric: bool = False) -> str:
    if not count:
        return "未测"
    percentage = 100 * total / count
    if numeric:
        return f"{percentage:.1f}%（{count} 题的平均值）"
    return f"{int(total)} / {count} 题（{percentage:.1f}%）"


def metric_text(value: Any, numeric: bool = False) -> str:
    if numeric:
        parsed = number(value)
        return "未测" if parsed is None else f"{parsed:.3f}"
    parsed = boolean(value)
    return "未测" if parsed is None else ("命中" if parsed else "未命中")


def candidate_list(result: dict[str, Any], kind: str) -> list[dict[str, Any]]:
    if kind == "dense":
        raw = result.get("dense_results_top8", [])
    else:
        raw = result.get("production_context_candidates_top5")
        if raw is None:
            raw = result.get("production_candidate_top5")
        if raw is None:
            raw = result.get("production_visible_top5", [])
    return [row for row in (raw or []) if isinstance(row, dict)]


def render_candidates(result: dict[str, Any], kind: str) -> str:
    rows = candidate_list(result, kind)
    if not rows:
        return '<p class="muted">没有候选文字；可能尚未查找、没有结果，或被路由分到其他流程。</p>'
    blocks = []
    for index, row in enumerate(rows, 1):
        match = row.get("evidence_match")
        match_bool = boolean(match)
        match_label = (
            "命中参考依据" if match_bool is True else
            "未命中参考依据" if match_bool is False else
            "证据匹配：" + json_text(match) if match is not None else
            "未记录证据匹配判断"
        )
        indices = row.get("matched_evidence_indices")
        indices_text = (
            f"；对应参考依据序号：{json_text([value + 1 for value in indices])}"
            if indices is not None else ""
        )
        style = "good" if match_bool is True or indices else "muted"
        blocks.append(
            f'<article class="candidate"><h5>第 {esc(row.get("rank", index))} 条 · '
            f'{esc(row.get("title") or "未记录标题")}</h5>'
            f'<p class="meta">视频：{esc(row.get("video_id"))} · '
            f'片段：{esc(row.get("chunk_id"))} · 相似度：{esc(row.get("score", "未记录"))}</p>'
            f'<p class="{style}">{esc(match_label + indices_text)}</p>'
            f'<pre>{esc(row.get("text", ""))}</pre></article>'
        )
    return "".join(blocks)


def render_evidence(case: dict[str, Any]) -> str:
    evidence = case.get("evidence") or []
    if not evidence:
        return '<p class="muted">这道题是无答案对照题，没有指定支持答案的原文。</p>'
    parts = []
    for index, item in enumerate(evidence, 1):
        parts.append(
            f'<article class="evidence"><h5>参考依据 {index} · {esc(item.get("title"))}</h5>'
            f'<p class="meta">视频：{esc(item.get("video_id"))} · '
            f'{esc(timestamp(item.get("start_seconds")))} — '
            f'{esc(timestamp(item.get("end_seconds")))}</p>'
            f'<blockquote>{esc(item.get("quote"))}</blockquote></article>'
        )
    return "".join(parts)


def render_case_metrics(result: dict[str, Any], answerable: bool) -> str:
    if not answerable:
        return '<p class="notice">无答案题只列出实际找到的文字，等待人工核查是否无关。这里不自动给“回答正确”分数。</p>'
    labels = [
        ("hit_video_at_5", "前 5 条中找到正确视频", False),
        ("hit_evidence_at_5", "前 5 条中至少找到一条依据", False),
        ("all_evidence_at_5", "前 5 条中找到全部参考依据", False),
        ("evidence_coverage_at_5", "前 5 条的参考依据覆盖比例", True),
        ("hit_evidence_at_8", "前 8 条中至少找到一条依据", False),
        ("all_evidence_at_8", "前 8 条中找到全部参考依据", False),
        ("mrr_evidence", "首条证据的排名得分", True),
        ("distinct_video_at_5", "前 5 条涉及的视频数", True),
    ]
    body = []
    for key, label, numeric in labels:
        cells = []
        for kind in ("production", "dense"):
            value = metric_value(result, kind, key) if status_ok(result) else None
            cells.append(f"<td>{esc(metric_text(value, numeric))}</td>")
        body.append(f"<tr><th>{esc(label)}</th>{''.join(cells)}</tr>")
    return (
        '<div class="table-wrap"><table><thead><tr><th>检查项</th>'
        '<th>当前流程候选</th><th>直接向量查找</th></tr></thead><tbody>'
        + "".join(body) + '</tbody></table></div>'
    )


def render_case(case: dict[str, Any], result: dict[str, Any] | None) -> str:
    answerable = bool(case.get("answerable"))
    scope = case.get("scope") or {}
    kind_label = "可回答" if answerable else "无答案对照"
    if result is None:
        status = "尚未运行"
    elif status_ok(result):
        status = "已评测" if answerable else "已查找，待人工检查"
    elif result.get("validation_errors"):
        status = "题目或证据校验未通过，不计入得分"
    else:
        status = f'未计分：{result.get("status", "状态未知")}'
    search = " ".join(
        str(value) for value in (
            case.get("id"), case.get("question"), case.get("category"),
            scope.get("label"), case.get("expected_answer"),
        )
    )
    result_part = '<p class="muted">尚未提供这一题的运行结果。</p>'
    if result is not None:
        bypass = boolean(result.get("router_bypass"))
        bypass_text = "是" if bypass is True else "否" if bypass is False else "未记录"
        errors = result.get("validation_errors")
        errors_part = (
            f'<pre class="error">{esc(json_text(errors))}</pre>' if errors else ""
        )
        result_part = (
            f'<p>预期流程：{esc(route_label(case.get("expected_route")) if answerable else "无答案对照，不在此评判分流正确性")}；'
            f'实际流程：{esc(route_label(result.get("production_route")))}；'
            f'当前流程是否跳过了原文查找：{esc(bypass_text)}。</p>'
            + errors_part + render_case_metrics(result, answerable)
            + '<details class="sub"><summary>查看当前流程实际找到的前 5 条候选文字</summary>'
            + render_candidates(result, "production") + '</details>'
            + '<details class="sub"><summary>查看直接向量查找的前 8 条候选文字</summary>'
            + render_candidates(result, "dense") + '</details>'
            + '<details class="sub"><summary>查看这题的原始结果记录</summary>'
            + f'<pre>{esc(json_text(result))}</pre></details>'
        )
    return (
        f'<details class="case" data-search="{esc(search)}"><summary>'
        f'<span class="qid">{esc(case.get("id"))}</span>'
        f'<span class="question">{esc(case.get("question"))}</span>'
        f'<span class="badge">{esc(kind_label)}</span></summary>'
        '<div class="case-body">'
        f'<p class="meta">知识库：{esc(scope.get("label"))}（{esc(scope.get("knowledge_base_id"))}）'
        f' · 类型：{esc(case.get("category"))} · {esc(status)}</p>'
        '<h4>参考答案</h4>'
        f'<p class="answer">{esc(case.get("expected_answer"))}</p>'
        '<h4>文字稿中的参考依据</h4>' + render_evidence(case)
        + '<h4>本次查找结果</h4>' + result_part + '</div></details>'
    )


def render_summary(
    cases: list[dict[str, Any]],
    results: dict[str, dict[str, Any]],
    supplied: bool,
) -> str:
    answerable = sum(bool(case.get("answerable")) for case in cases)
    negatives = len(cases) - answerable
    evaluated = sum(status_ok(results.get(str(case.get("id")))) for case in cases)
    invalid = sum(bool((results.get(str(case.get("id"))) or {}).get("validation_errors")) for case in cases)
    missing = sum(str(case.get("id")) not in results for case in cases)
    other = len(cases) - evaluated - invalid - missing
    cards = (
        f'<div class="card"><strong>{len(cases)}</strong><span>总题数</span></div>'
        f'<div class="card"><strong>{answerable}</strong><span>可回答题</span></div>'
        f'<div class="card"><strong>{negatives}</strong><span>无答案对照题</span></div>'
        f'<div class="card"><strong>{evaluated}</strong><span>有效已运行</span></div>'
    )
    if not supplied:
        return '<div class="cards">' + cards + '</div><p class="notice">当前为考卷预览：没有提供运行结果，所有得分均为“未测”。</p>'
    route_pairs = []
    bypassed = 0
    for case in cases:
        row = results.get(str(case.get("id")))
        if not status_ok(row):
            continue
        expected, actual = case.get("expected_route"), row.get("production_route")
        if case.get("answerable") and expected is not None and actual is not None:
            route_pairs.append(expected == actual)
        bypassed += boolean(row.get("router_bypass")) is True
    errors = sum(not match for match in route_pairs)
    route_text = (
        f'{errors} / {len(route_pairs)} 题（{100 * errors / len(route_pairs):.1f}%）'
        if route_pairs else "未测"
    )
    keys = [
        ("hit_video_at_5", "前 5 条找到正确视频", False),
        ("hit_evidence_at_5", "前 5 条至少找到一条依据", False),
        ("all_evidence_at_5", "前 5 条找到全部参考依据", False),
        ("evidence_coverage_at_5", "前 5 条参考依据平均覆盖率", True),
        ("hit_evidence_at_8", "前 8 条至少找到一条依据", False),
        ("all_evidence_at_8", "前 8 条找到全部参考依据", False),
        ("evidence_coverage_at_8", "前 8 条参考依据平均覆盖率", True),
    ]
    metric_rows = []
    for key, label, numeric in keys:
        cells = [
            '<td>' + esc(ratio(*aggregate(cases, results, kind, key, numeric), numeric=numeric)) + '</td>'
            for kind in ("production", "dense")
        ]
        metric_rows.append(f'<tr><th>{esc(label)}</th>{"".join(cells)}</tr>')
    mrr_cells = []
    for kind in ("production", "dense"):
        total, count = aggregate(cases, results, kind, "mrr_evidence", True)
        mrr_cells.append('<td>' + (f'{total / count:.3f}（{count} 题）' if count else '未测') + '</td>')
    metric_rows.append('<tr><th>首条证据平均排名得分（MRR）</th>' + ''.join(mrr_cells) + '</tr>')
    return (
        '<div class="cards">' + cards + '</div>'
        f'<p>运行情况：有效已运行 {evaluated} 题；校验未通过 {invalid} 题；'
        f'尚未运行 {missing} 题；其他未计分状态 {other} 题。</p>'
        f'<p><strong>可回答题的路由误判：{esc(route_text)}</strong>。'
        f'“路由”就是决定这次提问该走哪种处理流程。'
        f'全部有效题中有 {bypassed} 题未进入正文向量查找，另行绕过路由做了直接查找对照；'
        '这里包含无答案题，未进入检索不一定是错误，例如空知识库无需查找。'
        '无答案题的流程与最终拒答表现仍需单独人工检查。</p>'
        '<div class="table-wrap"><table><thead><tr><th>检查项</th>'
        '<th>当前流程候选</th><th>直接向量查找</th></tr></thead><tbody>'
        + ''.join(metric_rows) + '</tbody></table></div>'
        '<p class="muted">每项分母只包含可回答、有效已运行且记录了该项指标的题目。'
        '未运行、校验失败、缺失指标和无答案题均不当成失败。'
        '其中，已确认被错误分到其他流程的可回答题，在“当前流程候选”中按未进入原文查找计零分，仍保留在分母中；'
        '这样不会只统计成功进入查找流程的题目而高估成绩。'
        '覆盖率先计算每题找到的参考依据比例，再对已测题取平均。MRR 越接近 1，第一条正确依据出现得越靠前。</p>'
    )


def validate_comparison(before: dict[str, Any], after: dict[str, Any]) -> None:
    """Refuse a like-for-like claim when the saved experiment inputs differ."""
    for key, label in (("dataset_sha256", "考卷"),):
        if not before.get(key) or before.get(key) != after.get(key):
            raise ValueError(f"前后{label} SHA256 不一致或未记录，不能直接对比")
    before_hash = (before.get("corpus") or {}).get("sha256")
    after_hash = (after.get("corpus") or {}).get("sha256")
    if not before_hash or before_hash != after_hash:
        raise ValueError("前后资料与知识库范围的 corpus.sha256 不一致或未记录，不能直接对比")
    configuration = before.get("configuration")
    if not isinstance(configuration, dict) or not configuration or configuration != after.get("configuration"):
        raise ValueError("前后模型或检索参数 configuration 不一致或未记录，不能直接对比")


def route_error_count(
    cases: list[dict[str, Any]], results: dict[str, dict[str, Any]],
) -> tuple[float, int]:
    pairs = [
        (case.get("expected_route"), results[str(case["id"])].get("production_route"))
        for case in cases
        if case.get("expected_route") is not None
        and results[str(case["id"])].get("production_route") is not None
    ]
    return sum(expected != actual for expected, actual in pairs), len(pairs)


def comparison_change(case: dict[str, Any], old: dict[str, Any], new: dict[str, Any]) -> str:
    old_values = [boolean(metric_value(old, "production", key)) for key in ("hit_evidence_at_5", "all_evidence_at_5")]
    new_values = [boolean(metric_value(new, "production", key)) for key in ("hit_evidence_at_5", "all_evidence_at_5")]
    expected = case.get("expected_route")
    if expected is not None:
        old_values.append(old.get("production_route") == expected if old.get("production_route") is not None else None)
        new_values.append(new.get("production_route") == expected if new.get("production_route") is not None else None)
    if None in old_values or None in new_values:
        return "未测完整，无法判断"
    differences = [int(right) - int(left) for left, right in zip(old_values, new_values)]
    if not any(differences):
        return "不变"
    if all(value >= 0 for value in differences):
        return "改善"
    if all(value <= 0 for value in differences):
        return "退步"
    return "有升有降"


def render_comparison(
    cases: list[dict[str, Any]], before: dict[str, Any], after: dict[str, Any],
) -> str:
    validate_comparison(before, after)
    old_rows, new_rows = result_rows(before), result_rows(after)
    positives = [case for case in cases if case.get("answerable")]
    paired = [
        case for case in positives
        if status_ok(old_rows.get(str(case["id"]))) and status_ok(new_rows.get(str(case["id"])))
    ]
    table_rows = []
    for key, label in (("hit_evidence_at_5", "前 5 条原文至少找到一条依据"), ("all_evidence_at_5", "前 5 条原文找到全部参考依据")):
        cells = [ratio(*aggregate(paired, rows, "production", key)) for rows in (old_rows, new_rows)]
        table_rows.append(f'<tr><th>{esc(label)}</th><td>{esc(cells[0])}</td><td>{esc(cells[1])}</td></tr>')
    route_cells = [ratio(*route_error_count(paired, rows)) for rows in (old_rows, new_rows)]
    table_rows.append(f'<tr><th>内容题误分流（越少越好）</th><td>{esc(route_cells[0])}</td><td>{esc(route_cells[1])}</td></tr>')
    details = []
    for case in positives:
        old, new = old_rows.get(str(case["id"])), new_rows.get(str(case["id"]))
        measured = status_ok(old) and status_ok(new)
        change = comparison_change(case, old, new) if measured else "未同时有效运行，不计入对比"
        cells = []
        for row in (old, new):
            cells.append('<td>' + esc(route_label((row or {}).get("production_route"))) + '</td>')
        for row in (old, new):
            values = [metric_text(metric_value(row, "production", key)) if measured else "未测" for key in ("hit_evidence_at_5", "all_evidence_at_5")]
            cells.append('<td>' + esc(" / ".join(values)) + '</td>')
        details.append(f'<tr><th>{esc(case["id"])} · {esc(case.get("question"))}</th>{"".join(cells)}<td>{esc(change)}</td></tr>')
    negatives = []
    for case in cases:
        if case.get("answerable"):
            continue
        cells = []
        for rows in (old_rows, new_rows):
            row = rows.get(str(case["id"])) or {}
            status = "已查找，待人工核查" if status_ok(row) else str(row.get("status", "未运行"))
            cells.append('<td>' + esc(f'{status}；流程：{route_label(row.get("production_route"))}；范围内已入库视频：{row.get("scope_done_video_count", "未记录")}；候选原文：{len(candidate_list(row, "production"))} 条') + '</td>')
        negatives.append(f'<tr><th>{esc(case["id"])} · {esc(case.get("question"))}</th>{"".join(cells)}</tr>')
    excluded = len(positives) - len(paired)
    return (
        '<section class="panel"><h2>修复前后对比</h2>'
        '<p class="good">已核对：同一份考卷、同一批资料与知识库范围、相同模型和检索参数，可以直接对比。</p>'
        '<p>本次改动是问题处理流程的判断。保存的实验记录显示未更换模型、检索参数或题库。'
        f'对比包含两轮均有效运行的 {len(paired)} / {len(positives)} 道可回答题，另有 {excluded} 道未同时有效运行，已排除。'
        '被分到其他流程、未查找原文的题目按零分计入分母；无答案题不参与以下得分。</p>'
        '<div class="table-wrap"><table><thead><tr><th>检查项</th><th>修复前</th><th>修复后</th></tr></thead><tbody>'
        + ''.join(table_rows) + '</tbody></table></div>'
        '<details class="sub"><summary>逐题对比处理流程与原文依据</summary>'
        '<p class="muted">依据两列依次显示“至少一条 / 全部参考依据”。“改善 / 退步”同时比较这两项和流程是否正确；'
        '各项都相同为“不变”，同时出现升降为“有升有降”。缺少指标的题目显示未测，且不纳入该项分母。</p>'
        '<div class="table-wrap"><table><thead><tr><th>题目</th><th>修复前流程</th><th>修复后流程</th>'
        '<th>修复前依据</th><th>修复后依据</th><th>变化</th></tr></thead><tbody>'
        + ''.join(details) + '</tbody></table></div></details>'
        '<details class="sub"><summary>无答案对照题：仅列资料查找与范围状态</summary>'
        '<p class="notice">这些题没有生成 AI 回答，不能据此声称回答正确或拒答成功。候选数量不代表相关性，需人工查看原文。</p>'
        '<div class="table-wrap"><table><thead><tr><th>题目</th><th>修复前</th><th>修复后</th></tr></thead><tbody>'
        + ''.join(negatives) + '</tbody></table></div></details>'
        f'<p class="meta">考卷 SHA256：{esc(after.get("dataset_sha256"))}<br>'
        f'资料 SHA256：{esc((after.get("corpus") or {}).get("sha256"))}</p></section>'
    )


CSS = """
:root{color-scheme:light;--ink:#2e302c;--muted:#6b7068;--line:#dce3d8;--green:#20674a;--bg:#f5f7f3}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:16px/1.7 system-ui,-apple-system,"Segoe UI","Microsoft YaHei",sans-serif}
main{max-width:1180px;margin:0 auto;padding:36px 24px 70px}h1{font-size:30px;line-height:1.3;margin:0 0 12px}h2{font-size:23px;margin:30px 0 14px}h4{margin:20px 0 8px}h5{font-size:16px;margin:0 0 6px}p{margin:9px 0}a{color:var(--green)}
.intro,.panel{background:white;border:1px solid var(--line);border-radius:16px;padding:24px;margin-bottom:20px}.muted,.meta{color:var(--muted)}.meta{font-size:14px}.notice{background:#fff5d9;border-left:4px solid #bd892c;padding:12px 16px;border-radius:6px}.cards{display:grid;grid-template-columns:repeat(4,1fr);gap:12px}.card{background:#f0f5ed;border:1px solid var(--line);border-radius:12px;padding:14px}.card strong{display:block;font-size:29px;color:var(--green)}.card span{font-size:14px}.table-wrap{overflow-x:auto}table{width:100%;border-collapse:collapse;margin:18px 0;background:white}th,td{text-align:left;vertical-align:top;padding:11px 13px;border-bottom:1px solid var(--line)}thead{background:#edf3e9}tbody th{font-weight:500}input{width:100%;font:inherit;padding:12px 15px;border:1px solid #b7c7b2;border-radius:9px;background:white;margin:8px 0 16px}.case{background:white;border:1px solid var(--line);border-radius:12px;margin:12px 0;overflow:hidden}.case>summary{padding:17px 18px;cursor:pointer;display:flex;align-items:baseline;gap:12px;list-style:none}.case>summary::before{content:"＋";color:var(--green)}.case[open]>summary::before{content:"－"}.qid{font-weight:700;white-space:nowrap;color:var(--green)}.question{flex:1}.badge{font-size:12px;white-space:nowrap;background:#edf3e9;border-radius:20px;padding:3px 9px;color:#44603b}.case-body{padding:0 22px 22px;border-top:1px solid var(--line)}.answer{white-space:pre-wrap}.evidence,.candidate{border:1px solid var(--line);border-radius:8px;padding:15px;margin:12px 0;background:#fafbf9}blockquote{margin:10px 0;padding:8px 15px;border-left:3px solid #93ad87;white-space:pre-wrap;overflow-wrap:anywhere}.sub{margin:14px 0;border:1px solid var(--line);border-radius:8px;padding:12px}.sub>summary{cursor:pointer;font-weight:600}.good{color:var(--green);font-weight:600}.error{color:#9b2626;background:#fff1ee}pre{white-space:pre-wrap;overflow-wrap:anywhere;font:14px/1.75 ui-monospace,Consolas,"Microsoft YaHei",monospace;max-width:100%;margin:10px 0;padding:12px;background:#f1f4ef;border-radius:6px}code{overflow-wrap:anywhere}footer{font-size:13px;color:var(--muted);margin-top:30px}[hidden]{display:none!important}
@media(max-width:650px){main{padding:20px 12px 40px}.intro,.panel{padding:16px}h1{font-size:25px}.cards{grid-template-columns:repeat(2,1fr)}.case>summary{padding:14px 12px;flex-wrap:wrap;gap:8px}.question{flex-basis:75%}.badge{margin-left:31px}.case-body{padding:0 13px 16px}th,td{padding:9px;font-size:14px}}
@media print{body{background:white}main{max-width:none;padding:0}.case,.panel,.intro{break-inside:avoid}input{display:none}.case>summary::before{content:""}}
"""


def render_html(
    dataset: dict[str, Any], baseline: dict[str, Any] | None,
    before: dict[str, Any] | None = None,
) -> str:
    cases = dataset["cases"]
    results = result_rows(baseline)
    evidence_videos = {item["video_id"] for case in cases for item in case.get("evidence", [])}
    usable_videos = (baseline or {}).get("corpus", {}).get("done_video_count", len(evidence_videos))
    body = ''.join(render_case(case, results.get(str(case.get('id')))) for case in cases)
    if before is not None and baseline is None:
        raise ValueError("前后对比必须同时提供修复后的 --results")
    comparison = render_comparison(cases, before, baseline) if before is not None else ""
    return (
        '<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        '<title>视频知识库 · 第一套检索考卷与基线报告</title><style>' + CSS + '</style></head><body><main>'
        '<header class="intro"><p class="meta">DouyinMind · 本地检索评测</p>'
        '<h1>先看资料找得准不准</h1>'
        '<p>这份报告只检查两件事：问题是否走对处理流程，以及系统是否找到了能支持答案的原文。'
        '<strong>没有测最终 AI 回答，也没有测摘要压缩后是否丢失信息。</strong></p>'
        f'<p>本次资料包含 {esc(usable_videos)} 个可用视频，考题涉及其中 {len(evidence_videos)} 个视频。部分项目名或命令只出现在画面里，'
        '没有被文字稿记录，因此没有拿来出题。时间表示原文字稿片段的起止位置。</p>'
        '<p>“找到正确视频”只说明方向对了；“找到支持答案的原文”才说明拿到了答题依据。'
        '一题需要多条依据时，“至少一条命中”和“全部依据找到”分开统计。</p>'
        '<p>“当前流程候选”是在现有处理流程下取得的资料；“直接向量查找”跳过流程判断，'
        '直接按问题与文字的含义相近程度找资料，用来区分是流程判断出错，还是查找本身不准。'
        '相似度分数不是答案正确的概率。</p>'
        '<p>这是根据真实视频文字稿拟定的第一版题库，评分以现有文字稿为依据；尚未逐题回看原视频。后续继续加入实际使用时问过的问题。</p>'
        '<p class="notice">无答案对照题要由人工检查找到的文字是否确实无关，'
        '并在后续检查 AI 是否明确说明没有找到依据。本报告没有生成答案，不能自动判这些题回答准确。</p>'
        '</header>' + comparison + '<section class="panel"><h2>本次成绩</h2>'
        + render_summary(cases, results, baseline is not None) + '</section>'
        '<section><h2>逐题查看</h2><label for="filter">输入题号、问题关键词或知识库名称筛选</label>'
        '<input id="filter" type="search" placeholder="例如：Q01、Git、111" autocomplete="off">'
        '<p id="visible-count" class="meta" aria-live="polite"></p>' + body + '</section>'
        '<footer>此文件可离线打开；筛选只在当前页面进行，不发送资料、不调用任何接口。'
        '点击题目可展开参考答案、原话与全部候选文字；原始结果保留供核对。'
        f'考卷版本：{esc(dataset.get("schema_version", "未记录"))}。</footer></main>'
        '<script>const input=document.getElementById("filter");'
        'const cases=Array.from(document.querySelectorAll(".case"));'
        'function filterCases(){const key=input.value.trim().toLocaleLowerCase();let count=0;'
        'for(const item of cases){const show=(item.dataset.search||"").toLocaleLowerCase().includes(key);'
        'item.hidden=!show;if(show)count++;}'
        'document.getElementById("visible-count").textContent="显示 "+count+" / "+cases.length+" 题";}'
        'input.addEventListener("input",filterCases);filterCases();</script></body></html>'
    )


def excel_safe(value: Any) -> str:
    text = str(value if value is not None else "")
    # A leading apostrophe makes spreadsheet applications treat risky cells as text.
    if text and (text[0] in "\t\r\n" or text.lstrip().startswith(("=", "+", "-", "@"))):
        return "'" + text
    return text


def write_csv(path: Path, dataset: dict[str, Any], baseline: dict[str, Any] | None) -> None:
    results = result_rows(baseline)
    headers = [
        "题号", "问题", "知识库编号", "知识库名称", "类别", "是否可回答", "预期流程",
        "实际流程", "运行状态", "当前流程跳过原文查找", "参考答案", "参考依据（含时间）", "校验错误",
        "当前流程找到正确视频@5", "当前流程至少一条依据@5", "当前流程全部依据@5",
        "当前流程依据覆盖率@5", "直接查找找到正确视频@5", "直接查找至少一条依据@5",
        "直接查找全部依据@5", "直接查找依据覆盖率@5", "当前流程候选原文", "直接查找候选原文",
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(headers)
        for case in dataset["cases"]:
            row = results.get(str(case.get("id"))) or {}
            scope = case.get("scope") or {}
            values: list[Any] = [
                case.get("id"), case.get("question"), scope.get("knowledge_base_id"),
                scope.get("label"), case.get("category"), "是" if case.get("answerable") else "否",
                route_label(case.get("expected_route")) if case.get("answerable") else "无答案对照，不评预期流程",
                route_label(row.get("production_route")), row.get("status", "尚未运行"),
                row.get("router_bypass", "未记录"), case.get("expected_answer"),
                json_text(case.get("evidence") or []), json_text(row.get("validation_errors") or []),
            ]
            for kind in ("production", "dense"):
                for key in ("hit_video_at_5", "hit_evidence_at_5", "all_evidence_at_5", "evidence_coverage_at_5"):
                    raw = metric_value(row, kind, key) if status_ok(row) and case.get("answerable") else None
                    values.append("未测" if raw is None else raw)
            values.extend(json_text(candidate_list(row, kind)) for kind in ("production", "dense"))
            writer.writerow(excel_safe(value) for value in values)


def read_document(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8-sig") as handle:
        document = json.load(handle)
    if not isinstance(document, dict):
        raise ValueError(f"JSON 顶层必须是对象：{path}")
    return document


def validate_results(
    dataset_path: Path,
    dataset: dict[str, Any],
    baseline: dict[str, Any],
) -> None:
    expected_hash = hashlib.sha256(dataset_path.read_bytes()).hexdigest()
    if baseline.get("dataset_sha256") != expected_hash:
        raise ValueError("结果中的 dataset_sha256 与当前考卷文件不一致，不能生成混用不同考卷的报告")
    raw = baseline.get("cases")
    if isinstance(raw, dict):
        ids = []
        for key, value in raw.items():
            if not isinstance(value, dict) or str(value.get("id", key)) != str(key):
                raise ValueError("结果的 cases 对象必须按题号保存，且行内 id 必须与键一致")
            ids.append(str(key))
    elif isinstance(raw, list):
        if any(not isinstance(row, dict) or row.get("id") is None for row in raw):
            raise ValueError("结果的每一行必须是含 id 的对象")
        ids = [str(row["id"]) for row in raw]
    else:
        raise ValueError("结果 JSON 必须有 cases 数组或按题号保存的对象")
    if len(set(ids)) != len(ids):
        raise ValueError("结果中存在重复题号")
    unknown = set(ids) - {str(case["id"]) for case in dataset["cases"]}
    if unknown:
        raise ValueError("结果中含考卷以外的题号：" + "、".join(sorted(unknown)))


def main() -> int:
    parser = argparse.ArgumentParser(description="生成离线中文检索考卷 / 评测 HTML 和 Excel 可打开的 CSV")
    parser.add_argument("--dataset", type=Path, required=True, help="本地考卷 JSON")
    parser.add_argument("--results", type=Path, help="本地基线结果 JSON；省略则只显示考卷")
    parser.add_argument("--before", type=Path, help="可选，修复前结果 JSON；与 --results 的修复后结果对比")
    parser.add_argument("--output", type=Path, required=True, help="完整 HTML 输出路径")
    parser.add_argument("--csv", type=Path, help="可选，UTF-8 BOM 编码 CSV 输出路径")
    args = parser.parse_args()
    try:
        dataset = read_document(args.dataset)
        if not isinstance(dataset.get("cases"), list):
            raise ValueError("考卷 JSON 必须有 cases 数组")
        ids = [str(case.get("id")) for case in dataset["cases"] if isinstance(case, dict)]
        if len(ids) != len(dataset["cases"]) or len(set(ids)) != len(ids) or "None" in ids:
            raise ValueError("每道题必须是对象，且 id 不可缺失或重复")
        baseline = read_document(args.results) if args.results else None
        if baseline is not None:
            validate_results(args.dataset, dataset, baseline)
        before = read_document(args.before) if args.before else None
        if before is not None:
            if baseline is None:
                raise ValueError("--before 必须与修复后的 --results 一起使用")
            validate_results(args.dataset, dataset, before)
            validate_comparison(before, baseline)
        protected = {args.dataset.resolve()}
        if args.results:
            protected.add(args.results.resolve())
        if args.before:
            protected.add(args.before.resolve())
        if args.output.resolve() in protected or (args.csv and args.csv.resolve() in protected):
            raise ValueError("输出文件不能覆盖考卷或原始结果")
        if args.csv and args.csv.resolve() == args.output.resolve():
            raise ValueError("HTML 和 CSV 输出路径不能相同")
        html_document = render_html(dataset, baseline, before)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(html_document, encoding="utf-8")
        if args.csv:
            write_csv(args.csv, dataset, baseline)
    except (OSError, ValueError, TypeError) as error:
        parser.exit(1, f"无法生成报告：{error}\n")
    print(f"HTML 已生成：{args.output.resolve()}")
    if args.csv:
        print(f"CSV 已生成：{args.csv.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
