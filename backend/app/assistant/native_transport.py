"""Bounded, GET-only native notification transport.

No network or credential access occurs on import/construction.  This module has
no signer, browser connection, arbitrary URL, POST, or response logging surface.
"""
from __future__ import annotations

from decimal import Decimal
import json
import math
import re
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request

MAX_BYTES = 2 * 1024 * 1024
ORIGIN = "https://www.douyin.com"
PREFIX = "/aweme/v1/web/"
PATH_KEYS = {
    PREFIX + "query/user/": frozenset(),
    PREFIX + "notice/count/": frozenset(("is_new_notice", "need_social_count")),
    PREFIX + "notice/": frozenset(("is_new_notice", "is_mark_read", "notice_group", "count", "min_time", "max_time")),
    PREFIX + "notice/detail/": frozenset(("id_list",)),
}
_ID = re.compile(r"[1-9][0-9]{0,29}", re.ASCII)
_ZERO = re.compile(r"(?:0|[1-9][0-9]{0,29})", re.ASCII)


ENDPOINT_ROLES = {
    PREFIX + "query/user/": "identity",
    PREFIX + "notice/count/": "notice_count",
    PREFIX + "notice/": "notice_list",
    PREFIX + "notice/detail/": "notice_detail",
}


def project_native_transport_facts(value):
    """Memory-only fixed facts, never an authority or raw-response projection.

    Unknown keys are not read. Missing/ill-typed/oversized optional numbers are
    omitted, not coerced to zero; without a known endpoint role nothing exports.
    """
    if type(value) is not dict:
        return {}
    role = value.get("endpoint_role")
    if type(role) is not str or role not in ENDPOINT_ROLES.values():
        return {}
    result = {"endpoint_role": role}
    status = value.get("http_status")
    if type(status) is int and 100 <= status <= 599:
        result["http_status"] = status
    code = value.get("api_status_code")
    if type(code) is int and -(2 ** 31) <= code < 2 ** 31:
        result["api_status_code"] = code
    return result


class NativeTransportError(RuntimeError):
    def __init__(self, code, *, transport_facts=None):
        self._transport_facts = tuple(project_native_transport_facts(transport_facts).items())
        # Only module-authored literal codes are passed to this constructor.
        self.code = code if type(code) is str and re.fullmatch(r"[a-z_]{1,64}", code) else "transport_error"
        super().__init__(self.code)

    @property
    def transport_facts(self):
        return dict(self._transport_facts)


def fail(code):
    raise NativeTransportError(code) from None


def decimal_id(value, *, zero=False, code="invalid_identifier"):
    if type(value) is int:
        if not 0 <= value < 10 ** 30:
            fail(code)
        value = str(value)
    if type(value) is not str or not (_ZERO if zero else _ID).fullmatch(value):
        fail(code)
    return value


class SecretSession:
    __slots__ = ("_cookie_header", "_user_agent", "_expected_uid", "_expires_at")

    def __init__(self, cookie_header: str, user_agent: str, expected_uid: str, expires_at: float):
        for value, limit in ((cookie_header, 65536), (user_agent, 4096)):
            if (type(value) is not str or not 1 <= len(value) <= limit
                    or any(ord(c) < 32 or ord(c) == 127 for c in value)):
                fail("invalid_session")
        expected_uid = decimal_id(expected_uid, code="invalid_session")
        if (type(expires_at) not in (int, float) or not math.isfinite(expires_at)
                or not 0 < expires_at < 10 ** 12):
            fail("invalid_session")
        object.__setattr__(self, "_cookie_header", cookie_header)
        object.__setattr__(self, "_user_agent", user_agent)
        object.__setattr__(self, "_expected_uid", expected_uid)
        object.__setattr__(self, "_expires_at", float(expires_at))

    def __setattr__(self, name, value):
        raise AttributeError("session_immutable")

    @property
    def expected_uid(self):
        return self._expected_uid

    @property
    def expires_at(self):
        return self._expires_at

    def __repr__(self):
        return "<SecretSession redacted>"

    __str__ = __repr__


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        fail("redirect_denied")


def _stdlib_get(url, headers, timeout, max_bytes):
    try:
        parsed = urllib.parse.urlsplit(url)
        if (type(url) is not str or len(url) > 16384 or parsed.scheme != "https"
                or parsed.netloc != "www.douyin.com" or parsed.path not in PATH_KEYS or parsed.fragment):
            fail("request_not_allowed")
        pairs = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True, max_num_fields=20, errors="strict")
        keys = [key for key, _ in pairs]
        if len(keys) != len(set(keys)) or set(keys) - (PATH_KEYS[parsed.path] | {"device_platform", "aid", "channel"}):
            fail("request_not_allowed")
        if parsed.path == PREFIX + "notice/" and dict(pairs).get("is_mark_read") != "0":
            fail("request_not_allowed")
    except NativeTransportError:
        raise
    except (TypeError, ValueError, UnicodeError):
        fail("request_not_allowed")
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({}), _NoRedirect(),
        urllib.request.HTTPSHandler(context=ssl.create_default_context()))
    request = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with opener.open(request, timeout=timeout) as response:
            return response.status, response.read(max_bytes + 1)
    except urllib.error.HTTPError as error:
        status = error.code
        error.close()
        return status, b""


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            fail("duplicate_json_key")
        result[key] = value
    return result


def _integer(value):
    if len(value.lstrip("-")) > 100:
        fail("json_integer_limit")
    return int(value)


def _constant(value):
    fail("invalid_json_constant")


def strict_json(body):
    if type(body) is not bytes or len(body) > MAX_BYTES:
        fail("response_too_large")
    if not body.strip():
        fail("empty_response")
    try:
        value = json.loads(body.decode("utf-8"), object_pairs_hook=_pairs,
                           parse_int=_integer, parse_float=Decimal, parse_constant=_constant)
    except NativeTransportError:
        raise
    except (ValueError, UnicodeError, RecursionError, ArithmeticError):
        fail("response_not_json")
    if type(value) is not dict:
        fail("invalid_response_shape")
    return value


class NativeTransport:
    """Injected network signature: (url, headers, timeout, max_bytes)->(int, bytes).

    The session provider is app-owned and loaded only by verify_identity().
    No raw response, URL query, header or external exception is retained.
    """
    def __init__(self, session_provider, expected_uid, network=None, *, timeout=8.0, clock=None):
        if not callable(session_provider) or (network is not None and not callable(network)):
            fail("invalid_session_provider")
        self.expected_uid = decimal_id(expected_uid)
        if type(timeout) not in (int, float) or not 0 < timeout <= 15:
            fail("invalid_timeout")
        self._provider = session_provider
        self._network = network or _stdlib_get
        self._clock = clock or time.time
        self._timeout = float(timeout)
        self._session = None
        self._verified = False
        self._groups = {}

    def __repr__(self):
        return "<NativeTransport GET-only redacted>"

    def _guard(self, verified=True):
        if type(self._session) is not SecretSession:
            fail("authorization_not_provisioned")
        if self._clock() >= self._session.expires_at:
            self._verified = False
            fail("session_expired")
        if verified and not self._verified:
            fail("identity_not_verified")

    def _get_json(self, path, parameters):
        facts = project_native_transport_facts({"endpoint_role": ENDPOINT_ROLES.get(path)}) if type(path) is str else {}
        try:
            return self._get_json_checked(path, parameters, facts)
        except NativeTransportError as error:
            # Only this call's fixed route/validated response numbers survive.
            # An injected network exception cannot supply its own facts.
            raise NativeTransportError(error.code, transport_facts=facts) from None

    def _get_json_checked(self, path, parameters, facts):
        self._guard(verified=False)
        if path not in PATH_KEYS or type(parameters) is not dict or set(parameters) - PATH_KEYS[path]:
            fail("request_not_allowed")
        if path == PREFIX + "notice/" and parameters.get("is_mark_read") != 0:
            fail("request_not_allowed")
        url = ORIGIN + path + "?" + urllib.parse.urlencode({
            "device_platform": "webapp", "aid": "6383", "channel": "channel_pc_web", **parameters})
        headers = {"Cookie": self._session._cookie_header, "User-Agent": self._session._user_agent,
                   "Accept": "application/json", "Referer": ORIGIN + "/"}
        try:
            response = self._network(url, headers, self._timeout, MAX_BYTES)
        except NativeTransportError:
            raise
        except (ssl.SSLError, ssl.CertificateError):
            fail("tls_error")
        except TimeoutError:
            fail("network_timeout")
        except Exception:
            fail("network_error")
        self._guard(verified=False)
        if (type(response) is not tuple or len(response) != 2
                or type(response[0]) is not int or type(response[1]) is not bytes):
            fail("network_response_invalid")
        status, body = response
        facts["http_status"] = status
        if 300 <= status < 400:
            fail("redirect_denied")
        if status in (401, 403):
            self._verified = False
            fail("session_invalid")
        if status == 429:
            fail("rate_limited")
        if status != 200:
            fail("http_error")
        value = strict_json(body)
        code = value.get("status_code")
        if type(code) is not int:
            fail("invalid_response_status")
        facts["api_status_code"] = code
        message = value.get("status_msg", "")
        message = message.casefold() if type(message) is str else ""
        if any(value.get(key) for key in ("captcha_required", "verify_required", "isRiskManagementVerifyError", "verify_center_decision_conf")) or any(
                word in message for word in ("验证码", "安全验证", "captcha", "verification")):
            self._verified = False
            fail("verification_required")
        if code != 0:
            if any(word in message for word in ("未登录", "登录失效", "登录过期", "login required", "session expired")):
                self._verified = False
                fail("session_invalid")
            fail("invalid_parameters" if code == 5 else "api_error")
        return value

    def verify_identity(self):
        self._verified = False
        try:
            session = self._provider()
        except Exception as error:
            # Never propagate provider exception text or secret-bearing paths.
            code = getattr(error, "code", "authorization_not_provisioned")
            allowed = {"authorization_expired", "authorization_not_provisioned", "capsule_invalid", "capsule_unprotect_failed", "vault_path_not_owned", "vault_path_not_regular"}
            fail(code if code in allowed else "authorization_not_provisioned")
        if type(session) is not SecretSession or session.expected_uid != self.expected_uid:
            fail("session_identity_mismatch")
        self._session = session
        value = self._get_json(PREFIX + "query/user/", {})
        uid = decimal_id(value.get("user_uid"), code="identity_schema_unknown")
        if uid != self.expected_uid:
            fail("session_identity_mismatch")
        self._verified = True
        return uid

    def get_groups(self):
        self._guard()
        value = self._get_json(PREFIX + "notice/count/", {"is_new_notice": 1, "need_social_count": 1})
        rows = value.get("interactive_group")
        if type(rows) is not list or len(rows) > 100:
            fail("group_schema_unknown")
        result = {}
        for kind in (9, 7):
            matches = [row for row in rows if type(row) is dict and type(row.get("type")) in (int, str) and row["type"] in (kind, str(kind))]
            if len(matches) != 1:
                fail("group_mapping_not_unique")
            result[kind] = decimal_id(matches[0].get("group"), code="invalid_group")
        if result[9] == result[7]:
            fail("group_mapping_not_unique")
        self._groups = result
        return dict(result)

    def read_page(self, group, cursor_pair):
        self._guard()
        group = decimal_id(group, code="invalid_group")
        if group not in self._groups.values():
            fail("group_not_verified")
        if type(cursor_pair) is not tuple or len(cursor_pair) != 2:
            fail("invalid_cursor")
        minimum, maximum = (decimal_id(v, zero=True, code="invalid_cursor") for v in cursor_pair)
        value = self._get_json(PREFIX + "notice/", {
            "is_new_notice": 1, "is_mark_read": 0, "notice_group": group, "count": 10,
            "min_time": minimum, "max_time": maximum})
        rows, more = value.get("notice_list_v2"), value.get("has_more")
        if type(rows) is not list or len(rows) > 100 or type(more) not in (int, bool) or more not in (0, 1):
            fail("page_schema_unknown")
        next_min = decimal_id(value.get("min_time"), zero=True, code="page_schema_unknown")
        next_max = decimal_id(value.get("max_time"), zero=True, code="page_schema_unknown")
        if more and not rows:
            fail("empty_nonterminal")
        if more and (next_min, next_max) == (minimum, maximum):
            fail("cursor_stalled")
        # This transient raw JSON is consumed by native_events then discarded;
        # the transport never stores or logs it, including unrelated users.
        return {"status_code": 0, "notice_list_v2": rows, "has_more": bool(more),
                "min_time": next_min, "max_time": next_max}
