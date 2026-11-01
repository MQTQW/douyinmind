"""Self-serve provisioning for the opt-in native (API) intake channel.

This module is the missing "public initialization tool" for the native
notification channel. It performs one explicit local operation:

1. opens the assistant's own B browser profile (never the public favourites
   profile) and confirms the configured B profile page is the logged-in self;
2. captures only the cookies required by the four fixed GET routes plus the
   browser user-agent;
3. verifies the real platform identity through ``/aweme/v1/web/query/user/``;
4. seals the session into the app-owned Windows DPAPI capsule;
5. creates the native source store and writes ``native-receiver.json``
   (``enabled: true``) atomically.

It never reads notification content, publishes, imports any ledger or returns
raw credentials. ``disable`` flips ``enabled`` to false so the product falls
back to the normal-page intake lane.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path
from urllib.parse import urlencode

from app.core.config import settings
from app.mention_receiver.browser import SELF_URL, browser_options, isolated_profile
from app.mention_agent.contracts import public_profile

BACKEND = Path(__file__).resolve().parents[2]
STORAGE = BACKEND / "app" / "storage"
RECEIVER_STATE = STORAGE / "mention_receiver"
QUEUE_STATE = STORAGE / "assistant"
CONFIG_NAME = "native-receiver.json"
CONFIG_VERSION = 1
SESSION_SOURCE = "owned_capsule"
NATIVE_ROOT_NAME = "native-receiver"
SOURCE_DB_NAME = "sources.sqlite3"
OWNER_MARKER_SUFFIX = ".owner.json"

_ORIGIN = "https://www.douyin.com"
_QUERY_USER = "/aweme/v1/web/query/user/"
_ROUTES = (_ORIGIN + _QUERY_USER,
           _ORIGIN + "/aweme/v1/web/notice/count/",
           _ORIGIN + "/aweme/v1/web/notice/",
           _ORIGIN + "/aweme/v1/web/notice/detail/")
_SELF_MARKERS = ("编辑资料", "私密作品", "作品管理")
_SEC_UID = re.compile(r"[A-Za-z0-9_-]{8,256}", re.ASCII)
# Short-lived page telemetry cookies (e.g. sdk_source_info/bit_env, ~30s TTL)
# are not session credentials and would make the sealed snapshot look expired
# before it is even used. Only this fixed telemetry allowlist is dropped;
# unknown short cookies still fail closed through the sealed session rules.
_EPHEMERAL_SECONDS = 120
_EPHEMERAL_COOKIE_NAMES = frozenset({"sdk_source_info", "bit_env", "gulu_source_res",
                                     "__ac_nonce", "sdk_source_info_v2", "is_dbsc", "x_tt_token"})
_CREDENTIAL_COOKIES = frozenset({"sessionid", "sessionid_ss", "sid_tt", "sid_guard",
                                 "uid_tt", "uid_tt_ss", "ttwid", "passport_csrf_token",
                                 "passport_csrf_token_default"})


def _stable_cookies(cookies, now: float) -> list:
    stable = []
    for row in cookies:
        if not isinstance(row, dict):
            _fail("native_provision_session_unavailable")
        name, expiry = row.get("name"), row.get("expires")
        if (isinstance(name, str) and name in _EPHEMERAL_COOKIE_NAMES
                and isinstance(expiry, (int, float)) and expiry != -1
                and expiry <= now + _EPHEMERAL_SECONDS):
            continue
        if isinstance(name, str) and name in _CREDENTIAL_COOKIES and isinstance(expiry, (int, float)) \
                and expiry != -1 and expiry <= now + _EPHEMERAL_SECONDS:
            _fail("native_provision_session_unavailable")
        stable.append(row)
    if not stable:
        _fail("native_provision_session_unavailable")
    return stable


class NativeProvisionError(RuntimeError):
    def __init__(self, code):
        self.code = code if type(code) is str and re.fullmatch(r"[a-z_]{1,80}", code) else "native_provision_failed"
        super().__init__(self.code)


def _fail(code):
    raise NativeProvisionError(code) from None


def _receiver_identity(receiver_state: Path) -> dict:
    from .setup import _read_public_config

    try:
        return _read_public_config(Path(receiver_state) / "receiver.local.json")
    except Exception:
        _fail("native_provision_receiver_configuration_required")


def _sec_uid(profile_url: str) -> str:
    value = (profile_url or "").rstrip("/").rsplit("/", 1)[-1]
    if not _SEC_UID.fullmatch(value):
        _fail("native_provision_receiver_configuration_invalid")
    return value


def _protected_profile() -> Path:
    protected = Path(settings.playwright_user_data_dir)
    return protected if protected.is_absolute() else BACKEND / protected


def capture_b_session(profile: Path, receiver_profile_url: str) -> dict:
    """Capture UA + fixed-route cookies from the assistant's own B profile."""
    from playwright.sync_api import sync_playwright

    try:
        with sync_playwright() as playwright:
            context = playwright.chromium.launch_persistent_context(
                str(profile), headless=True, viewport={"width": 1100, "height": 800},
                **browser_options(STORAGE))
            try:
                page = context.pages[0] if context.pages else context.new_page()
                page.goto(SELF_URL, wait_until="domcontentloaded", timeout=45000)
                page.wait_for_timeout(1200)
                page.goto(receiver_profile_url, wait_until="domcontentloaded", timeout=45000)
                page.wait_for_timeout(1200)
                text = page.inner_text("body")[:8000]
                if not any(marker in text for marker in _SELF_MARKERS):
                    _fail("native_provision_profile_unverified")
                cdp = context.new_cdp_session(page)
                version = cdp.send("Browser.getVersion", {})
                cdp.detach()
                user_agent = version.get("userAgent") if isinstance(version, dict) else None
                if type(user_agent) is not str or not user_agent:
                    _fail("native_provision_session_unavailable")
                rows = context.cookies(list(_ROUTES))
                if type(rows) is not list or not rows:
                    _fail("native_provision_session_unavailable")
                return {"user_agent": user_agent, "cookies": rows}
            finally:
                context.close()
    except NativeProvisionError:
        raise
    except Exception:
        _fail("native_provision_profile_unavailable")


def _identity_uid(cookie_header: str, user_agent: str, network) -> str:
    from .native_transport import decimal_id, strict_json

    url = _ORIGIN + _QUERY_USER + "?" + urlencode(
        {"device_platform": "webapp", "aid": "6383", "channel": "channel_pc_web"})
    headers = {"Cookie": cookie_header, "User-Agent": user_agent,
               "Accept": "application/json", "Referer": _ORIGIN + "/"}
    try:
        status, body = network(url, headers, 8.0, 2 * 1024 * 1024)
    except Exception:
        _fail("native_provision_network_unavailable")
    if type(status) is not int or type(body) is not bytes:
        _fail("native_provision_network_unavailable")
    if status in (401, 403):
        _fail("native_provision_session_invalid")
    if status != 200:
        _fail("native_provision_http_error")
    try:
        value = strict_json(body)
    except Exception:
        _fail("native_provision_schema_unknown")
    if not isinstance(value, dict):
        _fail("native_provision_schema_unknown")
    if any(value.get(key) for key in ("captcha_required", "verify_required",
                                      "isRiskManagementVerifyError", "verify_center_decision_conf")):
        _fail("native_provision_verification_required")
    code = value.get("status_code")
    if type(code) is not int or code != 0:
        _fail("native_provision_api_error")
    try:
        return decimal_id(value.get("user_uid"), code="native_provision_identity_unavailable")
    except Exception:
        _fail("native_provision_identity_unavailable")


def _build_session(cookies, user_agent: str, uid: str, *, clock=None):
    from .native_lifecycle import _cookie_snapshot

    try:
        return _cookie_snapshot(cookies, user_agent, uid, now=(clock or time.time)(), ttl_cap=3600)
    except Exception:
        _fail("native_provision_session_unavailable")


def _read_owner_marker(path: Path):
    marker = Path(str(path) + OWNER_MARKER_SUFFIX)
    if not marker.is_file() or marker.is_symlink():
        return None
    try:
        value = json.loads(marker.read_text(encoding="utf-8"))
    except Exception:
        _fail("native_provision_store_mismatch")
    if (not isinstance(value, dict) or type(value.get("receiver_uid")) is not str
            or not isinstance(value.get("receiver_profile_url"), str)):
        _fail("native_provision_store_mismatch")
    return value


def _read_config(queue_state: Path):
    from .native_configuration import NativeConfigurationError, load_native_configuration

    path = Path(queue_state) / CONFIG_NAME
    if not path.exists():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        _fail("native_provision_configuration_invalid")
    if not isinstance(value, dict):
        _fail("native_provision_configuration_invalid")
    return value


def _write_json_atomic(path: Path, value: dict) -> None:
    path = Path(path).absolute()
    if path.is_symlink():
        _fail("native_provision_configuration_invalid")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name("." + path.name + "." + os.urandom(8).hex() + ".tmp")
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    separators=(",", ":"), allow_nan=False))
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        try:
            if temporary.exists():
                temporary.unlink()
        except OSError:
            pass


def provision(*, receiver_state=None, queue_state=None, capture=None, network_get=None,
              clock=None, protector=None) -> dict:
    """Enable (or refresh) the native intake channel for the configured B."""
    receiver_state = Path(receiver_state or RECEIVER_STATE).absolute()
    queue_state = Path(queue_state or QUEUE_STATE).absolute()
    identity = _receiver_identity(receiver_state)
    profile_url = public_profile(identity["receiver_profile_url"])
    if profile_url is None:
        _fail("native_provision_receiver_configuration_invalid")
    sec_uid = _sec_uid(profile_url)

    existing = _read_config(queue_state)
    root = queue_state / NATIVE_ROOT_NAME
    store_path = root / SOURCE_DB_NAME
    previous_owner = _read_owner_marker(store_path) if store_path.exists() else None
    if existing is not None:
        uid = existing.get("receiver_uid")
        if type(uid) is not str or not existing.get("receiver_profile_url") == profile_url:
            _fail("native_provision_configuration_invalid")
        if existing.get("receiver_sec_uid") != sec_uid:
            _fail("native_provision_identity_changed")
    if previous_owner is not None:
        if (previous_owner.get("receiver_profile_url") != profile_url):
            _fail("native_provision_store_mismatch")
        if type(existing) is dict and existing.get("receiver_uid") not in (None, previous_owner.get("receiver_uid")):
            _fail("native_provision_identity_changed")

    if capture is None:
        profile = isolated_profile(STORAGE, _protected_profile())
        capture = lambda: capture_b_session(profile, profile_url)  # noqa: E731
    data = capture()
    if not isinstance(data, dict):
        _fail("native_provision_session_unavailable")
    user_agent = data.get("user_agent")
    cookies = data.get("cookies")
    if type(user_agent) is not str or not isinstance(cookies, list):
        _fail("native_provision_session_unavailable")
    now = (clock or time.time)()
    cookies = _stable_cookies(cookies, now)
    cookie_header = "; ".join(f"{row['name']}={row['value']}"
                              for row in sorted(cookies, key=lambda value: value.get("name", "")))
    uid = _identity_uid(cookie_header, user_agent, network_get or _default_network())
    if previous_owner is not None and previous_owner.get("receiver_uid") != uid:
        _fail("native_provision_identity_changed")
    if isinstance(existing, dict) and existing.get("receiver_uid") != uid:
        _fail("native_provision_identity_changed")

    session = _build_session(cookies, user_agent, uid, clock=clock)
    from .native_session import NativeSessionVault

    root.mkdir(parents=True, exist_ok=True)
    (root / "private").mkdir(parents=True, exist_ok=True)
    vault = NativeSessionVault(uid, private_root=root / "private", clock=clock,
                               protector=protector)
    try:
        vault.save(session)
        loaded = vault.load()
    except Exception:
        _fail("native_provision_session_unavailable")
    if loaded.expected_uid != uid:
        _fail("native_provision_session_unavailable")

    from .native_source_store import NativeSourceStore, NativeStoreError

    try:
        NativeSourceStore(store_path, uid, profile_url, create=True)
    except NativeStoreError:
        _fail("native_provision_store_mismatch")
    except Exception:
        _fail("native_provision_store_mismatch")

    _write_json_atomic(queue_state / CONFIG_NAME, {
        "version": CONFIG_VERSION, "enabled": True, "receiver_uid": uid,
        "receiver_sec_uid": sec_uid, "receiver_profile_url": profile_url,
        "session_source": SESSION_SOURCE,
    })
    # Re-read through the product loader: a config the runtime cannot accept is
    # not a successful provision.
    from .native_configuration import NativeConfigurationError, load_native_configuration

    try:
        accepted = load_native_configuration(queue_state, profile_url)
    except NativeConfigurationError:
        _fail("native_provision_configuration_invalid")
    if accepted is None:
        _fail("native_provision_configuration_invalid")
    return {"enabled": True, "profile_verified": True,
            "receiver_uid_digits": len(uid), "native_store_created": previous_owner is None}


def disable(*, queue_state=None) -> dict:
    """Keep the channel off; the normal-page intake lane resumes on restart."""
    queue_state = Path(queue_state or QUEUE_STATE).absolute()
    existing = _read_config(queue_state)
    if existing is None:
        return {"enabled": False, "changed": False}
    if existing.get("version") != CONFIG_VERSION:
        _fail("native_provision_configuration_invalid")
    updated = {**existing, "enabled": False}
    _write_json_atomic(queue_state / CONFIG_NAME, updated)
    return {"enabled": False, "changed": True}


def _default_network():
    from .native_transport import _stdlib_get

    return _stdlib_get


def status(*, queue_state=None, receiver_state=None) -> dict:
    queue_state = Path(queue_state or QUEUE_STATE).absolute()
    receiver_state = Path(receiver_state or RECEIVER_STATE).absolute()
    existing = _read_config(queue_state)
    configured = bool(_receiver_identity(receiver_state))
    enabled = existing.get("enabled") is True if isinstance(existing, dict) else False
    return {"configured": configured, "enabled": enabled,
            "uid_present": isinstance(existing, dict) and type(existing.get("receiver_uid")) is str}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Native (API) intake provisioning.")
    parser.add_argument("--action", choices=("enable", "disable", "status"), default="enable")
    parser.add_argument("--result-file", default="")
    args = parser.parse_args(argv)
    try:
        if args.action == "enable":
            result = provision()
        elif args.action == "disable":
            result = disable()
        else:
            result = status()
        payload = {"ok": True, **result}
        code = 0
    except NativeProvisionError as error:
        payload = {"ok": False, "code": error.code}
        code = 1
    except Exception:
        payload = {"ok": False, "code": "native_provision_failed"}
        code = 1
    if args.result_file:
        try:
            _write_json_atomic(Path(args.result_file), payload)
        except Exception:
            pass
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True), flush=True)
    return code


if __name__ == "__main__":
    sys.exit(main())
