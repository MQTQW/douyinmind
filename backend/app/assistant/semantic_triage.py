"""On-demand DeepSeek failure triage text. It never changes task state or sends.

The model only receives fixed structural failure facts and may only produce a
short user-facing explanation plus a next step. Every task status, permission,
send and retry decision stays in deterministic code; this module is display text.
"""
from __future__ import annotations

import hashlib
import json
import re

from .usage_budget import reservation_pending

TRIAGE_ESTIMATE_TOKENS = 900
TRIAGE_MAX_CHARS = 600
_FAILURE_STATES = frozenset({"needs_review", "needs_reconcile", "verification_required",
                             "delivery_unknown", "failed", "failed_no_submit", "waiting_library"})
_FORBIDDEN_CLAIMS = ("已保存", "已入库", "已发送", "已发表", "已经保存", "已经发出", "已经入库")


class TriageError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def _facts(task):
    details = task.get('details') if isinstance(task.get('details'), dict) else {}
    choice = details.get('library_choice') if isinstance(details.get('library_choice'), dict) else {}
    return {
        "state": task.get("state"),
        "reason_code": task.get("reason_code"),
        "kind": task.get("kind"),
        "reply_state": task.get("reply_state"),
        "import_state": task.get("import_state"),
        "has_reply_draft": bool(details.get("reply_text")),
        "library_choice_state": choice.get("state"),
        'execution_attempts': task.get('execution_attempts', 0),
        'publish_attempts': task.get('publish_attempts', 0),
    }


def context_key(task):
    return hashlib.sha256(json.dumps(_facts(task), sort_keys=True,
        separators=(',', ':')).encode('utf-8')).hexdigest()


def cached_text(task):
    details = task.get('details') if isinstance(task.get('details'), dict) else {}
    body = details.get('semantic_triage')
    if (applicable(task) and isinstance(body, str) and body.strip()
            and details.get('semantic_triage_context_key') == context_key(task)):
        return body
    return None


def triage_messages(facts):
    system = (
        "你是抖音助手的失败分诊说明器。只根据给定的结构化失败事实，用中文向提问账号解释"
        "当前发生了什么、下一步最好做什么。请控制在120字以内，避免声称已经保存、已入库或"
        "已发送。不得编造平台验证码/账号状态，不得给出与你无法控制的发送、重试、权限相关"
        "的承诺。执行尝试超过一次时，避免再次建议直接重新处理，按页面可用操作引导核查。"
        "不要输出URL、密钥或具体评论原文。只输出一段普通文本。"
    )
    return [{"role": "system", "content": system},
            {"role": "user", "content": json.dumps(facts, ensure_ascii=False, sort_keys=True)}]


def _fallback_text(facts):
    state, reason = facts.get("state"), facts.get("reason_code") or ""
    if state == "waiting_library":
        return "请在原评论线程回复本次要保存到的知识库名称，确认选择后继续保存。"
    if state == "verification_required":
        return "抖音要求在发表前由本人完成安全验证。可先只读核对，或标记为未提交后重新发送指令。"
    if state == "delivery_unknown":
        return "发送结果暂时无法确认，系统不会自动重发。可以先核查任务状态，保留原记录。"
    if state == "failed_no_submit" or reason == "operator_confirmed_unsent":
        return "本次没有提交内容。如需结果，请重新在评论区 @ 助手发送一次指令。"
    if facts.get('execution_attempts', 0) > 1:
        return "已记录多次处理尝试，请先查看处理记录，再按页面允许的操作核对结果或结束本次任务。"
    if reason == "readback_confirmed_unsent":
        return "这条回复还没有发出。可以按页面允许的操作继续处理，或标记为未提交后重新在评论区发送指令。"
    if reason == "original_notification_candidates_exceed_limit":
        return "原评论定位遇到多条相同提醒。可以稍后只读核对，或重新发一条评论指令再试。"
    return "这次任务没有完成，原记录已保留。可以按页面按钮做只读核对或重新发送指令。"


def explain(task, *, model, budget):
    """Return {'text', 'fallback'} for a failure-state task; never mutates it."""
    facts = _facts(task)
    request_key = "triage:" + str(task.get("id")) + ':' + context_key(task)
    try:
        reservation = budget.reserve(task.get("id"), request_key, TRIAGE_ESTIMATE_TOKENS)
    except Exception:
        return {"text": _fallback_text(facts), "fallback": True}
    if not isinstance(reservation, dict) or reservation.get('may_request') is not True:
        if reservation_pending(reservation):
            return {'text': '这条任务的说明正在生成，请稍后刷新查看。', 'fallback': True, 'pending': True}
        return {'text': '上次说明请求的结果尚待核对，请刷新记录查看。', 'fallback': True}
    try:
        turn = model.complete(triage_messages(facts), [])
    except Exception:
        try:
            budget.mark_unknown(request_key)
        except Exception:
            pass
        return {'text': _fallback_text(facts), 'fallback': True}
    try:
        try:
            budget.settle(request_key, turn.usage)
        except Exception:
            try:
                budget.mark_unknown(request_key)
            except Exception:
                pass
        message = turn.message if isinstance(turn.message, dict) else {}
        text = message.get("content")
        if not isinstance(text, str):
            raise TriageError("triage_response_invalid")
        text = re.sub(r"\s+", " ", text).strip()
        if (not 1 <= len(text) <= TRIAGE_MAX_CHARS or any(word in text for word in _FORBIDDEN_CLAIMS)
                or facts['execution_attempts'] > 1 and any(word in text for word in ('重新处理', '重试'))):
            return {"text": _fallback_text(facts), "fallback": True}
        return {"text": text, "fallback": False}
    except Exception:
        return {"text": _fallback_text(facts), "fallback": True}


def fallback_for(task):
    return _fallback_text(_facts(task))


def applicable(task):
    return isinstance(task, dict) and task.get("state") in _FAILURE_STATES
