"""Owner-thread native session lifecycle and bounded public health facts.

Uses only the already authorized app-owned B context and fixed read-only native
identity transport. No browser launch, credential DB extraction, arbitrary URL,
UI input or sending API. Import performs no credential or network operation.
"""
from __future__ import annotations

from contextlib import contextmanager
import math
import re
import threading
import time

from app.assistant.native_transport import NativeTransport, SecretSession, decimal_id, project_native_transport_facts

_URLS = (
    "https://www.douyin.com/aweme/v1/web/query/user/",
    "https://www.douyin.com/aweme/v1/web/notice/count/",
    "https://www.douyin.com/aweme/v1/web/notice/",
    "https://www.douyin.com/aweme/v1/web/notice/detail/",
)
_NAME = re.compile(r"[!#$%&'*+.^_`|~0-9A-Za-z-]{1,256}", re.ASCII)
_SEC = re.compile(r"[A-Za-z0-9_.=-]{1,256}", re.ASCII)
_CODES = frozenset({
    "session_owner_required", "session_context_changed", "session_existing_draft",
    "session_draft_state_unknown", "verification_required", "session_invalid",
    "session_cycle_active", "session_cycle_not_active", "authorization_expired",
    "authorization_not_provisioned", "capsule_invalid", "capsule_unprotect_failed",
    "capsule_identity_mismatch", "vault_path_not_owned", "vault_path_not_regular",
    "session_configuration_invalid", "session_cookie_shape_unknown",
    "session_cookie_scope_unknown", "session_cookie_ambiguous", "session_cookie_expired",
    "session_cookie_expiring", "session_metadata_unavailable", "session_candidate_invalid",
    "session_identity_mismatch", "session_refresh_unavailable", "session_expired",
    "tls_error", "network_timeout", "network_error", "redirect_denied", "rate_limited",
    "empty_response", "empty_nonterminal", "response_not_json", "invalid_response_shape", "invalid_response_status",
    "identity_schema_unknown", "invalid_parameters", "api_error", "http_error",
    "response_too_large", "duplicate_json_key", "json_integer_limit", "invalid_json_constant",
    "session_cooldown", "session_transient_retry_exhausted",
    "invalid_native_session_status",
})
_TRANSIENT_CODES = frozenset({"network_timeout", "network_error", "rate_limited", "http_error", "empty_response", "empty_nonterminal", "session_cookie_expiring"})
_TRANSIENT_DELAYS = (45.0, 90.0)


def _project_status_transport_facts(value):
    """Strict optional public projection; never inspect unknown extra values."""
    if type(value) is not dict:
        _fail("invalid_native_session_status")
    known = {key: value[key] for key in ("endpoint_role", "http_status", "api_status_code") if key in value}
    clean = project_native_transport_facts(known)
    if "endpoint_role" not in clean or clean != known:
        _fail("invalid_native_session_status")
    return clean


def project_native_session_status(value):
    """Copy only bounded public lifecycle facts; never serialize input wholesale.

    Unknown extra fields are discarded without reading their values. Expected
    fields must have exact primitive types and known fixed enumerations.
    """
    fields = {"state", "reason_code", "generation", "refresh_count", "source", "seal_state",
              "transient_failures", "last_transient_code", "retry_after_seconds"}
    if type(value) is not dict or not fields <= value.keys():
        _fail("invalid_native_session_status")
    result = {key: value[key] for key in fields}
    enums = {
        "state": {"not_started", "ready", "cycle_active", "cooldown", "paused"},
        "source": {"none", "owned", "legacy_once", "same_context"},
        "seal_state": {"not_attempted", "sealed", "failed"},
    }
    for key, allowed in enums.items():
        if type(result[key]) is not str or result[key] not in allowed:
            _fail("invalid_native_session_status")
    for key, maximum in (("generation", 10 ** 9), ("refresh_count", 10 ** 9),
                         ("transient_failures", 3), ("retry_after_seconds", 90)):
        if type(result[key]) is not int or not 0 <= result[key] <= maximum:
            _fail("invalid_native_session_status")
    reason, last = result["reason_code"], result["last_transient_code"]
    if reason is not None and (type(reason) is not str or reason not in _CODES):
        _fail("invalid_native_session_status")
    if last is not None and (type(last) is not str or last not in _TRANSIENT_CODES):
        _fail("invalid_native_session_status")
    if (result["state"] == "paused") != (reason is not None):
        _fail("invalid_native_session_status")
    if result["state"] == "cooldown" and (last is None or result["transient_failures"] not in (1, 2)
                                            or result["retry_after_seconds"] == 0):
        _fail("invalid_native_session_status")
    if "transport_facts" in value:
        result["transport_facts"] = _project_status_transport_facts(value["transport_facts"])
    return result


def visible_draft_is_empty(adapter):
    """Only a boolean/unknown result; never returns or logs composer text.

    Zero known editors is acceptable on a readable page with no other visible
    textarea/contenteditable surface. It is not a claim about hidden drafts or
    arbitrary future editor widgets; unknown visible editables fail closed.
    """
    try:
        page = adapter.page
        if page.is_closed() is not False or page.locator("body").count() != 1:
            return None
        visible = page.locator(":visible")
        unknown = page.locator('textarea, [contenteditable="true"]:not([role="combobox"])').and_(visible)
        unknown_count = unknown.count()
        if type(unknown_count) is not int or unknown_count != 0:
            return None
        editors = page.locator('[contenteditable="true"][role="combobox"]').and_(visible)
        count = editors.count()
        if type(count) is not int or not 0 <= count <= 4:
            return None
        for index in range(count):
            text = editors.nth(index).inner_text(timeout=3000)
            if type(text) is not str:
                return None
            if text.strip():
                return False
        return True
    except Exception:
        return None


class SessionLifecycleError(RuntimeError):
    """Fixed safe code only; no path, raw response, headers or exception text."""
    def __init__(self, code, *, transport_facts=None):
        self._transport_facts = tuple(project_native_transport_facts(transport_facts).items())
        self.code = code if type(code) is str and code in _CODES else "session_refresh_unavailable"
        super().__init__(self.code)

    @property
    def transport_facts(self):
        return dict(self._transport_facts)


def _fail(code):
    raise SessionLifecycleError(code) from None


def _translated(error):
    return SessionLifecycleError(getattr(error, "code", None),
        transport_facts=getattr(error, "transport_facts", None))


def _path_matches(request_path, cookie_path):
    return (request_path == cookie_path or (request_path.startswith(cookie_path)
            and (cookie_path.endswith("/") or request_path[len(cookie_path):].startswith("/"))))


def _cookie_snapshot(rows, ua, expected_uid, *, now, ttl_cap):
    """Project transient normal-context data into one redacted memory object."""
    if type(rows) is not list or not 1 <= len(rows) <= 256:
        _fail("session_cookie_shape_unknown")
    values, expires = {}, [now + ttl_cap]
    for row in rows:
        if type(row) is not dict:
            _fail("session_cookie_shape_unknown")
        domain, path = row.get("domain"), row.get("path")
        # cookies(_URLS) is already URL-filtered; retain a narrow independent
        # host/path check, and never interpret a partitioned-cookie scope.
        if (type(domain) is not str or domain not in {"douyin.com", ".douyin.com", "www.douyin.com", ".www.douyin.com"}
                or type(path) is not str or not path.startswith("/")
                # NativeTransport uses ONE fixed header for all four routes;
                # accepting a narrower cookie would broaden its actual scope.
                # Hold instead of guessing per-route selection this phase.
                or not all(_path_matches(url.split("www.douyin.com", 1)[1], path) for url in _URLS)
                or row.get("partitionKey") not in (None, "")):
            _fail("session_cookie_scope_unknown")
        name, value, expiry = row.get("name"), row.get("value"), row.get("expires")
        if (type(name) is not str or not _NAME.fullmatch(name) or type(value) is not str
                or len(value) > 16384 or not value.isascii()
                or any(ord(c) <= 32 or ord(c) >= 127 or c in ";," for c in value)
                or type(expiry) not in (int, float) or not math.isfinite(expiry)):
            _fail("session_cookie_shape_unknown")
        if expiry != -1:
            if expiry <= now:
                _fail("session_cookie_expired")
            expires.append(float(expiry))
        if name in values and values[name] != value:
            _fail("session_cookie_ambiguous")
        values[name] = value
    deadline = min(expires)
    if deadline <= now + 60:
        _fail("session_cookie_expiring")
    try:
        return SecretSession("; ".join(key + "=" + value for key, value in sorted(values.items())),
                             ua, expected_uid, deadline)
    except Exception:
        _fail("session_candidate_invalid")


class SameContextCapture:
    """Trusted adapter's existing context only; never accepts an external profile.

    `draft_is_empty` is a trusted local UI read callback, not a request flag.
    It must return exactly True for a verified empty visible composer set and
    False for a nonempty draft; unknown state must raise or return neither.
    Its implementation must not return or log draft text.
    """
    def __init__(self, adapter, expected_profile_url, expected_uid, *,
                 draft_is_empty, clock=None, ttl_cap=3600):
        prefix = "https://www.douyin.com/user/"
        if (type(expected_profile_url) is not str or not expected_profile_url.startswith(prefix)
                or not _SEC.fullmatch(expected_profile_url[len(prefix):]) or not callable(draft_is_empty)
                or type(ttl_cap) not in (int, float) or not 600 <= ttl_cap <= 3600):
            _fail("session_configuration_invalid")
        self.expected_uid = decimal_id(expected_uid)
        self._adapter, self._expected_profile = adapter, expected_profile_url
        self._page, self._context = adapter.page, adapter.page.context
        self._owner = threading.get_ident()
        self._draft_is_empty, self._clock = draft_is_empty, clock or time.time
        self._ttl_cap = float(ttl_cap)

    def __repr__(self):
        return "<SameContextCapture owner-bound redacted>"

    def guard(self):
        if threading.get_ident() != self._owner:
            _fail("session_owner_required")
        if (self._adapter.page is not self._page or self._page.context is not self._context
                or self._adapter.expected_profile_url != self._expected_profile):
            _fail("session_context_changed")
        try:
            self._adapter.check_intervention()
        except Exception:
            _fail("verification_required")
        try:
            empty = self._draft_is_empty()
        except Exception:
            _fail("session_draft_state_unknown")
        if empty is False:
            _fail("session_existing_draft")
        if empty is not True:
            _fail("session_draft_state_unknown")

    def capture(self):
        self.guard()
        metadata = None
        try:
            browser = self._context.browser
            if browser is None:
                _fail("session_metadata_unavailable")
            metadata = browser.new_browser_cdp_session()
            version = metadata.send("Browser.getVersion", {})
            if type(version) is not dict:
                _fail("session_metadata_unavailable")
            ua = version.get("userAgent")
            if (type(ua) is not str or not 1 <= len(ua) <= 4096
                    or any(ord(c) < 32 or ord(c) == 127 for c in ua)):
                _fail("session_metadata_unavailable")
            # Only these fixed HTTPS API paths, not all browser cookies.
            rows = self._context.cookies(list(_URLS))
            snapshot = _cookie_snapshot(rows, ua, self.expected_uid,
                                        now=self._clock(), ttl_cap=self._ttl_cap)
            self.guard()
            return snapshot
        except SessionLifecycleError:
            raise
        except Exception:
            _fail("session_refresh_unavailable")
        finally:
            if metadata is not None:
                try:
                    metadata.detach()
                except Exception:
                    pass


class NativeSessionLifecycle:
    """One owner-bound pinned snapshot per poll; no midcycle context access.

    owned_vault is the normal trusted app-owned NativeSessionVault.  Optional
    legacy_loader is a fixed local compatibility loader, not a path parameter.
    It is attempted at most once per lifecycle, only if owned is missing, never
    after owned corruption/expiry and never used to extend or seal legacy data.

    The optional injected network is solely a trusted local testing seam. No
    configuration or HTTP request can choose its callable, URL or method.
    """
    def __init__(self, *, expected_uid, owned_vault, context_capture,
                 legacy_loader=None, network=None, clock=None, cooldown_clock=None,
                 ttl_cap=3600, refresh_before=300, seal_refreshed=True,
                 require_seal=False):
        if (type(ttl_cap) not in (int, float) or not 600 <= ttl_cap <= 3600
                or type(refresh_before) not in (int, float) or not 60 <= refresh_before < ttl_cap
                or type(seal_refreshed) is not bool or type(require_seal) is not bool
                or (legacy_loader is not None and not callable(legacy_loader))
                or (network is not None and not callable(network))):
            _fail("session_configuration_invalid")
        self.expected_uid = decimal_id(expected_uid)
        if getattr(owned_vault, "expected_uid", None) != self.expected_uid or context_capture.expected_uid != self.expected_uid:
            _fail("session_configuration_invalid")
        self._owned, self._capture, self._legacy = owned_vault, context_capture, legacy_loader
        self._network, self._clock = network, clock or time.time
        self._cooldown_clock = cooldown_clock or (clock if clock is not None else time.monotonic)
        self._ttl_cap, self._refresh_before = float(ttl_cap), float(refresh_before)
        self._seal, self._require_seal = seal_refreshed, require_seal
        self._owner = threading.get_ident()
        self._loaded = self._legacy_attempted = False
        self._active = self._pinned = None
        self._blocked = None
        self._generation = self._refreshes = 0
        self._source = "none"
        self._seal_state = "not_attempted"
        self._transient_failures = 0
        self._last_transient = None
        self._cooldown_until = 0.0
        self._failure_sequence = 0
        self._last_transport_facts = {}
        self._meaning_claim_lock = threading.RLock()
        self._poll_cycle_in_progress = False

    def __repr__(self):
        return "<NativeSessionLifecycle redacted>"

    @contextmanager
    def native_meaning_claim_window(self):
        """Local scheduling only; hold across a SQLite claim, never a model.

        A poll owns its in-progress marker before prepare/network begins and
        until its failure budget has been recorded. Only a strictly ready
        lifecycle permits entering the unchanged Source/Registry claim gate.
        Other states veto a new claim; this grants no recovery or permission.
        """
        with self._meaning_claim_lock:
            can_claim = False
            if self._poll_cycle_in_progress is False:
                try:
                    status = project_native_session_status(self.safe_status())
                    can_claim = (status['state'] == 'ready'
                        and status['reason_code'] is None
                        and status['transient_failures'] == 0
                        and status['last_transient_code'] is None
                        and status['retry_after_seconds'] == 0)
                except SessionLifecycleError:
                    pass  # Unknown health denies a claim; it is not a retry.
            yield can_claim

    def _owner_guard(self):
        if threading.get_ident() != self._owner:
            _fail("session_owner_required")
        if self._blocked:
            _fail(self._blocked)
        if self._cooldown_clock() < self._cooldown_until:
            _fail("session_cooldown")

    def _record_poll_failure(self, code, *, transport_facts=None):
        """Exactly one failed cycle; no automatic auth/draft/vault recovery."""
        if threading.get_ident() != self._owner:
            _fail("session_owner_required")
        self._last_transport_facts = project_native_transport_facts(transport_facts)
        self._pinned = None
        if self._blocked or code == "session_cooldown":
            return
        normalized = SessionLifecycleError(code).code
        self._failure_sequence += 1
        if normalized in _TRANSIENT_CODES:
            self._transient_failures += 1
            self._last_transient = normalized
            if self._transient_failures <= len(_TRANSIENT_DELAYS):
                self._cooldown_until = self._cooldown_clock() + _TRANSIENT_DELAYS[self._transient_failures - 1]
                return
            self._blocked = "session_transient_retry_exhausted"
        else:
            self._blocked = normalized
        self._active = None
        self._cooldown_until = 0.0

    def _record_poll_success(self):
        self._last_transport_facts = {}
        self._transient_failures = 0
        self._last_transient = None
        self._cooldown_until = 0.0

    def _initial(self):
        """Only normal absence/expiry authorizes same-context refresh."""
        try:
            return self._owned.load(), "owned"
        except Exception as error:
            code = getattr(error, "code", None)
            if code not in {"authorization_not_provisioned", "authorization_expired"}:
                raise _translated(error) from None
            if code == "authorization_not_provisioned" and self._legacy is not None and not self._legacy_attempted:
                self._legacy_attempted = True
                try:
                    return self._legacy(), "legacy_once"
                except Exception as legacy_error:
                    if getattr(legacy_error, "code", None) not in {"authorization_not_provisioned", "authorization_expired"}:
                        raise _translated(legacy_error) from None
        return None, "none"

    def prepare_cycle(self):
        self._owner_guard()
        if self._pinned is not None:
            _fail("session_cycle_active")
        try:
            self._capture.guard()
            candidate, source = self._active, self._source
            if not self._loaded:
                self._loaded = True
                candidate, source = self._initial()
            now, refreshed = self._clock(), False
            if candidate is not None and type(candidate) is not SecretSession:
                _fail("session_candidate_invalid")
            if candidate is None or candidate.expires_at <= now + self._refresh_before:
                self._refreshes += 1
                candidate, source, refreshed = self._capture.capture(), "same_context", True
            if (type(candidate) is not SecretSession or candidate.expected_uid != self.expected_uid
                    or candidate.expires_at <= now + 60):
                _fail("session_candidate_invalid")
            if candidate.expires_at > now + self._ttl_cap:
                candidate = SecretSession(candidate._cookie_header, candidate._user_agent,
                                          candidate.expected_uid, now + self._ttl_cap)
            if candidate is not self._active:
                # A configured expected_uid is not a proof. Only the actual
                # query/user result of this exact snapshot permits activation.
                verifier = NativeTransport(lambda: candidate, self.expected_uid,
                                           network=self._network, clock=self._clock)
                if verifier.verify_identity() != self.expected_uid:
                    _fail("session_identity_mismatch")
                self._capture.guard()
                if refreshed and self._seal:
                    try:
                        self._owned.save(candidate)
                        self._seal_state = "sealed"
                    except Exception:
                        self._seal_state = "failed"
                        if self._require_seal:
                            _fail("session_refresh_unavailable")
                self._active, self._source = candidate, source
                self._generation += 1
            self._pinned = self._active
            # A successful identity prepare clears old diagnostics without
            # resetting the separate downstream-poll retry budget.
            self._last_transport_facts = {}
            return self.safe_status()
        except Exception as error:
            translated = _translated(error)
            self._pinned = None
            self._record_poll_failure(translated.code, transport_facts=translated.transport_facts)
            raise translated from None

    def provider(self):
        """Transport calls this repeatedly; no context/file/network reads here."""
        self._owner_guard()
        if self._pinned is None:
            _fail("session_cycle_not_active")
        if self._pinned.expires_at <= self._clock():
            self._blocked = "authorization_expired"
            self._active = None
            _fail("authorization_expired")
        return self._pinned

    def end_cycle(self):
        if threading.get_ident() != self._owner:
            _fail("session_owner_required")
        self._pinned = None

    def pause(self, code):
        """Explicit sticky pause; ordinary poll failures are owned by cycle()."""
        if threading.get_ident() != self._owner:
            _fail("session_owner_required")
        if self._pinned is not None:
            _fail("session_cycle_active")
        self._last_transport_facts = {}
        self._blocked = SessionLifecycleError(code).code
        self._active = None

    def resume_after_explicit_start(self):
        """Owner hook ONLY after the existing human Start/intervention gate.

        Not called by a timer or provider. Damaged/unsafe vault state requires
        repair/restart rather than this auth recovery. This does not reset a
        queue draft, original send reservation, native history fence or source.
        """
        if threading.get_ident() != self._owner:
            _fail("session_owner_required")
        if self._pinned is not None:
            _fail("session_cycle_active")
        allowed = {None, "verification_required", "session_invalid", "session_identity_mismatch",
                   "authorization_expired", "session_expired", "session_existing_draft",
                   "session_cookie_expired", "session_cookie_expiring", "session_transient_retry_exhausted"}
        # Only this explicit human finish/Start hook may recheck a previous
        # identity API business rejection. This is not identity success: the
        # existing same-context capture, fixed UID GET, sealing and receiver
        # checkpoint still have to pass. Missing/other-role facts stay blocked.
        facts = self._last_transport_facts
        identity_api_recheck = (self._blocked == "api_error"
            and type(facts) is dict and set(facts) == {"endpoint_role", "http_status", "api_status_code"}
            and facts["endpoint_role"] == "identity"
            and type(facts["http_status"]) is int and facts["http_status"] == 200
            and type(facts["api_status_code"]) is int and facts["api_status_code"] != 0)
        if self._blocked not in allowed and not identity_api_recheck:
            _fail(self._blocked)
        # Recheck normally; no refresh if the draft/challenge still exists.
        self._capture.guard()
        self._blocked, self._active, self._pinned = None, None, None
        self._loaded = True  # Recovery refreshes this context, not stale capsule.
        self._record_poll_success()

    @contextmanager
    def cycle(self, *, reset_failures_on_success=True):
        if type(reset_failures_on_success) is not bool:
            _fail("session_configuration_invalid")
        if threading.get_ident() != self._owner:
            _fail("session_owner_required")
        if self._pinned is not None:
            _fail("session_cycle_active")
        with self._meaning_claim_lock:
            self._poll_cycle_in_progress = True
        previous_failure = self._failure_sequence
        try:
            self.prepare_cycle()
            yield self
        except Exception as error:
            # prepare_cycle already records its own failure. A downstream
            # NativeReceiver error is recorded here exactly once after unpinning.
            self._pinned = None
            if self._failure_sequence == previous_failure:
                self._record_poll_failure(getattr(error, "code", None),
                    transport_facts=getattr(error, "transport_facts", None))
            raise
        else:
            # An identity prepare alone does not reset the retry budget; only
            # the entire downstream poll succeeding does. A separately scoped
            # owner identity checkpoint pins normally but passes False here.
            if reset_failures_on_success:
                self._record_poll_success()
        finally:
            try:
                self.end_cycle()
            finally:
                with self._meaning_claim_lock:
                    self._poll_cycle_in_progress = False

    def safe_status(self):
        result = {"state": "paused" if self._blocked else "cooldown" if self._cooldown_clock() < self._cooldown_until else "cycle_active" if self._pinned is not None else "ready" if self._active is not None else "not_started",
                "reason_code": self._blocked, "generation": self._generation,
                "refresh_count": self._refreshes, "source": self._source,
                "seal_state": self._seal_state, "transient_failures": self._transient_failures,
                "last_transient_code": self._last_transient,
                "retry_after_seconds": max(0, math.ceil(self._cooldown_until - self._cooldown_clock()))}
        if self._last_transport_facts:
            result["transport_facts"] = dict(self._last_transport_facts)
        return result
