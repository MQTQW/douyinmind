"""First-run setup for a fresh local assistant installation.

A fresh install has no receiver configuration. The web service must present that
as "setup required" rather than "unreadable legacy configuration", and the user
must be able to provide the public assistant identity from the normal local page.

This module only reads/writes the public receiver identity
(``receiver.local.json``: public Douyin number and canonical public profile) and
reports which setup steps remain. It never starts the assistant, enables intake,
imports a legacy ledger or overwrites an existing receiver binding.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import tempfile

from app.mention_agent.contracts import public_profile

from .queue import QueueError

CONFIG_NAME = "receiver.local.json"
IDENTITY_NAME = "receiver-identity.json"
INBOX_NAME = "inbox.sqlite3"
PUBLIC_NUMBER = re.compile(r"[0-9]{5,30}")
MAX_CONFIG_BYTES = 8192

#: Fixed user-facing reasons. Unknown exceptions are never projected as-is.
SETUP_REASONS = {
    "assistant_setup_required": "首次使用还需要填写助手 B 的公开抖音号与主页。",
    "assistant_receiver_configuration_required": "本机已有助手记录，但接收器配置缺失；请核对原有设置，不会自动覆盖。",
    "assistant_receiver_configuration_invalid": "接收器配置无效，请核对原有设置。",
}


def _absolute_directory(value: Path, *, create: bool) -> Path:
    """Return an absolute, non-redirected directory, optionally creating it."""
    path = Path(os.path.abspath(value))
    if path.resolve() != path:
        raise QueueError("setup_path_redirected")
    if path.exists():
        if path.is_symlink() or not path.is_dir():
            raise QueueError("setup_state_dir_invalid")
        return path
    if not create:
        return path
    parent = Path(os.path.abspath(path.parent))
    if parent.resolve() != parent or parent.is_symlink() or not parent.is_dir():
        raise QueueError("setup_state_dir_invalid")
    path.mkdir()
    return path


def _read_public_config(path: Path) -> dict:
    """Read and validate the public receiver identity without importing app state."""
    if path.is_symlink() or not path.is_file() or path.stat().st_nlink != 1:
        raise QueueError("assistant_receiver_configuration_invalid")
    if path.stat().st_size > MAX_CONFIG_BYTES:
        raise QueueError("assistant_receiver_configuration_invalid")

    def reject_constant(_value):
        raise ValueError("invalid constant")

    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("duplicate key")
            result[key] = value
        return result

    try:
        value = json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=pairs,
            parse_constant=reject_constant,
        )
        if type(value) is not dict:
            raise ValueError("object required")
        number = value.get("receiver_public_number")
        profile = value.get("receiver_profile_url")
        if not isinstance(number, str) or not PUBLIC_NUMBER.fullmatch(number):
            raise ValueError("invalid public number")
        canonical = public_profile(profile)
    except (ValueError, TypeError, KeyError, UnicodeError, OSError):
        raise QueueError("assistant_receiver_configuration_invalid") from None
    return {"receiver_public_number": number, "receiver_profile_url": canonical}


def receiver_setup_state(receiver_state: Path) -> dict:
    """Describe the local receiver configuration without mutating anything."""
    root = _absolute_directory(Path(receiver_state), create=False)
    config = root / CONFIG_NAME
    identity = root / IDENTITY_NAME
    inbox = root / INBOX_NAME
    legacy_ledger = any(
        item.exists() or item.is_symlink() for item in (identity, inbox))
    if not config.exists() and not config.is_symlink():
        code = ("assistant_receiver_configuration_required" if legacy_ledger
                else "assistant_setup_required")
        return {"state": "legacy_missing" if legacy_ledger else "missing",
                "reason_code": code, "message": SETUP_REASONS[code]}
    try:
        return {"state": "ready", **_read_public_config(config)}
    except QueueError as error:
        code = str(error)
        return {"state": "invalid", "reason_code": code, "message": SETUP_REASONS[code]}


def write_receiver_config(receiver_state: Path, public_number, profile_url) -> dict:
    """Create the public receiver identity once; never silently replace it."""
    if not isinstance(public_number, str):
        raise QueueError("invalid_receiver_public_number")
    number = public_number.strip()
    if not PUBLIC_NUMBER.fullmatch(number):
        raise QueueError("invalid_receiver_public_number")
    try:
        profile = public_profile(profile_url)
    except (ValueError, TypeError, AttributeError):
        raise QueueError("invalid_receiver_profile_url") from None

    root = _absolute_directory(Path(receiver_state), create=True)
    current = receiver_setup_state(root)
    if current["state"] == "ready":
        if (current["receiver_public_number"] == number
                and current["receiver_profile_url"] == profile):
            return current
        raise QueueError("receiver_configuration_locked")
    if current["state"] in {"legacy_missing", "invalid"}:
        raise QueueError(current["reason_code"])

    payload = {"receiver_public_number": number, "receiver_profile_url": profile}
    handle, temporary = tempfile.mkstemp(prefix=".receiver-", suffix=".tmp", dir=root)
    try:
        with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(payload, stream, ensure_ascii=False, separators=(",", ":"))
            stream.write("\n")
        os.replace(temporary, root / CONFIG_NAME)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return receiver_setup_state(root)


def _step(key, title, state, message):
    return {"key": key, "title": title, "state": state, "message": message}


def setup_status(registry_snapshot, receiver_state) -> dict:
    """Combine receiver state and the local authorization registry into steps.

    ``registry_snapshot`` is the public registry snapshot (or ``None`` when the
    registry has not been created yet). No secret, account credential, cookie or
    platform identifier is read here.
    """
    receiver = receiver_setup_state(receiver_state)
    receiver_ready = receiver["state"] == "ready"
    users = (registry_snapshot or {}).get("users") or []
    verified = [user for user in users
                if user.get("active") and user.get("verification_status") == "verified"]
    matching = [user for user in verified
                if receiver_ready
                and user.get("receiver_profile_url") == receiver["receiver_profile_url"]]
    devices = [(user, device) for user in verified for device in user.get("devices", [])
               if device.get("is_local") and device.get("enabled") and not device.get("revoked")
               and device.get("verification_status") == "verified"]
    scopes = [(user, device) for user, device in devices
              if any(grant.get("can_query") or grant.get("can_import")
                     for grant in device.get("libraries", []))
              or (device.get("local_owner_access") or {}).get("active")]

    accounts_message = (
        "已登记账号，并与助手 B 主页一致。" if matching else
        "请先填写上方助手 B 的公开信息，再核对账号主页。" if verified and not receiver_ready else
        "已登记账号，但助手 B 主页与接收器配置不一致，请在下方核对。" if verified else
        "请在下方登记提问账号 A 与助手账号 B 的公开主页。")

    steps = [
        _step("receiver", "填写助手 B 的公开抖音号与主页",
              "ready" if receiver_ready else
              ("blocked" if receiver["state"] in {"legacy_missing", "invalid"} else "missing"),
              receiver.get("message") or "将用于固定这台电脑接收 @ 的助手账号。"),
        _step("accounts", "登记提问账号 A 与助手 B",
              "ready" if matching else "missing", accounts_message),
        _step("device", "在本机确认处理设备",
              "ready" if devices else ("blocked" if not matching else "missing"),
              "本机设备已确认。" if devices else
              ("请先完成账号登记，再登记并确认这台电脑。" if not matching else
               "请在下方登记这台电脑，并在本机确认。")),
        _step("scope", "授权可用的资料范围",
              "ready" if scopes else ("blocked" if not devices else "missing"),
              "已授权至少一个资料范围。" if scopes else
              ("请先确认本机设备，再选择允许查询或入库的知识库。" if not devices else
               "请在设备下勾选允许查询/入库的知识库，或允许使用本人全部资料。")),
    ]
    ready = receiver_ready and bool(matching) and bool(devices) and bool(scopes)
    next_step = next((step["key"] for step in steps if step["state"] != "ready"), None)
    return {"ready": ready, "state": "ready" if ready else "setup_required",
            "next_step": next_step, "steps": steps, "receiver": receiver,
            "login": {"state": "on_start" if ready else "pending",
                      "message": "完成后首次开启助手时，用手机扫码登录助手 B。"}}
