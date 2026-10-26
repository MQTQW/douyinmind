"""Production lifecycle: invented account data, fake browser/network, temp vault."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import threading
import unittest
import urllib.parse

from app.assistant.native_session import NativeSessionVault
from app.assistant.native_transport import NativeTransport, SecretSession
from app.assistant.native_lifecycle import (NativeSessionLifecycle, SameContextCapture,
                                          SessionLifecycleError, visible_draft_is_empty,
                                          project_native_session_status as candidate_status)

UID, PROFILE = "123456789", "https://www.douyin.com/user/fixture_B_owned"


class FakeProtector:
    def protect(self, value, entropy):
        return b"fixture-sealed-not-dpapi:" + value
    def unprotect(self, value, entropy):
        if not value.startswith(b"fixture-sealed-not-dpapi:"):
            raise ValueError("fixture confidential detail must not propagate")
        return value[len(b"fixture-sealed-not-dpapi:"):]


class FakeMetadata:
    def __init__(self, owner): self.owner = owner
    def send(self, method, parameters):
        if method != "Browser.getVersion" or parameters != {}:
            raise AssertionError("unexpected metadata command")
        self.owner.metadata_calls += 1
        return {"userAgent": self.owner.ua}
    def detach(self): self.owner.detach_calls += 1


class FakeBrowser:
    def __init__(self, context): self.context = context
    def new_browser_cdp_session(self):
        self.context.metadata_sessions += 1
        return FakeMetadata(self.context)


class FakeContext:
    def __init__(self, now):
        self.now = now
        self.cookie_calls = self.metadata_calls = self.metadata_sessions = self.detach_calls = 0
        self.ua = "fixture-UA"
        self.browser = FakeBrowser(self)
        self.rows = [{"name": "fixture_session", "value": "invented_fixture_only", "domain": ".douyin.com",
                      "path": "/", "expires": -1, "httpOnly": True, "secure": True}]
    def cookies(self, urls):
        expected = ["https://www.douyin.com/aweme/v1/web/" + route for route in
                    ("query/user/", "notice/count/", "notice/", "notice/detail/")]
        if urls != expected: raise AssertionError("cookie scope widened")
        self.cookie_calls += 1
        return deepcopy(self.rows)


class FakePage:
    def __init__(self, context): self.context = context


class FakeAdapter:
    def __init__(self, context):
        self.page = FakePage(context)
        self.expected_profile_url = PROFILE
        self.challenge = False
        self.checks = 0
    def check_intervention(self):
        self.checks += 1
        if self.challenge:
            raise ValueError("fixture page challenge text is not an error payload")


class FakeNetwork:
    def __init__(self): self.uid, self.calls, self.after_request, self.failure = UID, 0, None, None
    def __call__(self, url, headers, timeout, max_bytes):
        if urllib.parse.urlsplit(url).path != "/aweme/v1/web/query/user/":
            raise AssertionError("candidate verifier used a non-identity endpoint")
        if self.failure: raise self.failure
        self.calls += 1
        result = 200, json.dumps({"status_code": 0, "user_uid": self.uid}).encode()
        if self.after_request: self.after_request()
        return result


class CodeError(RuntimeError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


class CandidateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root, self.now = Path(self.temp.name).resolve(), [1000.0]
        self.vault = NativeSessionVault(UID, private_root=self.root / "private", protector=FakeProtector(),
                                        clock=lambda: self.now[0])
        self.context = FakeContext(self.now)
        self.adapter, self.network = FakeAdapter(self.context), FakeNetwork()
        self.empty = True
        self.capture = SameContextCapture(self.adapter, PROFILE, UID, draft_is_empty=lambda: self.empty,
                                           clock=lambda: self.now[0])
        self.legacy_calls = 0

    def lifecycle(self, **options):
        return NativeSessionLifecycle(expected_uid=UID, owned_vault=self.vault, context_capture=self.capture,
                                      network=self.network, clock=lambda: self.now[0], **options)

    def test_construction_has_no_capture_network_or_capsule(self):
        lifecycle = self.lifecycle()
        self.assertEqual(self.context.cookie_calls, 0)
        self.assertEqual(self.network.calls, 0)
        self.assertEqual(list(self.root.iterdir()), [])
        self.assertEqual(lifecycle.safe_status()["state"], "not_started")

    def test_context_refresh_verifies_then_seals_reopen_owned(self):
        lifecycle = self.lifecycle()
        with lifecycle.cycle():
            snapshot = lifecycle.provider()
            self.assertEqual(snapshot.expected_uid, UID)
            self.assertEqual(snapshot.expires_at, 4600)
        self.assertEqual(self.context.cookie_calls, 1)
        self.assertEqual(self.context.metadata_calls, 1)
        self.assertEqual(self.context.detach_calls, 1)
        self.assertEqual(self.vault.load().expires_at, 4600)
        self.assertEqual(lifecycle.safe_status()["seal_state"], "sealed")

    def test_provider_never_reads_context_vault_or_network_during_cycle(self):
        lifecycle = self.lifecycle()
        with lifecycle.cycle():
            snapshot = lifecycle.provider()
            before = (self.adapter.checks, self.context.cookie_calls, self.context.metadata_calls, self.network.calls)
            for _ in range(10): self.assertIs(lifecycle.provider(), snapshot)
            self.assertEqual((self.adapter.checks, self.context.cookie_calls, self.context.metadata_calls, self.network.calls), before)
            # Real fixed transport, all network injected, repeated identity reads
            # use the same candidate object and never recapture cookies.
            transport = NativeTransport(lifecycle.provider, UID, self.network, clock=lambda: self.now[0])
            for _ in range(3): self.assertEqual(transport.verify_identity(), UID)
            self.assertEqual(self.context.cookie_calls, 1)

    def test_owned_preferred_over_legacy_and_capture(self):
        self.vault.save(SecretSession("fixture=owned", "fixture-UA", UID, 1900))
        def legacy():
            self.legacy_calls += 1
            return SecretSession("fixture=legacy", "fixture-UA", UID, 2000)
        lifecycle = self.lifecycle(legacy_loader=legacy)
        with lifecycle.cycle(): self.assertEqual(lifecycle.provider().expires_at, 1900)
        self.assertEqual(self.legacy_calls, 0)
        self.assertEqual(self.context.cookie_calls, 0)
        self.assertEqual(lifecycle.safe_status()["source"], "owned")

    def test_legacy_once_keeps_original_expiry_and_is_not_copied_to_owned(self):
        def legacy():
            self.legacy_calls += 1
            return SecretSession("fixture=legacy", "fixture-UA", UID, 1800)
        lifecycle = self.lifecycle(legacy_loader=legacy)
        with lifecycle.cycle(): self.assertEqual(lifecycle.provider().expires_at, 1800)
        with lifecycle.cycle(): self.assertEqual(lifecycle.provider().expires_at, 1800)
        self.assertEqual(self.legacy_calls, 1)
        self.assertFalse(self.vault.path.exists())
        self.assertEqual(lifecycle.safe_status()["source"], "legacy_once")

    def test_expired_owned_refreshes_context_without_legacy_fallback(self):
        self.vault.save(SecretSession("fixture=owned", "fixture-UA", UID, 1400))
        self.now[0] = 1500
        def legacy():
            self.legacy_calls += 1
            return SecretSession("fixture=legacy", "fixture-UA", UID, 8000)
        lifecycle = self.lifecycle(legacy_loader=legacy)
        with lifecycle.cycle(): self.assertEqual(lifecycle.provider().expires_at, 5100)
        self.assertEqual(self.legacy_calls, 0)
        self.assertEqual(self.context.cookie_calls, 1)

    def test_expired_legacy_normal_context_refresh_only_once(self):
        def legacy():
            self.legacy_calls += 1
            from app.assistant.native_session import NativeSessionError
            raise NativeSessionError("authorization_expired")
        lifecycle = self.lifecycle(legacy_loader=legacy)
        with lifecycle.cycle(): self.assertEqual(lifecycle.provider().expected_uid, UID)
        with lifecycle.cycle(): self.assertEqual(lifecycle.provider().expected_uid, UID)
        self.assertEqual(self.legacy_calls, 1)
        self.assertEqual(self.context.cookie_calls, 1)

    def test_owned_corruption_stops_without_legacy_or_overwrite(self):
        self.vault.path.parent.mkdir(parents=True)
        self.vault.path.write_bytes(b"invented-corrupt-capsule")
        before = self.vault.path.read_bytes()
        def legacy():
            self.legacy_calls += 1
            return SecretSession("fixture=legacy", "fixture-UA", UID, 8000)
        lifecycle = self.lifecycle(legacy_loader=legacy)
        with self.assertRaisesRegex(SessionLifecycleError, "capsule_invalid"):
            lifecycle.prepare_cycle()
        self.assertEqual(self.context.cookie_calls, 0)
        self.assertEqual(self.legacy_calls, 0)
        self.assertEqual(self.vault.path.read_bytes(), before)
        with self.assertRaisesRegex(SessionLifecycleError, "capsule_invalid"):
            lifecycle.resume_after_explicit_start()

    def test_native_uid_mismatch_never_seals_or_activates(self):
        self.network.uid = "999999999"
        lifecycle = self.lifecycle()
        with self.assertRaisesRegex(SessionLifecycleError, "session_identity_mismatch"):
            lifecycle.prepare_cycle()
        self.assertFalse(self.vault.path.exists())
        self.assertEqual(lifecycle.safe_status()["state"], "paused")
        self.assertEqual(self.context.detach_calls, 1)

    def test_challenge_before_capture_and_after_verify_both_pause(self):
        lifecycle = self.lifecycle()
        self.adapter.challenge = True
        with self.assertRaisesRegex(SessionLifecycleError, "verification_required"):
            lifecycle.prepare_cycle()
        self.assertEqual(self.context.cookie_calls, 0)
        self.adapter.challenge = False
        lifecycle = self.lifecycle()
        self.network.after_request = lambda: setattr(self.adapter, "challenge", True)
        with self.assertRaisesRegex(SessionLifecycleError, "verification_required"):
            lifecycle.prepare_cycle()
        self.assertFalse(self.vault.path.exists())

    def test_nonempty_or_unknown_draft_pauses_without_clearing(self):
        for state, code in ((False, "session_existing_draft"), (None, "session_draft_state_unknown")):
            self.empty = state
            lifecycle = self.lifecycle()
            with self.subTest(code=code), self.assertRaisesRegex(SessionLifecycleError, code):
                lifecycle.prepare_cycle()
            self.assertIs(self.empty, state)
        self.assertEqual(self.context.cookie_calls, 0)

    def test_same_context_and_owner_are_required(self):
        lifecycle = self.lifecycle()
        self.adapter.page = FakePage(FakeContext(self.now))
        with self.assertRaisesRegex(SessionLifecycleError, "session_context_changed"):
            lifecycle.prepare_cycle()
        self.assertEqual(self.context.cookie_calls, 0)
        lifecycle = self.lifecycle()
        results = []
        def wrong_thread():
            try: lifecycle.prepare_cycle()
            except SessionLifecycleError as error: results.append(error.code)
        worker = threading.Thread(target=wrong_thread)
        worker.start(); worker.join()
        self.assertEqual(results, ["session_owner_required"])

    def test_near_expiry_refreshes_only_at_next_cycle_boundary(self):
        lifecycle = self.lifecycle()
        with lifecycle.cycle(): first = lifecycle.provider()
        self.now[0] = 4400
        with lifecycle.cycle():
            self.assertIsNot(lifecycle.provider(), first)
            self.assertEqual(lifecycle.provider().expires_at, 8000)
        self.assertEqual(self.context.cookie_calls, 2)

    def test_expiry_inside_cycle_never_recaptures(self):
        lifecycle = self.lifecycle()
        with lifecycle.cycle():
            self.now[0] = 4700
            with self.assertRaisesRegex(SessionLifecycleError, "authorization_expired"):
                lifecycle.provider()
        self.assertEqual(self.context.cookie_calls, 1)
        self.assertEqual(lifecycle.safe_status()["state"], "paused")

    def test_actual_cookie_expiry_caps_and_session_cookie_is_finite(self):
        self.context.rows[0]["expires"] = 1750
        lifecycle = self.lifecycle()
        with lifecycle.cycle(): self.assertEqual(lifecycle.provider().expires_at, 1750)
        self.context.rows[0]["expires"] = -1
        lifecycle = self.lifecycle()
        # Existing owned 1750 remains preferred and is never extended by load.
        with lifecycle.cycle(): self.assertEqual(lifecycle.provider().expires_at, 1750)

    def test_duplicate_names_or_foreign_partition_reject_and_detach(self):
        second = deepcopy(self.context.rows[0]); second["value"] = "different_fixture"
        self.context.rows.append(second)
        with self.assertRaisesRegex(SessionLifecycleError, "session_cookie_ambiguous"):
            self.lifecycle().prepare_cycle()
        self.assertEqual(self.context.detach_calls, 1)
        self.context.rows = [second]; second["partitionKey"] = "https://foreign.invalid"
        with self.assertRaisesRegex(SessionLifecycleError, "session_cookie_scope_unknown"):
            self.lifecycle().prepare_cycle()

    def test_single_header_rejects_cookie_path_not_covering_all_endpoints(self):
        for path in ("/aweme/v1/web/query/user/", "/aweme/v1/web/notice/", "/a"):
            self.context.rows[0]["path"] = path
            with self.subTest(path=path), self.assertRaisesRegex(SessionLifecycleError, "session_cookie_scope_unknown"):
                self.capture.capture()
        self.assertFalse(self.vault.path.exists())

        for path in ("/", "/aweme", "/aweme/v1/web", "/aweme/v1/web/"):
            self.context.rows[0]["path"] = path
            self.assertEqual(self.capture.capture().expected_uid, UID)

    def test_no_plaintext_fallback_when_optional_seal_fails(self):
        original_save = self.vault.save
        def broken(snapshot): raise OSError("fixture secret path must not escape")
        self.vault.save = broken
        lifecycle = self.lifecycle()
        with lifecycle.cycle(): self.assertEqual(lifecycle.provider().expected_uid, UID)
        self.assertEqual(lifecycle.safe_status()["seal_state"], "failed")
        self.assertFalse(self.vault.path.exists())
        with self.assertRaisesRegex(SessionLifecycleError, "session_refresh_unavailable"):
            self.lifecycle(require_seal=True).prepare_cycle()
        self.vault.save = original_save

    def test_failure_messages_and_safe_status_do_not_leak_headers_or_ua(self):
        self.network.failure = RuntimeError("fixture confidential cookie=NEVER-OUTPUT")
        lifecycle = self.lifecycle()
        with self.assertRaises(SessionLifecycleError) as error:
            lifecycle.prepare_cycle()
        self.assertEqual(str(error.exception), "network_error")
        public = json.dumps(lifecycle.safe_status()) + repr(lifecycle) + repr(self.capture)
        for secret in ("invented_fixture_only", "fixture-UA", "NEVER-OUTPUT", "www.douyin.com"):
            self.assertNotIn(secret, public)

    def test_explicit_start_only_recovery_recaptures_after_empty_draft(self):
        lifecycle = self.lifecycle()
        self.empty = False
        with self.assertRaisesRegex(SessionLifecycleError, "session_existing_draft"):
            lifecycle.prepare_cycle()
        self.empty = True
        # The timer/provider cannot silently dismiss the pause.
        with self.assertRaisesRegex(SessionLifecycleError, "session_existing_draft"):
            lifecycle.prepare_cycle()
        lifecycle.resume_after_explicit_start()
        with lifecycle.cycle(): self.assertEqual(lifecycle.provider().expected_uid, UID)

    def test_later_transport_failure_invalidates_cache_until_explicit_start(self):
        lifecycle = self.lifecycle()
        with lifecycle.cycle(): pass
        lifecycle.pause("session_invalid")
        with self.assertRaisesRegex(SessionLifecycleError, "session_invalid"):
            lifecycle.prepare_cycle()
        lifecycle.resume_after_explicit_start()
        with lifecycle.cycle(): pass
        self.assertEqual(self.context.cookie_calls, 2)

    def test_cycle_hook_always_releases_pin_after_downstream_error(self):
        lifecycle = self.lifecycle()
        with self.assertRaisesRegex(ValueError, "fixture downstream"):
            with lifecycle.cycle(): raise ValueError("fixture downstream")
        with self.assertRaisesRegex(SessionLifecycleError, "session_refresh_unavailable"):
            lifecycle.provider()
        self.assertNotEqual(lifecycle.safe_status()["state"], "cycle_active")

    def test_transient_prepare_failure_cools_down_then_recovers_without_double_count(self):
        lifecycle = self.lifecycle()
        self.network.failure = TimeoutError("invented transient detail must not escape")
        with self.assertRaises(SessionLifecycleError):
            with lifecycle.cycle(): pass
        self.assertEqual(lifecycle.safe_status()["state"], "cooldown")
        self.assertEqual(lifecycle.safe_status()["transient_failures"], 1)
        captures = self.context.cookie_calls
        with self.assertRaisesRegex(SessionLifecycleError, "session_cooldown"):
            with lifecycle.cycle(): pass
        self.assertEqual(self.context.cookie_calls, captures)
        self.assertEqual(lifecycle.safe_status()["transient_failures"], 1)
        self.network.failure = None
        self.now[0] += 45
        with lifecycle.cycle(): self.assertEqual(lifecycle.provider().expected_uid, UID)
        self.assertEqual(lifecycle.safe_status()["transient_failures"], 0)
        self.assertEqual(lifecycle.safe_status()["state"], "ready")

    def test_downstream_retry_budget_survives_successful_prepare_and_exhausts(self):
        lifecycle = self.lifecycle()
        for attempt, delay in ((1, 45), (2, 90), (3, 0)):
            with self.assertRaisesRegex(CodeError, "network_error"):
                with lifecycle.cycle():
                    self.assertEqual(lifecycle.provider().expected_uid, UID)
                    raise CodeError("network_error")
            status = lifecycle.safe_status()
            self.assertEqual(status["transient_failures"], attempt)
            if delay:
                self.assertEqual(status["state"], "cooldown")
                self.assertEqual(status["retry_after_seconds"], delay)
                self.now[0] += delay
            else:
                self.assertEqual(status["state"], "paused")
                self.assertEqual(status["reason_code"], "session_transient_retry_exhausted")
        self.assertEqual(self.context.cookie_calls, 1)
        self.now[0] += 10000
        with self.assertRaisesRegex(SessionLifecycleError, "session_transient_retry_exhausted"):
            lifecycle.prepare_cycle()

    def test_each_transient_family_has_bounded_cooldown(self):
        for code in ("network_error", "network_timeout", "rate_limited", "http_error", "empty_response", "session_cookie_expiring"):
            lifecycle = self.lifecycle()
            with self.subTest(code=code), self.assertRaises(CodeError):
                with lifecycle.cycle(): raise CodeError(code)
            self.assertEqual(lifecycle.safe_status()["state"], "cooldown")
            self.assertEqual(lifecycle.safe_status()["last_transient_code"], code)

    def test_auth_draft_uid_and_tls_are_sticky_never_timer_recovered(self):
        for code in ("verification_required", "session_existing_draft", "session_identity_mismatch", "capsule_invalid", "tls_error"):
            lifecycle = self.lifecycle()
            with self.subTest(code=code), self.assertRaises(CodeError):
                with lifecycle.cycle(): raise CodeError(code)
            self.assertEqual(lifecycle.safe_status()["state"], "paused")
            self.now[0] += 10000
            captures = self.context.cookie_calls
            with self.assertRaisesRegex(SessionLifecycleError, code):
                with lifecycle.cycle(): pass
            self.assertEqual(self.context.cookie_calls, captures)

    def test_cooldown_success_reuses_pinned_cache_without_recapture(self):
        lifecycle = self.lifecycle()
        with lifecycle.cycle(): snapshot = lifecycle.provider()
        with self.assertRaises(CodeError):
            with lifecycle.cycle(): raise CodeError("rate_limited")
        before = self.context.cookie_calls
        self.now[0] += 45
        with lifecycle.cycle(): self.assertIs(lifecycle.provider(), snapshot)
        self.assertEqual(self.context.cookie_calls, before)
        self.assertEqual(lifecycle.safe_status()["transient_failures"], 0)

    def test_nested_cycle_rejection_does_not_clear_active_snapshot(self):
        lifecycle = self.lifecycle()
        with lifecycle.cycle():
            snapshot = lifecycle.provider()
            with self.assertRaisesRegex(SessionLifecycleError, "session_cycle_active"):
                with lifecycle.cycle(): pass
            self.assertIs(lifecycle.provider(), snapshot)

    def test_retry_exhaustion_requires_explicit_start_before_new_capture(self):
        lifecycle = self.lifecycle()
        for delay in (45, 90, 0):
            with self.assertRaises(CodeError):
                with lifecycle.cycle(): raise CodeError("http_error")
            self.now[0] += delay
        captures = self.context.cookie_calls
        lifecycle.resume_after_explicit_start()
        with lifecycle.cycle(): self.assertEqual(lifecycle.provider().expected_uid, UID)
        self.assertEqual(self.context.cookie_calls, captures + 1)
        self.assertEqual(lifecycle.safe_status()["transient_failures"], 0)

    def test_identity_only_success_pins_but_preserves_full_poll_failure_budget(self):
        from app.assistant.native_receiver import NativeReceiver
        lifecycle = self.lifecycle()
        with lifecycle.cycle(): pass
        with self.assertRaises(CodeError):
            with lifecycle.cycle(): raise CodeError("network_error")
        self.now[0] += 45
        class CheckpointStoreFixture:
            receiver_uid, receiver_profile_url = UID, PROFILE
            updates = 0
            def set_verified(self): self.updates += 1
            def pause(self, code): raise AssertionError("unexpected fake checkpoint pause")
        store = CheckpointStoreFixture()
        transport = NativeTransport(lifecycle.provider, UID, self.network, clock=lambda: self.now[0])
        receiver = NativeReceiver(transport, store, receiver_uid=UID,
                    receiver_sec_uid=PROFILE.rsplit("/", 1)[1], profile_allowed=lambda profile: True)
        before = self.network.calls, self.context.cookie_calls
        with lifecycle.cycle(reset_failures_on_success=False):
            snapshot = lifecycle.provider()
            self.assertTrue(receiver.reverify_identity_checkpoint())
            self.assertIs(lifecycle.provider(), snapshot)
        self.assertEqual((self.network.calls - before[0], self.context.cookie_calls - before[1]), (1, 0))
        self.assertEqual(store.updates, 1)
        self.assertEqual(lifecycle.safe_status()["transient_failures"], 1)
        with self.assertRaisesRegex(SessionLifecycleError, "session_cycle_not_active"):
            lifecycle.provider()
        with lifecycle.cycle(): pass
        self.assertEqual(lifecycle.safe_status()["transient_failures"], 0)

    def test_false_cycle_failure_is_still_counted_once(self):
        lifecycle = self.lifecycle()
        with self.assertRaises(CodeError):
            with lifecycle.cycle(reset_failures_on_success=False): raise CodeError("http_error")
        self.assertEqual(lifecycle.safe_status()["transient_failures"], 1)
        self.assertEqual(lifecycle.safe_status()["state"], "cooldown")

    def test_false_prepare_failure_is_counted_once_and_unpinned(self):
        lifecycle = self.lifecycle()
        self.network.failure = TimeoutError("synthetic only")
        with self.assertRaises(SessionLifecycleError):
            with lifecycle.cycle(reset_failures_on_success=False): pass
        self.assertEqual(lifecycle.safe_status()["transient_failures"], 1)
        self.assertEqual(self.context.cookie_calls, 1)
        self.assertIsNone(lifecycle._pinned)

    def test_cycle_reset_switch_requires_an_explicit_bool(self):
        lifecycle = self.lifecycle()
        for value in (0, 1, None, "False"):
            with self.subTest(value_type=type(value).__name__), self.assertRaisesRegex(SessionLifecycleError, "session_configuration_invalid"):
                with lifecycle.cycle(reset_failures_on_success=value): pass
        self.assertEqual(self.context.cookie_calls, 0)
        self.assertEqual(self.network.calls, 0)

    def test_identity_success_does_not_erase_budget_before_next_failed_poll(self):
        lifecycle = self.lifecycle()
        with self.assertRaises(CodeError):
            with lifecycle.cycle(): raise CodeError("rate_limited")
        self.now[0] += 45
        with lifecycle.cycle(reset_failures_on_success=False): pass
        with self.assertRaises(CodeError):
            with lifecycle.cycle(): raise CodeError("rate_limited")
        self.assertEqual(lifecycle.safe_status()["transient_failures"], 2)
        self.assertEqual(lifecycle.safe_status()["retry_after_seconds"], 90)

    def normal_browser_cookie_expiry(self):
        original = self.context.cookies
        def cookies_from_normal_context(urls):
            # Simulate the browser API's ordinary expiry behavior. The product
            # never filters/drops cookie rows or changes their expiry itself.
            return [row for row in original(urls) if row["expires"] == -1 or row["expires"] > self.now[0]]
        self.context.cookies = cookies_from_normal_context

    def test_short_cookie_waits_for_normal_browser_expiry_then_verifies_and_seals(self):
        lifecycle = self.lifecycle()
        with lifecycle.cycle(): old_snapshot = lifecycle.provider()
        old_expiry = old_snapshot.expires_at
        self.now[0] = 4400
        self.context.rows.append({"name": "fixture_short", "value": "invented_short_only",
            "domain": ".douyin.com", "path": "/", "expires": self.now[0] + 50})
        self.normal_browser_cookie_expiry()
        with self.assertRaisesRegex(SessionLifecycleError, "session_cookie_expiring"):
            with lifecycle.cycle(): pass
        self.assertIs(lifecycle._active, old_snapshot)
        self.assertEqual(old_snapshot.expires_at, old_expiry)
        status = lifecycle.safe_status()
        self.assertEqual(status["state"], "cooldown")
        self.assertEqual(candidate_status(status)["last_transient_code"], "session_cookie_expiring")
        captures = self.context.cookie_calls
        with self.assertRaisesRegex(SessionLifecycleError, "session_cooldown"):
            lifecycle.provider()
        self.assertEqual(self.context.cookie_calls, captures)
        self.now[0] += 45
        with self.assertRaisesRegex(SessionLifecycleError, "session_cookie_expiring"):
            with lifecycle.cycle(): pass
        self.now[0] += 90
        before_queries = self.network.calls
        with lifecycle.cycle(): new_snapshot = lifecycle.provider()
        self.assertEqual(self.network.calls - before_queries, 1)
        self.assertIsNot(new_snapshot, old_snapshot)
        self.assertEqual(old_snapshot.expires_at, old_expiry)
        self.assertEqual(self.vault.load().expires_at, new_snapshot.expires_at)
        status = lifecycle.safe_status()
        self.assertEqual((status["state"], status["generation"], status["seal_state"]), ("ready", 2, "sealed"))
        self.assertEqual(status["transient_failures"], 0)

    def test_short_cookie_recovery_does_not_hide_real_authentication_failure(self):
        lifecycle = self.lifecycle()
        with lifecycle.cycle(): pass
        sealed_before = self.vault.path.read_bytes()  # Synthetic temporary data.
        self.now[0] = 4400
        self.context.rows.append({"name": "fixture_short", "value": "invented_short_only",
            "domain": ".douyin.com", "path": "/", "expires": self.now[0] + 30})
        self.normal_browser_cookie_expiry()
        with self.assertRaisesRegex(SessionLifecycleError, "session_cookie_expiring"):
            with lifecycle.cycle(): pass
        self.now[0] += 45
        lifecycle._network = lambda *args: (401, b"")
        with self.assertRaisesRegex(SessionLifecycleError, "session_invalid"):
            with lifecycle.cycle(): pass
        self.assertEqual(lifecycle.safe_status()["state"], "paused")
        self.assertEqual(lifecycle.safe_status()["reason_code"], "session_invalid")
        self.assertEqual(self.vault.path.read_bytes(), sealed_before)
        with self.assertRaisesRegex(SessionLifecycleError, "session_invalid"):
            lifecycle.prepare_cycle()

    def test_repeated_expiring_cookie_exhausts_existing_budget_without_uid_or_seal(self):
        original = self.context.cookies
        def normal_context_with_rotating_short_cookie(urls):
            rows = original(urls)
            rows.append({"name": "fixture_rotating", "value": "invented_fixture_only",
                "domain": ".douyin.com", "path": "/", "expires": self.now[0] + 50})
            return rows
        self.context.cookies = normal_context_with_rotating_short_cookie
        lifecycle = self.lifecycle()
        for delay in (45, 90, 0):
            with self.assertRaisesRegex(SessionLifecycleError, "session_cookie_expiring"):
                with lifecycle.cycle(): pass
            self.now[0] += delay
        self.assertEqual(lifecycle.safe_status()["reason_code"], "session_transient_retry_exhausted")
        self.assertEqual(candidate_status(lifecycle.safe_status())["transient_failures"], 3)
        self.assertEqual(self.network.calls, 0)
        self.assertFalse(self.vault.path.exists())


class DraftReadTests(unittest.TestCase):
    class Locator:
        def __init__(self, page, kind, index=None): self.page, self.kind, self.index = page, kind, index
        def and_(self, other): return self
        def count(self):
            if self.kind == "body": return self.page.body_count
            if self.kind == "unknown": return self.page.unknown_count
            return len(self.page.texts)
        def nth(self, index): return DraftReadTests.Locator(self.page, "editor", index)
        def inner_text(self, timeout):
            if self.page.read_error: raise RuntimeError("invented draft detail not exported")
            return self.page.texts[self.index]

    class Page:
        def __init__(self, texts=(), unknown=0, body=1, closed=False, read_error=False):
            self.texts, self.unknown_count, self.body_count = list(texts), unknown, body
            self.closed, self.read_error = closed, read_error
        def is_closed(self): return self.closed
        def locator(self, selector):
            kind = "body" if selector == "body" else "unknown" if selector.startswith("textarea") else "editor"
            return DraftReadTests.Locator(self, kind)

    def result(self, **options):
        adapter = type("DraftAdapterFixture", (), {})()
        adapter.page = self.Page(**options)
        return visible_draft_is_empty(adapter)

    def test_zero_editors_on_readable_page_has_no_visible_draft(self):
        self.assertIs(self.result(), True)

    def test_zero_known_but_unknown_editable_is_not_assumed_empty(self):
        self.assertIsNone(self.result(unknown=1))

    def test_closed_or_unreadable_zero_editor_page_is_unknown(self):
        self.assertIsNone(self.result(body=0))
        self.assertIsNone(self.result(closed=True))

    def test_known_empty_and_nonempty_editors(self):
        self.assertIs(self.result(texts=["", " \n "]), True)
        self.assertIs(self.result(texts=["invented draft must remain"]), False)

    def test_excess_or_failed_editor_read_is_unknown(self):
        self.assertIsNone(self.result(texts=[""] * 5))
        self.assertIsNone(self.result(texts=[""], read_error=True))


if __name__ == "__main__": unittest.main()
