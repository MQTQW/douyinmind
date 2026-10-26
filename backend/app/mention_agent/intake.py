"""Turn a verified public UI snapshot into a scoped agent intake envelope.

No model, browser, credential, application database or sender is opened here.
The existing preview inbox is deliberately not an execution source. A UI
reference is only a bounded observation: it must be reacquired before a future
write, and cannot distinguish otherwise identical repeated platform comments.
"""
from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timezone
import hashlib
import json
import re
from typing import Any

from app.mention_receiver.browser import canonical_profile


_VIDEO_ID = re.compile(r"\d{5,30}\Z")
_SOURCE_MODES = frozenset({"live_ui", "replay", "isolated"})


def _value(binding: Any, key: str) -> Any:
    return binding.get(key) if isinstance(binding, Mapping) else getattr(binding, key, None)


def _text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} is required")
    return value.strip()


def _profile(value: Any) -> str:
    return canonical_profile(_text(value, "public profile URL"))


def _binding(binding: Any) -> dict[str, Any]:
    if _value(binding, "active") is not True:
        raise ValueError("An active binding is required")
    result = {key: _text(_value(binding, key), key) for key in ("binding_id", "instance_id")}
    for key in ("sender_profile_url", "receiver_profile_url"):
        result[key] = _profile(_value(binding, key))
    if result["sender_profile_url"] == result["receiver_profile_url"]:
        raise ValueError("The assistant cannot be its own bound sender")
    library = _value(binding, "knowledge_base_id")
    query_scope = _value(binding, "query_scope")
    if query_scope == "all_local":
        if library is not None:
            raise ValueError("An all-local query cannot borrow a knowledge base ID")
    elif query_scope in (None, "library"):
        if (isinstance(library, bool) or not isinstance(library, (str, int))
                or (isinstance(library, int) and library <= 0)
                or (isinstance(library, str) and not library.strip())):
            raise ValueError("A bound knowledge base is required")
    else:
        raise ValueError("Unknown bound query scope")
    if not isinstance(_value(binding, "public_reply_allowed"), bool):
        raise ValueError("Public reply permission must be explicit")
    return result


def _normal(value: str) -> str:
    return re.sub(r"\s+", "", value)


def _identifier(namespace: str, value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return namespace + ":" + hashlib.sha256(encoded).hexdigest()


def should_receive_notice(notice: Mapping[str, Any], binding: Any) -> bool:
    """Accept any nonempty public comment notice from the explicitly bound A.

    This is an eligibility filter, not proof of a real @ or authorization to
    execute a tool. The detail snapshot must subsequently pass make_envelope.
    """
    try:
        scoped = _binding(binding)
        _text(notice.get("exact_text"), "notification text")
        return _profile(notice.get("source_profile_url")) == scoped["sender_profile_url"]
    except (AttributeError, TypeError, ValueError):
        return False


def make_envelope(notice: Mapping[str, Any], detail: Mapping[str, Any], binding: Any,
                  source_mode: str = "live_ui") -> dict[str, Any]:
    """Validate fresh detail evidence; preserve explicit non-live provenance.

    Replay/isolated envelopes exist for offline model and storage tests. They
    must remain non-live through storage and can never authorize real sending.
    """
    if not isinstance(source_mode, str) or source_mode not in _SOURCE_MODES:
        raise ValueError("Unknown intake source mode")
    scoped = _binding(binding)
    if not should_receive_notice(notice, binding):
        raise ValueError("The notice is not from the active bound sender")
    if source_mode == "live_ui":
        _text(notice.get("semantic_fingerprint"), "complete live notification fingerprint")
    if not isinstance(detail, Mapping) or detail.get("ok") is not True:
        raise ValueError("A successful detail snapshot is required")
    evidence = detail.get("evidence")
    if not isinstance(evidence, Mapping):
        raise ValueError("Structured public detail evidence is required")
    for flag in ("real_mention", "sender_verified", "video_verified"):
        if detail.get(flag) is not True or evidence.get(flag) is not True:
            raise ValueError(f"Missing verified detail evidence: {flag}")
    for value in (detail.get("unique_count"), evidence.get("unique_count")):
        if type(value) is not int or value != 1:
            raise ValueError("The original comment must be uniquely matched")
    for key in ("sender_profile_url", "receiver_profile_url"):
        if (_profile(detail.get(key)) != scoped[key]
                or _profile(evidence.get(key)) != scoped[key]):
            raise ValueError("Comment identity differs from the binding")
    video_id = detail.get("video_id")
    if not isinstance(video_id, str) or not _VIDEO_ID.fullmatch(video_id):
        raise ValueError("A complete public video ID is required")
    if evidence.get("video_id") != video_id:
        raise ValueError("Video evidence differs from the detail")
    if detail.get("source_url") != "https://www.douyin.com/video/" + video_id:
        raise ValueError("The visible video source is inconsistent")
    comment = _text(detail.get("command_text"), "complete comment text")
    evidence_text = _text(evidence.get("command_text"), "evidence comment text")
    if comment != evidence_text or _normal(comment) != _normal(_text(notice.get("exact_text"), "notification text")):
        raise ValueError("The notification and original comment differ")
    native_id = detail.get("native_comment_id")
    if native_id is not None and (not isinstance(native_id, str) or not _VIDEO_ID.fullmatch(native_id)):
        raise ValueError("Invalid public native comment ID")
    if evidence.get("native_comment_id") != native_id or detail.get("native_comment_id_conflict"):
        raise ValueError("Native comment identity is inconsistent")
    diagnostics = detail.get("diagnostics")
    if (not isinstance(diagnostics, Mapping) or diagnostics.get("id_conflict") is not False
            or diagnostics.get("command_match_count") != 1
            or diagnostics.get("visible_video_ids") != [video_id]):
        raise ValueError("The original-comment snapshot has conflicting or missing diagnostics")
    quality = "native" if native_id is not None else "verified_ui"
    # Never use wall time, transient pre_index, display name or knowledge-base
    # name as source identity. This is local deduplication, not exactly-once.
    reference_id = _identifier("reference", {
        "receiver": scoped["receiver_profile_url"], "sender": scoped["sender_profile_url"],
        "video": video_id, "native_comment_id": native_id,
        "ui_body": None if native_id is not None else _normal(comment),
    })
    command_id = _identifier("command", {
        "binding_id": scoped["binding_id"], "instance_id": scoped["instance_id"],
        "reference_id": reference_id, "comment_text": comment, "source_mode": source_mode,
    })
    return {
        "command_id": command_id, "reference_id": reference_id,
        "reference_quality": quality, "native_comment_id": native_id,
        "sender_profile_url": scoped["sender_profile_url"],
        "receiver_profile_url": scoped["receiver_profile_url"],
        "video_id": video_id, "comment_text": comment, "source_mode": source_mode,
        "evidence": {
            "real_mention": True, "sender_verified": True, "video_verified": True,
            "unique_count": 1, "observed_at_utc": datetime.now(timezone.utc).isoformat(),
        },
    }
