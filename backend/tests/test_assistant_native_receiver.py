"""Only synthetic JSON, fake network/protection, temporary app-owned sidecars."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
import urllib.parse

from app.assistant.native_events import NativeEventError, canonical_profile, project_page
from app.assistant.native_receiver import NativeReceiver, NativeReceiverError
from app.assistant.native_session import NativeSessionVault, NativeSessionError
from app.assistant.native_source_store import NativeSourceStore, NativeStoreError
from app.assistant.native_transport import (NativeTransport, NativeTransportError,
                                           SecretSession, strict_json, _stdlib_get)

B, A, SEC_B, SEC_A = "9001", "8001", "fixture_B", "fixture_A"
PROFILE_B, PROFILE_A = canonical_profile(SEC_B), canonical_profile(SEC_A)


def at(nid="1001", cid="2001", *, created="90", uid=A, sec=SEC_A, text="保存视频"):
    return {"nid_str": nid, "type": 45, "user_id": B, "aweme_id": "3001", "create_time": created,
            "at": {"sub_type": 2, "aweme": {"aweme_id": "3001"},
                   "user_info": {"uid": uid, "sec_uid": sec}, "content": text,
                   "comment_status": 1, "comment_unvisible": 0,
                   "schema_url": "aweme://aweme/detail/3001?cid=" + cid}}


def reply(nid="1002", cid="2002", *, created="90", uid=A, sec=SEC_A, text="放到学习库", parent="2001"):
    return {"nid_str": nid, "type": 31, "user_id": B, "create_time": created,
            "comment": {"comment_type": 2, "comment_unvisible": 0, "parent_id": parent,
                        "aweme": {"aweme_id": "3001"}, "comment": {"cid": cid, "aweme_id": "3001",
                        "status": 1, "text": text, "user": {"uid": uid, "sec_uid": sec}}}}


def page(rows=(), more=False, minimum="0", maximum="0"):
    return {"status_code": 0, "notice_list_v2": list(rows), "has_more": more,
            "min_time": minimum, "max_time": maximum}


def project(value, channel):
    return project_page(value, channel, receiver_uid=B, receiver_sec_uid=SEC_B,
                        profile_allowed=lambda profile: profile == PROFILE_A)


class FakeNetwork:
    def __init__(self):
        self.uid = B
        self.groups = [{"type": 9, "group": "601"}, {"type": 7, "group": "2"}]
        self.pages = {"601": page(), "2": page()}
        self.calls = []
        self.override = None

    def __call__(self, url, headers, timeout, max_bytes):
        parsed = urllib.parse.urlsplit(url)
        query = urllib.parse.parse_qs(parsed.query)
        self.calls.append((parsed.path, query))  # No headers or raw URLs retained.
        if self.override is not None:
            return self.override
        if parsed.path.endswith("query/user/"):
            value = {"status_code": 0, "user_uid": self.uid}
        elif parsed.path.endswith("notice/count/"):
            value = {"status_code": 0, "interactive_group": self.groups}
        else:
            group = query["notice_group"][0]
            source = self.pages[group]
            value = source(query) if callable(source) else source
        return 200, json.dumps(value).encode()


class Fixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.now = [100.0]
        self.path = Path(self.temp.name).resolve() / "native" / "sources.sqlite3"
        self.store = NativeSourceStore(self.path, B, PROFILE_B, create=True, clock=lambda: self.now[0])

    def add(self, raw, channel=9, *, live=True, more=False, minimum="0", maximum="0"):
        context = self.store.prepare_channel(channel, "601" if channel == 9 else "2")
        return self.store.record_page(context, project(page([raw], more, minimum, maximum), channel),
                                      next_cursor=(minimum, maximum), has_more=more, allow_live=live)

    def arm(self):
        for channel, group in ((9, "601"), (7, "2")):
            context = self.store.prepare_channel(channel, group)
            self.store.record_page(context, project(page(), channel), next_cursor=("0", "0"), has_more=False)
        self.store.arm_if_ready(True)
        self.store.set_verified()
        self.now[0] = 102.0


class TransportTests(unittest.TestCase):
    def setUp(self):
        self.network = FakeNetwork()
        self.clock = [100.0]
        self.transport = NativeTransport(lambda: SecretSession("fixture-cookie=x", "fixture-agent", B, 1000.0),
                                         B, self.network, clock=lambda: self.clock[0])

    def test_no_import_or_constructor_network(self):
        self.assertEqual(self.network.calls, [])
        self.assertNotIn("fixture-cookie", repr(self.transport))

    def test_groups_are_dynamic_and_mark_read_is_zero(self):
        self.transport.verify_identity()
        self.assertEqual(self.transport.get_groups(), {9: "601", 7: "2"})
        self.transport.read_page("2", ("0", "0"))
        query = self.network.calls[-1][1]
        self.assertEqual(query["is_mark_read"], ["0"])
        self.assertEqual(query["count"], ["10"])

    def test_identity_mismatch(self):
        self.network.uid = "9002"
        with self.assertRaisesRegex(NativeTransportError, "session_identity_mismatch"):
            self.transport.verify_identity()

    def test_duplicate_groups_fail(self):
        self.transport.verify_identity()
        self.network.groups.append({"type": 9, "group": "999"})
        with self.assertRaisesRegex(NativeTransportError, "group_mapping_not_unique"):
            self.transport.get_groups()

    def test_expiry_and_repr_are_safe(self):
        secret = SecretSession("fixture-cookie=x", "fixture-agent", B, 1)
        self.assertNotIn("fixture", repr(secret))
        expired = NativeTransport(lambda: secret, B, self.network, clock=lambda: 100)
        with self.assertRaisesRegex(NativeTransportError, "session_expired"):
            expired.verify_identity()
        self.assertEqual(self.network.calls, [])

    def test_json_strictness_and_full_precision(self):
        self.assertEqual(strict_json(b'{"id":7699727520082952998}')["id"], 7699727520082952998)
        for body, code in ((b'{"x":1,"x":2}', "duplicate_json_key"), (b'{"x":NaN}', "invalid_json_constant"),
                           (b'{"x":' + b'1' * 101 + b'}', "json_integer_limit"), (b'', "empty_response")):
            with self.subTest(code=code), self.assertRaisesRegex(NativeTransportError, code):
                strict_json(body)

    def test_empty_nonterminal_and_stalled_cursor(self):
        self.transport.verify_identity()
        self.transport.get_groups()
        self.network.pages["601"] = page([], True, "1", "2")
        with self.assertRaisesRegex(NativeTransportError, "empty_nonterminal"):
            self.transport.read_page("601", ("0", "0"))
        self.network.pages["601"] = page([at()], True)
        with self.assertRaisesRegex(NativeTransportError, "cursor_stalled"):
            self.transport.read_page("601", ("0", "0"))

    def test_error_responses_do_not_leak(self):
        for response, code in (((302, b"secret"), "redirect_denied"), ((403, b"secret"), "session_invalid"),
                               ((200, b"secret"), "response_not_json"), ((429, b"secret"), "rate_limited")):
            self.network.override = response
            with self.subTest(code=code), self.assertRaises(NativeTransportError) as context:
                self.transport.verify_identity()
            self.assertEqual(str(context.exception), code)

    def test_verification_and_float_uid_fail(self):
        self.network.override = (200, b'{"status_code":0,"verify_required":true}')
        with self.assertRaisesRegex(NativeTransportError, "verification_required"):
            self.transport.verify_identity()
        self.network.override = (200, b'{"status_code":0,"user_uid":9001.0}')
        with self.assertRaisesRegex(NativeTransportError, "identity_schema_unknown"):
            self.transport.verify_identity()

    def test_arbitrary_url_rejected_before_network(self):
        for url in ("http://www.douyin.com/aweme/v1/web/notice/", "https://evil.invalid/aweme/v1/web/notice/",
                    "https://www.douyin.com/aweme/v1/web/notice/?is_mark_read=1"):
            with self.assertRaisesRegex(NativeTransportError, "request_not_allowed"):
                _stdlib_get(url, {}, 1, 100)


class ProjectionTests(unittest.TestCase):
    def test_at_and_reply_remain_distinct_unknown_relations(self):
        for raw, channel in ((at(), 9), (reply(), 7)):
            item = project(page([raw]), channel)["candidates"][0]
            self.assertFalse(item["dispatchable"])
            self.assertIsNone(item["root_comment_id"])
            self.assertIsNone(item["parent_comment_id"])
            self.assertEqual(item["recipient_uid"], B)
            self.assertEqual(item["sender_profile_url"], PROFILE_A)

    def test_unregistered_body_is_not_read(self):
        raw = at(uid="8888", sec="unregistered", text=None)
        raw["at"]["aweme"] = None
        result = project(page([raw]), 9)
        self.assertEqual(result["candidates"], [])
        self.assertNotIn("unregistered", json.dumps(result))

    def test_recipient_and_video_mismatch(self):
        raw = at()
        raw["user_id"] = "9999"
        with self.assertRaisesRegex(NativeEventError, "notice_recipient_mismatch"):
            project(page([raw]), 9)
        raw = reply()
        raw["comment"]["aweme"]["aweme_id"] = "3999"
        with self.assertRaisesRegex(NativeEventError, "notice_video_mismatch"):
            project(page([raw]), 7)

    def test_duplicate_encoded_cid_and_float_not_allowed(self):
        for schema in ("aweme://aweme/detail/3001?cid=2001&cid=2002", "aweme://aweme/detail/3001?%63id=2001", "aweme://aweme/detail/3001?CID=2001"):
            raw = at()
            raw["at"]["schema_url"] = schema
            with self.assertRaises(NativeEventError):
                project(page([raw]), 9)
        raw = reply()
        raw["comment"]["comment"]["cid"] = 2002.0
        with self.assertRaises(NativeEventError):
            project(page([raw]), 7)

    def test_explicit_unavailable_old_item_does_not_block_new_valid(self):
        for channel in (9, 7):
            unavailable = at() if channel == 9 else reply()
            if channel == 9:
                unavailable["at"]["comment_status"] = 0
                unavailable["at"]["aweme"] = None
                unavailable["at"]["content"] = None
            else:
                unavailable["comment"]["comment_unvisible"] = 1
                unavailable["comment"]["comment"]["text"] = None
                unavailable["comment"]["aweme"] = None
            valid = at(nid="1007", cid="2007", created="102") if channel == 9 else reply(nid="1007", cid="2007", created="102")
            result = project(page([unavailable, valid]), channel)
            self.assertEqual(len(result["candidates"]), 1)
            self.assertEqual(result["notices"][0]["skip_reason"], "native_comment_unavailable")
            self.assertEqual(result["candidates"][0]["native_comment_id"], "2007")

    def test_visible_missing_video_fails_and_unknown_visibility_is_quarantined(self):
        raw = at()
        raw["at"]["aweme"] = None
        with self.assertRaises(NativeEventError):
            project(page([raw]), 9)
        raw = at()
        raw["at"]["comment_status"] = False
        result=project(page([raw]), 9)
        self.assertEqual(result['candidates'], [])
        self.assertEqual(result['notices'][0]['skip_reason'], 'native_visibility_unknown')


class StoreTests(Fixture):
    def test_initial_both_channels_are_history(self):
        self.assertEqual(self.add(at(created="999")), [])
        self.assertEqual(self.add(reply(created="999"), 7), [])
        self.assertIsNone(self.store.claim_ui_candidate("owner"))
        self.assertEqual(self.store.safe_status()["counts"], {"historical": 2})

    def test_restart_keeps_fence_cursor_and_dedup(self):
        self.arm()
        first = self.add(at(created="102"), more=True, minimum="10", maximum="20")
        self.assertEqual(len(first), 1)
        reopened = NativeSourceStore(self.path, B, PROFILE_B, clock=lambda: self.now[0])
        context = reopened.prepare_channel(9, "601")
        self.assertEqual(context["cursor"], ("10", "20"))
        reopened.record_page(context, project(page([at(nid="1009", created="102")]), 9), next_cursor=("0", "0"), has_more=False, allow_live=True)
        self.assertEqual(reopened.safe_status()["counts"], {"fresh": 1})

    def test_same_cid_across_channels_is_one_source(self):
        self.arm()
        a = self.add(at(created="102", text="同正文"))[0]
        self.add(reply(cid="2001", created="102", text="同正文"), 7)
        self.assertEqual(self.store.safe_status()["counts"], {"fresh": 1})
        self.assertEqual(self.store.get_source(a["source_id"])["candidate"]["source_kind"], "native_notice_candidate")

    def test_payload_conflict_keeps_original_and_holds(self):
        self.arm()
        item = self.add(at(created="102", text="原文"))[0]
        self.add(at(nid="1008", created="103", text="修改"))
        row = self.store.get_source(item["source_id"])
        self.assertEqual(row["state"], "held_conflict")
        self.assertEqual(row["candidate"]["content"], "原文")
        self.assertIsNone(self.store.claim_ui_candidate("owner"))

    def test_history_never_becomes_fresh_and_missing_time_held(self):
        self.add(at(created="90"))
        self.arm()
        self.add(at(nid="1003", created="103"))
        missing = at(nid="1004", cid="2004")
        del missing["create_time"]
        self.add(missing)
        self.assertEqual(self.store.safe_status()["counts"], {"historical": 2})

    def test_uid_sec_conflict_holds_both_sources(self):
        self.arm()
        item = self.add(at(created="102"))[0]
        self.add(at(nid="1005", cid="2005", created="103", uid="8002"))
        self.assertEqual(self.store.get_source(item["source_id"])["state"], "held_conflict")
        self.assertEqual(self.store.safe_status()["counts"], {"held_conflict": 2})

    def test_same_body_different_cids_ambiguous(self):
        self.arm()
        self.add(at(created="102"))
        self.add(at(nid="1006", cid="2006", created="103"))
        claim = self.store.claim_ui_candidate("owner")
        checked = self.store.check_candidate(claim["source_id"], claim["claim_token"])
        self.assertFalse(checked["valid"])
        self.assertTrue(checked["semantic_conflict"])

    def test_identity_freshness_and_pause(self):
        self.arm()
        self.add(at(created="102"))
        claim = self.store.claim_ui_candidate("owner")
        self.assertTrue(self.store.check_candidate(claim["source_id"], claim["claim_token"])["valid"])
        self.store.pause("verification_required")
        self.assertFalse(self.store.check_candidate(claim["source_id"], claim["claim_token"])["valid"])

    def plan(self, reference="ui-reference"):
        envelope = {"reference_id": reference, "source_mode": "live_ui"}
        return {"task_id": None, "reference_id": reference, "envelope": envelope,
                "thread": {"quality": "ui_verified", "root_reference_id": "root"},
                "queue_item": {"binding": {"id": "binding"}, "envelope": envelope,
                               "authorization": {"user_revision": 1}},
                "native_correlation": {"source_native_comment_id": "2001", "observed_native_comment_id": None, "quality": "semantic_only"}}

    def test_plan_before_admission_recovers_once(self):
        self.arm()
        item = self.add(at(created="102"))[0]
        claim = self.store.claim_ui_candidate("owner")
        self.store.record_ui_plan(claim["source_id"], claim["claim_token"], self.plan())
        self.now[0] += 500
        reopened = NativeSourceStore(self.path, B, PROFILE_B, clock=lambda: self.now[0])
        self.assertIsNone(reopened.claim_ui_candidate("new-owner"))
        plans = reopened.recoverable_ui_plans()
        self.assertEqual(len(plans), 1)
        reopened.finish_ui_admission(item["source_id"], claim["claim_token"], "task-1", "ui-reference")
        reopened.finish_ui_admission(item["source_id"], claim["claim_token"], "task-1", "ui-reference")
        self.assertEqual(reopened.lookup_source_for_task("task-1")["source_id"], item["source_id"])
        self.assertEqual(reopened.recoverable_ui_plans(), [])

    def test_finish_does_not_clear_conflict(self):
        self.arm()
        item = self.add(at(created="102"))[0]
        claim = self.store.claim_ui_candidate("owner")
        self.store.record_ui_plan(claim["source_id"], claim["claim_token"], self.plan())
        self.assertFalse(self.store.admission_current(item["source_id"]))
        self.assertEqual(len(self.store.find_sources_by_ui_reference("ui-reference")), 1)
        self.store.finish_ui_admission(item["source_id"], claim["claim_token"], "task-1", "ui-reference")
        self.assertTrue(self.store.admission_current(item["source_id"]))
        self.add(at(nid="1008", created="103", text="changed"))
        self.store.finish_ui_admission(item["source_id"], claim["claim_token"], "task-1", "ui-reference")
        self.assertEqual(self.store.get_source(item["source_id"])["state"], "held_conflict")
        self.assertFalse(self.store.admission_current(item["source_id"]))

    def test_semantic_whitespace_also_ambiguous(self):
        self.arm()
        item = self.add(at(created="102", text="请 保存 视频"))[0]
        self.add(at(nid="1008", cid="2008", created="103", text="请保存视频"))
        self.assertTrue(self.store.semantic_conflicts(item))

    def test_reverse_cross_channel_keeps_actual_at_notice_identity(self):
        self.arm()
        item = self.add(reply(created="102", cid="2001", text="同正文"), 7)[0]
        self.add(at(nid="1003", created="102", text="同正文"))
        candidate = self.store.get_source(item["source_id"])["candidate"]
        self.assertEqual(candidate["native_notice_id"], "1003")
        self.assertEqual(candidate["source_kind"], "native_notice_candidate")
        self.assertEqual(candidate["parent_id_hint"], "2001")

    def test_registered_other_sender_can_be_projected_and_pinned(self):
        self.arm()
        context = self.store.prepare_channel(9, "601")
        projected = project_page(page([at(uid="8009", sec="registered_other", created="102")]), 9,
                                 receiver_uid=B, receiver_sec_uid=SEC_B, profile_allowed=lambda profile: True)
        items = self.store.record_page(context, projected, next_cursor=("0", "0"), has_more=False, allow_live=True)
        self.assertEqual(items[0]["sender_uid"], "8009")
        self.assertEqual(items[0]["sender_profile_url"], canonical_profile("registered_other"))

    def test_secret_fields_not_saved_in_plan(self):
        self.arm()
        self.add(at(created="102"))
        claim = self.store.claim_ui_candidate("owner")
        for key in ("Cookie_Header", "API-key", "authorization_header", "sensitive_token"):
            plan = self.plan()
            plan["queue_item"][key] = "fixture-secret"
            with self.subTest(key=key), self.assertRaisesRegex(NativeStoreError, "unsafe_ui_plan"):
                self.store.record_ui_plan(claim["source_id"], claim["claim_token"], plan)

    def test_lost_store_is_never_recreated(self):
        self.path.unlink()
        with self.assertRaisesRegex(NativeStoreError, "source_store_missing"):
            NativeSourceStore(self.path, B, PROFILE_B, create=True)
        self.assertFalse(self.path.exists())

    def test_group_change_and_page_repeat_fail(self):
        self.arm()
        with self.assertRaisesRegex(NativeStoreError, "native_group_changed"):
            self.store.prepare_channel(9, "999")
        self.add(at(created="102"), more=True, minimum="1", maximum="2")
        with self.assertRaisesRegex(NativeStoreError, "native_page_repeated"):
            self.add(at(created="102"), more=True, minimum="3", maximum="4")

    def test_expired_unplanned_claim_never_reissued(self):
        self.arm()
        self.add(at(created="102"))
        self.store.claim_ui_candidate("first", lease_seconds=1)
        self.now[0] += 2
        self.assertIsNone(self.store.claim_ui_candidate("second"))
        self.assertEqual(self.store.safe_status()["counts"], {"held": 1})


class ReceiverTests(Fixture):
    def make_receiver(self):
        network = FakeNetwork()
        transport = NativeTransport(lambda: SecretSession("fake=x", "fake", B, 10000), B, network,
                                    clock=lambda: self.now[0])
        return network, NativeReceiver(transport, self.store, receiver_uid=B, receiver_sec_uid=SEC_B,
                                       profile_allowed=lambda profile: profile == PROFILE_A,
                                       allow_live=True, clock=lambda: self.now[0])

    def test_dual_channels_then_only_fresh(self):
        network, receiver = self.make_receiver()
        network.pages["601"], network.pages["2"] = page([at()]), page([reply()])
        self.assertEqual(receiver.poll_once(2), [])
        self.assertTrue(receiver.safe_status()["armed"])
        self.now[0] = 102
        network.pages["601"] = page([at(nid="1003", cid="2003", created="102")])
        network.pages["2"] = page([reply(nid="1004", cid="2004", created="102")])
        self.assertEqual(len(receiver.poll_once(2)), 2)
        self.assertEqual(self.store.safe_status()["counts"], {"fresh": 2, "historical": 2})
        self.assertEqual(receiver.poll_once(2), [])

    def test_post_read_identity_mismatch_prevents_commit(self):
        network, receiver = self.make_receiver()
        counter = [0]
        original = network.__call__
        def switching(url, headers, timeout, max_bytes):
            result = original(url, headers, timeout, max_bytes)
            if urllib.parse.urlsplit(url).path.endswith("notice/"):
                network.uid = "9002"
            return result
        receiver.transport._network = switching
        network.pages["601"] = page([at()])
        with self.assertRaisesRegex(NativeReceiverError, "session_identity_mismatch"):
            receiver.poll_once(1)
        self.assertEqual(self.store.safe_status()["counts"], {})

    def test_backpressure_leaves_both_channel_cursors_unread(self):
        self.arm()
        context = self.store.prepare_channel(9, "601")
        rows = [at(nid=str(4000 + i), cid=str(5000 + i), created="102", text="问题" + str(i)) for i in range(100)]
        self.store.record_page(context, project(page(rows), 9), next_cursor=("0", "0"), has_more=False, allow_live=True)
        network, receiver = self.make_receiver()
        self.assertEqual(receiver.poll_once(2), [])
        self.assertEqual(receiver.safe_status()["code"], "native_backpressure")
        self.assertFalse(any(path.endswith("notice/") for path, _ in network.calls))

    def test_initial_unavailable_and_valid_items_do_not_stall_baseline(self):
        network, receiver = self.make_receiver()
        old = at()
        old["at"]["comment_status"] = 0
        old["at"]["aweme"] = None
        old["at"]["content"] = None
        network.pages["601"] = page([old, at(nid="1009", cid="2009")])
        self.assertEqual(receiver.poll_once(), [])
        self.assertTrue(receiver.safe_status()["armed"])
        self.assertEqual(receiver.safe_status()["unavailable_notices"], 1)
        self.assertEqual(receiver.safe_status()["counts"], {"historical": 1})
        self.now[0] = 102
        network.pages["601"] = page([old, at(nid="1010", cid="2010", created="102")])
        self.assertEqual(len(receiver.poll_once()), 1)


class VaultTests(unittest.TestCase):
    class Protector:
        def protect(self, value, entropy):
            return b"FAKE-SEALED:" + value
        def unprotect(self, value, entropy):
            if not value.startswith(b"FAKE-SEALED:"):
                raise ValueError("fixture secret never propagated")
            return value[len(b"FAKE-SEALED:"):]

    def test_fake_capsule_roundtrip_and_expiry(self):
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory).resolve()
            vault = NativeSessionVault(B, private_root=directory, protector=self.Protector(), clock=lambda: 100)
            vault.save(SecretSession("fake-cookie=x", "fake-ua", B, 1000))
            self.assertEqual(vault.load().expected_uid, B)
            self.assertNotIn("fake", repr(vault))
            expired = NativeSessionVault(B, private_root=directory, protector=self.Protector(), clock=lambda: 2000)
            with self.assertRaisesRegex(NativeSessionError, "authorization_expired"):
                expired.load()

    def test_missing_or_foreign_path_never_bootstrapped(self):
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory).resolve()
            vault = NativeSessionVault(B, private_root=directory, protector=self.Protector())
            with self.assertRaisesRegex(NativeSessionError, "authorization_not_provisioned"):
                vault.load()
            self.assertEqual(list(Path(directory).iterdir()), [])
            with self.assertRaisesRegex(NativeSessionError, "vault_path_not_owned"):
                NativeSessionVault(B, private_root=directory, path=Path(directory).parent / "foreign.dpapi")


if __name__ == "__main__":
    unittest.main()
