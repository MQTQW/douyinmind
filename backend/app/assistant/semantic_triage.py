"""On-demand DeepSeek failure triage text. It never changes task state or sends.

The model only receives fixed structural failure facts and may only produce a
short user-facing explanation plus a next step. Every task status, permission,
send and retry decision stays in deterministic code; this module is display text.
"""
from __future__ import annotations

import json
import re

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
    return {
        "state": task.get("state"),
        "reason_code": task.get("reason_code"),
        "kind": task.get("kind"),
        "reply_state": task.get("reply_state"),
        "import_state": task.get("import_state"),
        "has_reply_draft": bool(task.get("details", {}).get("reply_text")),
        "library_choice_state": (task.get("details", {}).get("library_choice") or {}).get("state"),
    }


def triage_messages(facts):
    system = (
        "你是抖音助手的失败分诊说明器。只根据给定的结构化失败事实，用中文向提问账号解释"
        "当前发生了什么、下一步最好做什么。要求：不超过120字；不得声称已经保存、已入库或"
        "已发送；不得编造平台验证码/账号状态；不得给出与你无法控制的发送、重试、权限相关"
        "的承诺；不要输出URL、密钥或具体评论原文；只输出一段普通文本。"
    )
    return [{"role": "system", "content": system},
            {"role": "user", "content": json.dumps(facts, ensure_ascii=False, sort_keys=True)}]


def _fallback_text(facts):
    state, reason = facts.get("state"), facts.get("reason_code") or ""
    if state == "waiting_library" or reason == "readback_confirmed_unsent":
        return "这条回复还没有发出。可以按页面允许的操作重新处理一次，或标记为未提交后重新在评论区发送指令。"
    if state == "verification_required":
        return "抖音要求在发表前由本人完成安全验证。可先只读核对，或标记为未提交后重新发送指令。"
    if state == "delivery_unknown":
        return "发送结果暂时无法确认，系统不会自动重发。可以先核查任务状态，保留原记录。"
    if state == "failed_no_submit" or reason == "operator_confirmed_unsent":
        return "本次没有提交内容。如需结果，请重新在评论区 @ 助手发送一次指令。"
    if reason == "original_notification_candidates_exceed_limit":
        return "原评论定位遇到多条相同提醒。可以稍后只读核对，或重新发一条评论指令再试。"
    return "这次任务没有完成，原记录已保留。可以按页面按钮做只读核对或重新发送指令。"


def explain(task, *, model, budget):
    """Return {'text', 'fallback'} for a failure-state task; never mutates it."""
    facts = _facts(task)
    request_key = "triage:" + str(task.get("id"))
    try:
        budget.reserve(task.get("id"), request_key, TRIAGE_ESTIMATE_TOKENS)
    except Exception:
        return {"text": _fallback_text(facts), "fallback": True}
    try:
        turn = model.complete(triage_messages(facts), [])
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
        if not 1 <= len(text) <= TRIAGE_MAX_CHARS or any(word in text for word in _FORBIDDEN_CLAIMS):
            return {"text": _fallback_text(facts), "fallback": True}
        return {"text": text, "fallback": False}
    except Exception:
        return {"text": _fallback_text(facts), "fallback": True}


def fallback_for(task):
    return _fallback_text(_facts(task))


def applicable(task):
    return isinstance(task, dict) and task.get("state") in _FAILURE_STATES
