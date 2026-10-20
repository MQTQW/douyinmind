#!/usr/bin/env python3
"""Render an offline final-answer assessment from saved runs and human reviews.

Only reads local JSON. It does not invoke the application, models, or a browser.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import html
import json
from collections import Counter
from pathlib import Path
from typing import Any


LABELS = {
    "complete": "关键点完整且有据",
    "partial": "缺少关键信息",
    "incorrect": "含无据内容或与资料不符",
    "abstained_correctly": "正确说明没有依据",
    "unsupported_answer": "无依据仍给具体答案",
    "unreviewed": "尚未评阅",
    "error": "生成失败，未计分",
    "not_generated": "尚未生成，未计分",
}
POSITIVE_VERDICTS = {"complete", "partial", "incorrect"}
NEGATIVE_VERDICTS = {"abstained_correctly", "unsupported_answer"}
CHANGE_LABELS = {"improved": "改善", "regressed": "退步", "unchanged": "不变", "uncompared": "未比较"}


def esc(value: Any) -> str:
    return html.escape(str(value if value is not None else ""), quote=True)


def pretty(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2)


def pre(value: Any) -> str:
    return "<pre>" + esc(value if isinstance(value, str) else pretty(value)) + "</pre>"


def read_document(path: Path) -> tuple[dict[str, Any], str]:
    raw = path.read_bytes()
    document = json.loads(raw.decode("utf-8-sig"))
    if not isinstance(document, dict):
        raise ValueError(f"{path.name} 顶层必须是 JSON 对象")
    return document, hashlib.sha256(raw).hexdigest()


def rows_by_id(document: dict[str, Any], label: str) -> dict[str, dict[str, Any]]:
    raw = document.get("cases", [])
    if isinstance(raw, dict):
        rows = []
        for key, value in raw.items():
            if not isinstance(value, dict):
                raise ValueError(f"{label} 的 cases 必须包含对象")
            if "id" in value and str(value["id"]) != str(key):
                raise ValueError(f"{label} 的题号与映射键不一致：{key}")
            rows.append({**value, "id": str(key)})
    elif isinstance(raw, list):
        rows = raw
    else:
        raise ValueError(f"{label} 的 cases 必须是列表或按题号映射的对象")
    result = {}
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("id"), str) or not row["id"].strip():
            raise ValueError(f"{label} 包含缺失或无效题号")
        if row["id"] in result:
            raise ValueError(f"{label} 包含重复题号：{row['id']}")
        result[row["id"]] = row
    return result


def load_inputs(
    dataset_path: Path, answers_path: Path, review_path: Path,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], dict[str, str]]:
    dataset, dataset_sha = read_document(dataset_path)
    answers, answers_sha = read_document(answers_path)
    review, review_sha = read_document(review_path)
    fingerprint = answers.get("fingerprint") or {}
    if not isinstance(fingerprint, dict):
        raise ValueError("回答文件的 fingerprint 必须是对象")
    recorded_dataset_sha = fingerprint.get("dataset_sha256", answers.get("dataset_sha256"))
    for actual, expected, label in (
        (recorded_dataset_sha, dataset_sha, "回答文件与考卷"),
        (review.get("dataset_sha256"), dataset_sha, "评阅文件与考卷"),
        (review.get("answers_sha256"), answers_sha, "评阅文件与回答文件"),
    ):
        if actual != expected:
            raise ValueError(f"{label}的 SHA-256 不匹配，拒绝生成报告")
    cases = rows_by_id(dataset, "考卷")
    answer_rows = rows_by_id(answers, "回答文件")
    reviews = rows_by_id(review, "评阅文件")
    if not cases:
        raise ValueError("考卷没有题目")
    for label, rows in (("回答文件", answer_rows), ("评阅文件", reviews)):
        unknown = set(rows) - set(cases)
        if unknown:
            raise ValueError(f"{label}包含考卷之外的题号：{', '.join(sorted(unknown))}")
    for case_id, case in cases.items():
        if not isinstance(case.get("answerable"), bool):
            raise ValueError(f"{case_id} 的 answerable 必须是布尔值")
        if case_id in reviews:
            allowed = POSITIVE_VERDICTS if case["answerable"] else NEGATIVE_VERDICTS
            if reviews[case_id].get("verdict") not in allowed:
                raise ValueError(f"{case_id} 的评阅分类不适用于该题类型")
    return dataset, answers, review, {
        "dataset_sha256": dataset_sha,
        "answers_sha256": answers_sha,
        "review_sha256": review_sha,
    }


def effective_verdict(answer: dict[str, Any] | None, review: dict[str, Any] | None) -> str:
    if answer is None or answer.get("status") not in {"completed", "error"}:
        return "not_generated"
    if answer.get("status") == "error":
        return "error"
    return str(review["verdict"]) if review is not None else "unreviewed"


def summary(
    cases: dict[str, dict[str, Any]], answers: dict[str, dict[str, Any]],
    reviews: dict[str, dict[str, Any]],
) -> dict[str, Counter[str]]:
    result = {"positive": Counter(), "negative": Counter()}
    for case_id, case in cases.items():
        counter = result["positive" if case["answerable"] else "negative"]
        counter["total"] += 1
        answer = answers.get(case_id)
        if answer is not None and answer.get("status") == "completed":
            counter["completed"] += 1
        verdict = effective_verdict(answer, reviews.get(case_id))
        counter[verdict] += 1
        if verdict in POSITIVE_VERDICTS | NEGATIVE_VERDICTS:
            counter["reviewed"] += 1
            if reviews[case_id].get("general_answer_provided"):
                counter["general_supplement"] += 1
            if reviews[case_id].get("minor_issues"):
                counter["minor_issue_cases"] += 1
            if case["answerable"]:
                for point in reviews[case_id].get("required_points", []):
                    if isinstance(point, dict) and (point.get("covered") is True or point.get("covered") is False):
                        counter["required_points"] += 1
                        counter["covered_points"] += point["covered"] is True
                if reviews[case_id].get("unsupported_claims"):
                    counter["unsupported_cases"] += 1
    return result


def validate_comparison(before: dict[str, Any], after: dict[str, Any]) -> None:
    """Compare runs only when their data and configured model parameters match.

    Source hashes intentionally may differ: the comparison measures a code/prompt
    repair, not a change of exam, source material, or configured model parameters.
    Each review's binding to its own answer file is checked by load_inputs first.
    """
    old, new = before.get("fingerprint"), after.get("fingerprint")
    if not isinstance(old, dict) or not isinstance(new, dict):
        raise ValueError("前后回答必须记录 fingerprint，拒绝生成对照报告")
    for key, label in (
        ("dataset_sha256", "考卷"), ("corpus_sha256", "资料快照"),
        ("retrieval_configuration", "检索参数"), ("answer_configuration", "回答模型与参数"),
    ):
        if not old.get(key) or not new.get(key) or old[key] != new[key]:
            raise ValueError(f"前后{label}指纹缺失或不一致，拒绝生成对照报告")
    for run in (before, after):
        corpus = run.get("corpus")
        if isinstance(corpus, dict) and corpus.get("sha256") is not None:
            if corpus["sha256"] != run["fingerprint"]["corpus_sha256"]:
                raise ValueError("资料快照与其 fingerprint 不一致，拒绝生成对照报告")
    if old.get("index") is not None and new.get("index") is not None and old["index"] != new["index"]:
        raise ValueError("前后实际检索索引指纹不一致，拒绝生成对照报告")


def covered_points(review: dict[str, Any] | None) -> tuple[int, int]:
    points = (review or {}).get("required_points") or []
    decided = [p for p in points if isinstance(p, dict) and isinstance(p.get("covered"), bool)]
    return sum(p["covered"] is True for p in decided), len(decided)


def case_change(
    case: dict[str, Any], before_answer: dict[str, Any] | None, before_review: dict[str, Any] | None,
    answer: dict[str, Any] | None, review: dict[str, Any] | None,
) -> str:
    old, new = effective_verdict(before_answer, before_review), effective_verdict(answer, review)
    allowed = POSITIVE_VERDICTS if case["answerable"] else NEGATIVE_VERDICTS
    if old not in allowed or new not in allowed:
        return "uncompared"
    ranks = {"complete": 3, "partial": 2, "incorrect": 1,
             "abstained_correctly": 2, "unsupported_answer": 1}
    old_score, new_score = (ranks[old], 0.0), (ranks[new], 0.0)
    old_covered, old_total = covered_points(before_review)
    new_covered, new_total = covered_points(review)
    # Use coverage only as a tie-breaker when both reviews assessed the same
    # number of points. Missing assessment must never look like a regression.
    if case["answerable"] and old_total and old_total == new_total:
        old_score, new_score = (ranks[old], old_covered / old_total), (ranks[new], new_covered / new_total)
    return "improved" if new_score > old_score else "regressed" if new_score < old_score else "unchanged"


def render_comparison_summary(
    cases: dict[str, dict[str, Any]], before_answers: dict[str, dict[str, Any]],
    before_reviews: dict[str, dict[str, Any]], answers: dict[str, dict[str, Any]],
    reviews: dict[str, dict[str, Any]],
) -> str:
    old, new = summary(cases, before_answers, before_reviews), summary(cases, answers, reviews)
    changes = Counter(case_change(case, before_answers.get(cid), before_reviews.get(cid),
                                  answers.get(cid), reviews.get(cid)) for cid, case in cases.items())
    rows = []
    metrics = (
        ("有答案题 · 已评题数 / 考卷题数", "positive", "reviewed", "total"),
        ("有答案题 · 必要知识点覆盖", "positive", "covered_points", "required_points"),
        ("有答案题 · 关键点完整且有据 / 已评题数", "positive", "complete", "reviewed"),
        ("有答案题 · 有无据扩写记录 / 已评题数", "positive", "unsupported_cases", "reviewed"),
        ("有答案题 · 缺少关键信息", "positive", "partial", None),
        ("有答案题 · 含无据内容或与资料不符", "positive", "incorrect", None),
        ("无答案题 · 已评题数 / 考卷题数", "negative", "reviewed", "total"),
        ("无答案题 · 正确说明没有依据 / 已评题数", "negative", "abstained_correctly", "reviewed"),
        ("无答案题 · 无依据仍给具体答案", "negative", "unsupported_answer", None),
    )
    for label, group, numerator, denominator in metrics:
        def display(stats: dict[str, Counter[str]]) -> str:
            c = stats[group]
            return f"{c[numerator]} / {c[denominator]}" if denominator else str(c[numerator])
        rows.append(f'<tr><th>{esc(label)}</th><td>{esc(display(old))}</td><td>{esc(display(new))}</td></tr>')
    for group, title in (("positive", "有答案题"), ("negative", "无答案题")):
        for key in ("unreviewed", "error", "not_generated"):
            rows.append(f'<tr><th>{esc(title + " · " + LABELS[key])}</th>'
                        f'<td>{old[group][key]}</td><td>{new[group][key]}</td></tr>')
    for kind, label in (("compression", "生成式摘要调用次数"), ("answer", "最终回答调用次数")):
        def calls(answer_rows: dict[str, dict[str, Any]]) -> int:
            return sum(isinstance(call, dict) and call.get("kind") == kind
                       for answer in answer_rows.values()
                       for call in (answer["model_calls"] if isinstance(answer.get("model_calls"), list) else []))
        rows.append(f'<tr><th>{label}（实际调用记录）</th><td>{calls(before_answers)}</td><td>{calls(answers)}</td></tr>')
    return (
        '<section class="intro"><h2>修复前后对照</h2><p>前后使用相同考卷、资料快照、检索参数及回答模型配置。'
        '源代码允许变化，本次比较的是资料处理与回答链路修复后的效果。统计按实际生成与评阅结果计算，不预设提升幅度。</p>'
        '<div class="table-wrap"><table><thead><tr><th>检查项</th><th>修复前</th><th>修复后</th></tr></thead><tbody>'
        + ''.join(rows) + '</tbody></table></div><p>'
        + ' · '.join(f'{esc(label)}：{changes[key]} 题' for key, label in CHANGE_LABELS.items())
        + '</p><p class="muted">逐题变化先比较评阅分类（有据完整优于关键点不全，关键点不全优于含无据内容；'
        '无答案题正确说明无依据优于无依据给答案）；分类相同时，仅在双方均评了相同数量的关键点时比较覆盖。'
        '“不变”指分类与上述覆盖结果不变，不表示措辞完全相同。未评、生成失败、尚未生成均单列，不能当作答错或改善。</p>'
        '<p class="muted">“有无据扩写记录”统计评阅中 unsupported_claims 非空的有答案题；'
        '它与整体结论分开记录。必要知识点分母来自各轮已评关键点，不会将未评点算作已覆盖。'
        '配置中的摘要参数是保留的默认设置，是否实际生成摘要以上述 model_calls 调用记录为准。</p></section>'
    )


def render_source_comparison(before: dict[str, Any], after: dict[str, Any]) -> str:
    old, new = before["fingerprint"], after["fingerprint"]
    checks = [("考卷 SHA-256", old["dataset_sha256"], new["dataset_sha256"]),
              ("资料快照 SHA-256", old["corpus_sha256"], new["corpus_sha256"]),
              ("检索参数", old["retrieval_configuration"], new["retrieval_configuration"]),
              ("回答模型与参数", old["answer_configuration"], new["answer_configuration"])]
    if old.get("index") is not None or new.get("index") is not None:
        checks.append(("实际检索索引", old.get("index"), new.get("index")))
    rows = ''.join('<tr><th>' + esc(label) + '</th><td>' + pre(a) + '</td><td>' + pre(b) + '</td></tr>'
                   for label, a, b in checks)
    old_sources, new_sources = old.get("source_code_sha256") or {}, new.get("source_code_sha256") or {}
    code_rows = []
    if not isinstance(old_sources, dict) or not isinstance(new_sources, dict):
        raise ValueError("source_code_sha256 必须是对象")
    for name in sorted(set(old_sources) | set(new_sources)):
        old_hash, new_hash = old_sources.get(name), new_sources.get(name)
        status = "相同" if old_hash == new_hash else "已变化（本轮链路修复允许）"
        code_rows.append(f'<tr><th>{esc(name)}</th><td class="quote">{esc(old_hash or "未记录")}</td>'
                         f'<td class="quote">{esc(new_hash or "未记录")}</td><td>{esc(status)}</td></tr>')
    return ('<details class="provenance"><summary>对照条件与代码变化（完整指纹）</summary>'
            '<p>输入文件内容及评阅绑定关系分别通过 SHA-256 校验。以下考卷、资料与配置必须一致；'
            '代码指纹允许不同，不会把链路修复误认成更换模型。</p><div class="table-wrap"><table><thead><tr>'
            '<th>对照条件</th><th>修复前</th><th>修复后</th></tr></thead><tbody>' + rows
            + '</tbody></table></div><h3>逐文件源代码指纹</h3><div class="table-wrap"><table><thead><tr>'
            '<th>文件</th><th>修复前 SHA-256</th><th>修复后 SHA-256</th><th>变化</th></tr></thead><tbody>'
            + (''.join(code_rows) if code_rows else '<tr><td colspan="4">双方均未记录源代码指纹，无法确认代码变化。</td></tr>')
            + '</tbody></table></div></details>')


def render_summary(stats: dict[str, Counter[str]]) -> str:
    tables = []
    for key, title, verdicts, success in (
        ("positive", "有答案题：关键信息与资料忠实度", ("complete", "partial", "incorrect"), "complete"),
        ("negative", "无答案对照题：能否说明资料不足", ("abstained_correctly", "unsupported_answer"), "abstained_correctly"),
    ):
        c = stats[key]
        ratio = f"{c[success]} / {c['reviewed']}" if c["reviewed"] else "未评，不计算比例"
        percentage = f"（{100 * c[success] / c['reviewed']:.1f}%）" if c["reviewed"] else ""
        table_rows = [("考卷题数", c["total"]), ("已成功生成回答", c["completed"]), ("已评题数", c["reviewed"])]
        if key == "positive":
            table_rows.append(("必要知识点覆盖（与有无无据扩写分开评）", f"{c['covered_points']} / {c['required_points']}"))
        table_rows.append(("另有轻微措辞或冗余说明的题数", c["minor_issue_cases"]))
        if key == "negative":
            table_rows.append(("另外给了已区分来源的通用补充", c["general_supplement"]))
        table_rows += [(LABELS[v], c[v]) for v in verdicts]
        table_rows += [(LABELS[v], c[v]) for v in ("unreviewed", "error", "not_generated")]
        tables.append(
            '<section class="summary"><h2>' + esc(title) + '</h2><p class="score">'
            + esc(ratio + percentage) + '</p><p class="muted">'
            + ("关键点完整且未发现实质无据内容 / 已评有答案题" if key == "positive" else "正确说明没有依据 / 已评无答案题")
            + '</p><table><tbody>'
            + ''.join(f'<tr><th>{esc(label)}</th><td>{esc(count)}</td></tr>' for label, count in table_rows)
            + '</tbody></table></section>'
        )
    return '<div class="summary-grid">' + ''.join(tables) + '</div>'


def timestamp(value: Any) -> str:
    if not isinstance(value, (float, int)) or isinstance(value, bool) or value < 0:
        return "时间未记录"
    minutes, seconds = divmod(float(value), 60)
    return f"{int(minutes):02d}:{seconds:05.2f}"


def render_evidence(case: dict[str, Any]) -> str:
    rows = case.get("evidence") or []
    if not rows:
        return '<p class="muted">无答案对照题，没有指定支持答案的参考原文。</p>'
    parts = []
    for i, item in enumerate(rows, 1):
        if not isinstance(item, dict):
            parts.append(pre(item))
            continue
        parts.append(
            f'<article class="evidence"><h4>依据 {i} · {esc(item.get("title"))}</h4>'
            f'<p class="meta">视频：{esc(item.get("video_id"))} · '
            f'{esc(timestamp(item.get("start_seconds")))} — {esc(timestamp(item.get("end_seconds")))}</p>'
            f'<blockquote>{esc(item.get("quote"))}</blockquote></article>'
        )
    return ''.join(parts)


def render_review(review: dict[str, Any] | None) -> str:
    if review is None:
        return '<p class="muted">尚未评阅，不按回答错误处理，也不进入正确率分母。</p>'
    parts = [f'<p>{esc(review.get("explanation"))}</p>']
    points = review.get("required_points") or []
    if points:
        rows = []
        for point in points:
            if not isinstance(point, dict):
                rows.append(f'<tr><td colspan="4">{esc(point)}</td></tr>')
                continue
            covered = point.get("covered")
            status = "已覆盖" if covered is True else "未覆盖" if covered is False else "未判定"
            rows.append(
                f'<tr><td>{esc(point.get("text"))}</td><td>{status}</td>'
                f'<td class="quote">{esc(point.get("answer_quote"))}</td><td>{esc(point.get("notes"))}</td></tr>'
            )
        parts.append('<h4>关键点逐项核查</h4><div class="table-wrap"><table><thead><tr>'
                     '<th>应包含的信息</th><th>覆盖情况</th><th>实际回答原句</th><th>说明</th>'
                     '</tr></thead><tbody>' + ''.join(rows) + '</tbody></table></div>')
    for key, label in (("missing_points", "遗漏的关键信息"), ("citation_issues", "引用问题")):
        values = review.get(key) or []
        parts.append(f'<h4>{label}</h4>' + (
            '<ul>' + ''.join(f'<li>{esc(v)}</li>' for v in values) + '</ul>'
            if values else '<p class="muted">未记录此类问题。</p>'
        ))
    claims = review.get("unsupported_claims") or []
    parts.append('<h4>缺少原文支持的说法</h4>')
    if claims:
        for claim in claims:
            if isinstance(claim, dict):
                parts.append('<article class="warning"><blockquote>' + esc(claim.get("answer_quote"))
                             + '</blockquote><p>' + esc(claim.get("reason")) + '</p><p class="meta">'
                             + esc(claim.get("evidence_status")) + '</p></article>')
            else:
                parts.append(pre(claim))
    else:
        parts.append('<p class="muted">未记录；这不等同于证明回答的每句话都正确。</p>')
    if review.get("context_analysis") is not None:
        parts.append('<h4>找到的资料与最终回答之间的关系</h4>' + pre(review["context_analysis"]))
    if review.get("minor_issues"):
        parts.append('<h4>轻微措辞与冗余说明（保留供核查）</h4>')
        for issue in review["minor_issues"]:
            if isinstance(issue, dict):
                parts.append('<article class="warning"><blockquote>' + esc(issue.get("answer_quote"))
                             + '</blockquote><p>' + esc(issue.get("note", issue.get("notes", ""))) + '</p></article>')
            else:
                parts.append(pre(issue))
    return ''.join(parts)


def answer_prompt(answer: dict[str, Any]) -> dict[str, Any] | None:
    calls = answer.get("model_calls") or []
    if isinstance(calls, list):
        for call in reversed(calls):
            if isinstance(call, dict) and call.get("kind") == "answer":
                return call
    value = answer.get("final_prompt")
    return value if isinstance(value, dict) else None


def render_answer_text(answer: dict[str, Any] | None) -> str:
    if answer is None:
        return '<p class="muted">没有这道题的生成记录。</p>'
    parts = [f'<p class="meta">运行状态：{esc(answer.get("status"))} · '
             f'实际流程：{esc(answer.get("production_route", answer.get("route_type", "未记录")))} · '
             f'用时：{esc(answer.get("latency_ms", "未记录"))} 毫秒</p>', pre(answer.get("answer", ""))]
    if answer.get("raw_answer") is not None and answer["raw_answer"] != answer.get("answer"):
        parts.append('<details><summary>模型原始回答（完整）</summary>' + pre(answer["raw_answer"]) + '</details>')
    if answer.get("status") == "error":
        parts.append('<p class="warning">本题生成失败，不进入评分分母；详见下面的调用记录。</p>')
    return ''.join(parts)


def render_case(
    case: dict[str, Any], answer: dict[str, Any] | None, review: dict[str, Any] | None,
    before_answer: dict[str, Any] | None = None, before_review: dict[str, Any] | None = None,
    comparison: bool = False,
) -> str:
    scope = case.get("scope") or {}
    scope_label = scope.get("label", "未记录") if isinstance(scope, dict) else str(scope)
    verdict = effective_verdict(answer, review)
    group = "positive" if case["answerable"] else "negative"
    search = ' '.join(str(x) for x in (case["id"], case.get("question"), scope_label, case.get("category")))
    status_class = "good" if verdict in {"complete", "abstained_correctly"} else "bad" if verdict in {"incorrect", "unsupported_answer"} else "warning"
    change = case_change(case, before_answer, before_review, answer, review) if comparison else ""
    change_class = "good" if change == "improved" else "bad" if change == "regressed" else "warning"
    change_pill = f'<span class="pill {change_class}">{esc(CHANGE_LABELS[change])}</span> ' if comparison else ""
    parts = [
        f'<details class="case" data-search="{esc(search.casefold())}" data-group="{group}" data-verdict="{verdict}" data-change="{change}">',
        f'<summary>{change_pill}<span class="pill {status_class}">{esc(LABELS[verdict])}</span> '
        f'<strong>{esc(case["id"])}</strong> · {esc(case.get("question"))}</summary>',
        '<div class="case-body">',
        f'<p class="meta">知识库：{esc(scope_label)} · 类型：{esc(case.get("category"))} · '
        f'{"有答案题" if case["answerable"] else "无答案对照题"}</p>',
        '<h3>题目与参考答案</h3>' + pre(case.get("question", "")),
        '<p><strong>参考答案：</strong></p>' + pre(case.get("expected_answer", "")),
        render_evidence(case),
        '<h3>实际完整回答</h3>',
    ]
    if comparison:
        old_verdict = effective_verdict(before_answer, before_review)
        parts.append('<div class="answer-comparison"><section><h4>修复前 · '
                     + esc(LABELS[old_verdict]) + '</h4>' + render_answer_text(before_answer)
                     + '</section><section><h4>修复后 · ' + esc(LABELS[verdict]) + '</h4>'
                     + render_answer_text(answer) + '</section></div>')
        parts.append('<details><summary>修复前人工对照结论（完整）</summary>'
                     + render_review(before_review) + '</details>')
    else:
        parts.append(render_answer_text(answer))
    parts.append('<h3>' + ('修复后逐题对照结论' if comparison else '逐题对照结论') + '</h3>' + render_review(review))
    if answer is not None:
        prompt = answer_prompt(answer)
        parts.append('<details><summary>模型实际收到的最终输入与资料（全文）</summary>')
        if prompt is None:
            parts.append('<p class="muted">没有记录最终回答模型的输入，不能据此判断资料是否在压缩时丢失。</p>')
        else:
            parts.append('<h4>系统要求</h4>' + pre(prompt.get("system_prompt", "")))
            parts.append('<h4>用户输入（含提供给模型的资料）</h4>' + pre(prompt.get("user_prompt", "")))
        parts.append('</details>')
        for key, label in (("sources", "显示给用户的来源"), ("dense_hits", "原始检索候选"),
                           ("retrieved_hits", "其他检索记录"), ("trace", "执行过程"), ("model_calls", "模型调用与资料压缩记录")):
            if key in answer:
                parts.append(f'<details><summary>{label}（完整记录）</summary>' + pre(answer[key]) + '</details>')
    parts.append('</div></details>')
    return ''.join(parts)


CSS = """
:root{color-scheme:light;--ink:#28302d;--muted:#67736d;--line:#d8e1da;--accent:#25674b}
*{box-sizing:border-box}body{margin:0;background:#f3f6f2;color:var(--ink);font:16px/1.7 system-ui,'Microsoft YaHei',sans-serif}
main{max-width:1180px;margin:36px auto;padding:0 22px 64px}h1{font-size:30px;line-height:1.35}h2{font-size:21px}h3{font-size:18px;margin-top:26px}h4{margin:16px 0 8px}
.intro,.summary,.toolbar,.case,.provenance{background:white;border:1px solid var(--line);border-radius:14px;padding:20px;margin:18px 0}
.notice,.warning{background:#fff5de;border-radius:8px;padding:12px}.intro p:first-child{margin-top:0}.muted,.meta{color:var(--muted);font-size:14px}.summary-grid{display:grid;grid-template-columns:1fr 1fr;gap:18px}.summary{margin:0}.score{font-size:28px;font-weight:700;color:var(--accent);margin:8px 0 0}
table{width:100%;border-collapse:collapse;font-size:14px}td,th{text-align:left;vertical-align:top;border-bottom:1px solid var(--line);padding:9px 10px}th{font-weight:600}.summary td{text-align:right}.table-wrap{overflow-x:auto}
.toolbar{position:sticky;top:8px;z-index:1;box-shadow:0 3px 16px #1a382410}.toolbar label{display:inline-block;margin:4px 14px 4px 0}input,select{font:inherit;padding:7px;border:1px solid #b8c8bc;border-radius:6px;max-width:100%}input{width:280px}.case{padding:0}.case>summary{padding:17px;cursor:pointer;list-style-position:inside}.case-body{padding:0 22px 24px;border-top:1px solid var(--line)}details details{border:1px solid var(--line);padding:12px;margin:12px 0;border-radius:8px}details details summary{cursor:pointer;font-weight:600}pre{white-space:pre-wrap;overflow-wrap:anywhere;word-break:break-word;font:14px/1.75 ui-monospace,Consolas,monospace;background:#f5f7f5;padding:14px;border-radius:8px;tab-size:2;max-width:100%}.quote,blockquote{white-space:pre-wrap;overflow-wrap:anywhere}blockquote{border-left:3px solid #9db7a7;margin:8px 0;padding:6px 14px;background:#f5f8f4}.pill{display:inline-block;border-radius:16px;font-size:13px;padding:2px 10px;margin-right:8px}.good{background:#e5f2e7;color:#235e39}.bad{background:#fce6e2;color:#942c25}.pill.warning{padding:2px 10px;color:#805600}.evidence{border:1px solid var(--line);padding:14px;border-radius:8px;margin:12px 0}.evidence h4{margin-top:0}[hidden]{display:none!important}
.answer-comparison{display:grid;grid-template-columns:minmax(0,1fr) minmax(0,1fr);gap:18px}.answer-comparison>section{min-width:0}.provenance td.quote{word-break:break-all}.provenance pre{min-width:180px}
@media(max-width:760px){main{padding:0 12px 30px;margin-top:20px}.summary-grid,.answer-comparison{grid-template-columns:1fr}.toolbar{position:static}.case-body{padding:0 14px 18px}h1{font-size:25px}}@media print{.toolbar{display:none}.case,.summary,.intro{break-inside:avoid}body{background:white}}
"""

JS = """
const query=document.getElementById('query'),group=document.getElementById('group'),verdict=document.getElementById('verdict'),change=document.getElementById('change');
function filter(){const q=query.value.trim().toLocaleLowerCase();let n=0;for(const row of document.querySelectorAll('.case')){const show=(!q||row.dataset.search.includes(q))&&(!group.value||row.dataset.group===group.value)&&(!verdict.value||row.dataset.verdict===verdict.value)&&(!change||!change.value||row.dataset.change===change.value);row.hidden=!show;if(show)n++;}document.getElementById('count').textContent='当前显示 '+n+' 题';}
for(const element of [query,group,verdict,change].filter(Boolean)){element.addEventListener('input',filter);element.addEventListener('change',filter);}
document.getElementById('expand').addEventListener('click',()=>{for(const row of document.querySelectorAll('.case:not([hidden])'))row.open=true;});
document.getElementById('collapse').addEventListener('click',()=>{for(const row of document.querySelectorAll('.case'))row.open=false;});filter();
"""


def render_report(
    dataset: dict[str, Any], answers: dict[str, Any], review: dict[str, Any], hashes: dict[str, str],
    before_answers: dict[str, Any] | None = None, before_review: dict[str, Any] | None = None,
    before_hashes: dict[str, str] | None = None,
) -> str:
    comparison = before_answers is not None
    if comparison != (before_review is not None):
        raise ValueError("修复前回答与评阅必须同时提供")
    if comparison:
        validate_comparison(before_answers, answers)
    before_answer_rows = rows_by_id(before_answers, "修复前回答文件") if comparison else {}
    before_reviews = rows_by_id(before_review, "修复前评阅文件") if comparison else {}
    cases = rows_by_id(dataset, "考卷")
    answer_rows = rows_by_id(answers, "回答文件")
    reviews = rows_by_id(review, "评阅文件")
    stats = summary(cases, answer_rows, reviews)
    options = ''.join(f'<option value="{key}">{esc(label)}</option>' for key, label in LABELS.items())
    fingerprint = answers.get("fingerprint") or {}
    provenance = {"文件内容指纹（SHA-256）": hashes, "运行模式": answers.get("mode"),
                  "运行指纹与配置": fingerprint, "资料快照": answers.get("corpus"),
                  "补充配置": answers.get("configuration"), "逐题评阅方法": review.get("method"),
                  "边界题交叉裁定": review.get("adjudications", []), "实际修复迭代记录": review.get("iteration_notes", [])}
    if comparison:
        provenance["修复前文件内容指纹（SHA-256）"] = before_hashes
        provenance["修复前人工评阅方法"] = before_review.get("method")
        provenance["修复前边界题交叉裁定"] = before_review.get("adjudications", [])
    comparison_html = render_comparison_summary(cases, before_answer_rows, before_reviews, answer_rows, reviews) if comparison else ""
    change_filter = ('<label>前后变化 <select id="change"><option value="">全部</option>'
                     + ''.join(f'<option value="{key}">{esc(label)}</option>' for key, label in CHANGE_LABELS.items())
                     + '</select></label>') if comparison else ""
    title = "最终回答修复前后准确性对照" if comparison else "最终回答准确性与依据核查"
    return (
        '<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        '<meta http-equiv="Content-Security-Policy" content="default-src \'none\'; style-src \'unsafe-inline\'; script-src \'unsafe-inline\'; connect-src \'none\'; img-src \'none\'; base-uri \'none\'; form-action \'none\'">'
        '<title>' + esc(title) + '</title><style>' + CSS + '</style></head><body><main>'
        '<h1>' + esc(title) + '</h1>'
        '<section class="intro"><p>使用同一套检索考卷，记录本次每题的一次真实生成，再由本助手逐条对照现有文字稿与参考证据，并交叉复核边界题。'
        '有答案题检查回答是否正确、是否漏掉关键信息；无答案题检查是否明确说明资料不足。</p>'
        '<p>本报告中的“幻觉”（资料无依据却给具体事实）按知识库原文核查；外部世界中可能正确的常识，也不能代替库内依据。'
        '来源链接存在，不代表链接中的原文支持回答。</p>'
        '<p>关键点覆盖与资料忠实度分开评分：答到了重点，仍可能夹带没有依据的产品功能、数字、路径或操作规则。'
        '“含无据内容”仅表示当前资料没有支持，不等于已证实该说法在外部世界错误。合理的同义解释、浅层可选建议及明确区分的通用补充，不按捏造扣分。</p>'
        '<p class="notice">评分范围是本次生成与现有文字稿。文字稿本身可能有转写错误；本次结果不保证未来每次回答相同。'
        '缺失评阅、尚未生成、生成失败分别展示，均不算答错。两个题型的比例分别计算。</p></section>'
        + comparison_html
        + ('<h2>修复后本轮结果</h2>' if comparison else '') + render_summary(stats)
        + '<section class="toolbar"><label>查题号或题目 <input id="query" placeholder="例如 Q01、N04、知识库名称"></label>'
        '<label>题型 <select id="group"><option value="">全部</option><option value="positive">有答案题</option><option value="negative">无答案对照</option></select></label>'
        '<label>结论 <select id="verdict"><option value="">全部</option>' + options + '</select></label>'
        + change_filter +
        '<button id="expand" type="button">展开当前题目</button> <button id="collapse" type="button">收起全部</button>'
        '<p class="muted" id="count" aria-live="polite"></p></section>'
        + ''.join(render_case(case, answer_rows.get(case_id), reviews.get(case_id),
                              before_answer_rows.get(case_id), before_reviews.get(case_id), comparison)
                  for case_id, case in cases.items())
        + (render_source_comparison(before_answers, answers) if comparison else '')
        + '<details class="provenance"><summary>数据来源、文件指纹与运行配置</summary>' + pre(provenance) + '</details>'
        '<p class="muted">这是离线报告；筛选只在本页内运行，不发送网络请求。原文、回答与调用记录按纯文字展示，均保留全文。</p>'
        '</main><script>' + JS + '</script></body></html>'
    )


def csv_safe(value: Any) -> str:
    text = value if isinstance(value, str) else pretty(value)
    if text.lstrip(" \t\r\n\ufeff").startswith(("=", "+", "-", "@")) or text.startswith(("\t", "\r", "\n")):
        return "'" + text
    return text


def write_csv(path: Path, dataset: dict[str, Any], answers: dict[str, Any], review: dict[str, Any]) -> None:
    cases, answer_rows, reviews = (rows_by_id(d, label) for d, label in (
        (dataset, "考卷"), (answers, "回答文件"), (review, "评阅文件")))
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(["题号", "题型", "知识库", "题目", "运行状态", "结论", "参考答案", "实际完整回答", "关键点核查", "遗漏信息", "缺少依据的说法", "引用问题", "人工说明", "参考证据", "最终模型输入"])
        for case_id, case in cases.items():
            answer, assessment = answer_rows.get(case_id), reviews.get(case_id)
            scope = case.get("scope") or {}
            writer.writerow([csv_safe(value) for value in (
                case_id, "有答案题" if case["answerable"] else "无答案对照题",
                scope.get("label", "") if isinstance(scope, dict) else scope,
                case.get("question", ""), (answer or {}).get("status", "未运行"),
                LABELS[effective_verdict(answer, assessment)], case.get("expected_answer", ""),
                (answer or {}).get("answer", ""), (assessment or {}).get("required_points", []),
                (assessment or {}).get("missing_points", []), (assessment or {}).get("unsupported_claims", []),
                (assessment or {}).get("citation_issues", []), (assessment or {}).get("explanation", ""),
                case.get("evidence", []), answer_prompt(answer or {}) or {},
            )])


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--answers", type=Path, required=True)
    parser.add_argument("--review", type=Path, required=True)
    parser.add_argument("--before-answers", type=Path, help="可选：修复前回答文件，需同时提供 --before-review")
    parser.add_argument("--before-review", type=Path, help="可选：修复前人工评阅，需绑定修复前回答的内容指纹")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--csv", type=Path)
    args = parser.parse_args()
    if bool(args.before_answers) != bool(args.before_review):
        parser.error("--before-answers 和 --before-review 必须同时提供")
    inputs = {p.resolve() for p in (args.dataset, args.answers, args.review, args.before_answers, args.before_review) if p}
    if args.output.resolve() in inputs or (args.csv and args.csv.resolve() in inputs):
        parser.error("输出路径不能覆盖考卷、回答或评阅输入文件")
    if args.csv and args.output.resolve() == args.csv.resolve():
        parser.error("HTML 与 CSV 输出路径不能相同")
    try:
        dataset, answers, review, hashes = load_inputs(args.dataset, args.answers, args.review)
        before_answers = before_review = before_hashes = None
        if args.before_answers:
            _, before_answers, before_review, before_hashes = load_inputs(args.dataset, args.before_answers, args.before_review)
        report = render_report(dataset, answers, review, hashes, before_answers, before_review, before_hashes)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(report, encoding="utf-8")
        if args.csv:
            write_csv(args.csv, dataset, answers, review)
    except (OSError, ValueError, TypeError, KeyError) as error:
        parser.exit(1, f"报告未生成：{error}\n")
    print(f"HTML: {args.output}")
    if args.csv:
        print(f"CSV: {args.csv}")


if __name__ == "__main__":
    main()
