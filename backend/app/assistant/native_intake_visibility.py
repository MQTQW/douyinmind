"""Read-only owner-side projection of native intake before queue admission.

The web API consumes this safe snapshot; it never initializes or opens a native
receiver store. A held source is not evidence that no paid work occurred.
"""
from __future__ import annotations

import json
import math
import re
from datetime import datetime

from .queue import utc

_MESSAGES = {
    "native_reply_parent_unresolved": "无法确认这条回复对应的原线程。",
    "native_followup_ui_unverified": "尚未在当前页面核实这条回复。",
    "native_ui_unverified": "尚未在当前页面核实原评论。",
    "native_scope_changed": "账号、资料范围或原线程条件已变化。",
    "native_unknown_lease": "上次处理结果需要核对，记录已保留。",
    "native_identity_conflict": "通知账号信息存在冲突，需要核对。",
    "native_payload_conflict": "同一通知的内容存在冲突，需要核对。",
}
_SOURCE = re.compile(r"native:[a-f0-9]{64}")
_STATES = {"held", "held_conflict"}


def _count(value):
    if type(value) is not int or not 0 <= value <= 1000000000:
        raise ValueError("invalid_native_intake_snapshot")
    return value


def _timestamp(value):
    if type(value) is not str or not 10 <= len(value) <= 40:
        raise ValueError("invalid_native_intake_snapshot")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise ValueError("invalid_native_intake_snapshot") from None
    if parsed.tzinfo is None:
        raise ValueError("invalid_native_intake_snapshot")
    return value


def _excerpt(candidate):
    if type(candidate) is not dict:
        raise ValueError("invalid_native_intake_snapshot")
    text = candidate.get("content")
    if type(text) is not str:
        return ""
    return " ".join("".join(ch for ch in text if ch.isprintable() or ch.isspace()).split())[:160]


def project_native_intake(value):
    """Accept only the bounded public shape, never plans, auth, UIDs or tokens."""
    if type(value) is not dict or type(value.get("available")) is not bool:
        raise ValueError("invalid_native_intake_snapshot")
    observed = _timestamp(value.get("observed_at"))
    if not value["available"]:
        return {"available": False, "observed_at": observed,
                "message": "收件明细暂时无法读取，已有记录保留。"}
    result = {"available": True, "observed_at": observed,
              "unresolved_count": _count(value.get("unresolved_count")),
              "pending_count": _count(value.get("pending_count")), "recent": []}
    recent = value.get("recent")
    if type(recent) is not list or len(recent) > 10 or len(recent) > result["unresolved_count"]:
        raise ValueError("invalid_native_intake_snapshot")
    for item in recent:
        if (type(item) is not dict or type(item.get("id")) is not str
                or not _SOURCE.fullmatch(item["id"]) or item.get("state") not in _STATES):
            raise ValueError("invalid_native_intake_snapshot")
        code = item.get("reason_code")
        code = code if type(code) is str and re.fullmatch(r"[a-z_]{1,80}", code) else "native_intake_unconfirmed"
        text = item.get("comment_excerpt", "")
        if type(text) is not str or len(text) > 160:
            raise ValueError("invalid_native_intake_snapshot")
        when = _timestamp(item.get("created_at"))
        result["recent"].append({"id": item["id"], "state": item["state"],
            "reason_code": code, "message": _MESSAGES.get(code, "原评论或受理条件需要核对。"),
            "comment_excerpt": text, "created_at": when})
    return result


def summarize_native_intake(store):
    """Read the already-owned store; never claim, initialize, recover or retry."""
    with store._connection() as db:
        held = db.execute("SELECT COUNT(*) FROM sources WHERE historical=0 AND task_id IS NULL "
                          "AND state IN ('held','held_conflict')").fetchone()[0]
        pending = db.execute("SELECT COUNT(*) FROM sources WHERE historical=0 AND task_id IS NULL "
                             "AND state IN ('fresh','claimed','planned')").fetchone()[0]
        rows = db.execute("SELECT source_id,state,hold_code,first_seen,candidate FROM sources "
                          "WHERE historical=0 AND task_id IS NULL AND state IN ('held','held_conflict') "
                          "ORDER BY first_seen DESC,source_id LIMIT 10").fetchall()
        recent = []
        for row in rows:
            seen = row["first_seen"]
            if type(seen) not in (int, float) or not math.isfinite(seen) or seen < 0:
                raise ValueError("invalid_native_intake_snapshot")
            recent.append({"id": row["source_id"], "state": row["state"],
                "reason_code": row["hold_code"], "created_at": utc(seen),
                "comment_excerpt": _excerpt(json.loads(row["candidate"]))})
    return project_native_intake({"available": True, "observed_at": utc(),
        "unresolved_count": held, "pending_count": pending, "recent": recent})


def record_native_intake(control, run_id, lane):
    store = getattr(lane, "source_store", None)
    if store is None:
        return None
    try:
        summary = summarize_native_intake(store)
    except Exception:
        summary = {"available": False, "observed_at": utc()}
    # The control's current-run check remains authoritative.
    return control.set_native_intake(run_id, summary)
