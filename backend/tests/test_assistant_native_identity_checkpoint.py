"""Synthetic single-GET identity checkpoint and actual temporary claim gates."""
import json
from unittest import mock

from app.assistant.native_receiver import NativeReceiver, NativeReceiverError
from app.assistant.native_transport import NativeTransport, SecretSession
from tests.test_assistant_native_receiver import (Fixture, FakeNetwork, at, B, SEC_B, PROFILE_A)


class ClaimFixture(Fixture):
    def setUp(self):
        super().setUp()
        self.arm()
        self.candidate = self.add(at(created="102", text="保存到学习库"))[0]
        self.claim = self.store.claim_ui_candidate("identity-check-fixture", lease_seconds=300)
        self.now[0] = 223.0

    def allowed(self, source_id=None, token=None):
        return self.store.candidate_identity_refresh_allowed(
            self.claim["source_id"] if source_id is None else source_id,
            self.claim["claim_token"] if token is None else token)

    def update(self, **fields):
        with self.store._connection(write=True) as db:
            for name, value in fields.items():
                if name not in {"state", "historical", "plan", "task_id", "reference_id", "lease_until", "candidate", "identity_revision"}:
                    raise AssertionError("unexpected fixture field")
                db.execute("UPDATE sources SET " + name + "=? WHERE source_id=?", (value, self.claim["source_id"]))

    def snapshot(self):
        with self.store._connection() as db:
            return {"channels": [tuple(row) for row in db.execute("SELECT * FROM channels ORDER BY channel")],
                    "sources": [tuple(row) for row in db.execute("SELECT * FROM sources ORDER BY source_id")],
                    "meta": dict(db.execute("SELECT key,value FROM meta")),
                    "identities": [tuple(row) for row in db.execute("SELECT * FROM identities ORDER BY uid,sec")]}


class RefreshPredicateTests(ClaimFixture):
    def test_only_stale_identity_allows_real_refresh_without_mutation(self):
        before = self.snapshot()
        self.assertEqual(self.store.check_candidate(self.claim["source_id"], self.claim["claim_token"])["reason"], "native_identity_unverified")
        self.assertTrue(self.allowed())
        self.assertEqual(self.snapshot(), before)

    def test_age_boundary_and_invalid_time_are_not_allowed(self):
        self.now[0] = 220.0
        self.assertFalse(self.allowed())
        self.now[0] = 220.001
        self.assertTrue(self.allowed())
        self.now[0] = float("nan")
        self.assertFalse(self.allowed())

    def test_not_claimed_historical_or_partially_linked_never_refreshes(self):
        for state in ("fresh", "planned", "admitted", "held", "held_conflict", "historical"):
            self.update(state=state)
            with self.subTest(state=state): self.assertFalse(self.allowed())
        self.update(state="claimed", historical=1)
        self.assertFalse(self.allowed())
        self.update(historical=0)
        for name, value in (("plan", "{}"), ("task_id", "fixture-task"), ("reference_id", "fixture-ref")):
            self.update(**{name: value})
            with self.subTest(link=name): self.assertFalse(self.allowed())
            self.update(**{name: None})
        self.assertTrue(self.allowed())

    def test_wrong_source_or_claim_token_never_refreshes(self):
        self.assertFalse(self.allowed(source_id="native:" + "0" * 64))
        self.assertFalse(self.allowed(token="f" * 32))
        self.assertFalse(self.store.candidate_identity_refresh_allowed(self.claim["source_id"], None))
        self.assertFalse(self.store.candidate_identity_refresh_allowed("not-a-source", self.claim["claim_token"]))

    def test_expired_missing_or_invalid_lease_never_refreshes(self):
        for lease in (None, 0, self.now[0] - 1, self.now[0], float("inf"), "invalid-fixture"):
            self.update(lease_until=lease)
            with self.subTest(lease_kind=type(lease).__name__): self.assertFalse(self.allowed())
        self.update(lease_until=self.now[0] + 1)
        self.assertTrue(self.allowed())

    def test_pause_invalid_future_zero_or_missing_identity_time_never_refreshes(self):
        with self.store._connection(write=True) as db:
            db.execute("UPDATE meta SET value='network_timeout' WHERE key='pause_code'")
        self.assertFalse(self.allowed())
        with self.store._connection(write=True) as db:
            db.execute("UPDATE meta SET value='' WHERE key='pause_code'")
        for stamp in ("0", "-1", "nan", "inf", "invalid-fixture", "300"):
            with self.store._connection(write=True) as db:
                db.execute("UPDATE meta SET value=? WHERE key='verified_at'", (stamp,))
            with self.subTest(stamp_kind=stamp): self.assertFalse(self.allowed())
        with self.store._connection(write=True) as db:
            db.execute("UPDATE meta SET value='100' WHERE key='verified_at'")
            db.execute("DELETE FROM meta WHERE key='pause_code'")
        self.assertFalse(self.allowed())

    def test_identity_revision_or_pin_conflict_never_refreshes(self):
        with self.store._connection(write=True) as db:
            db.execute("UPDATE identities SET revision=revision+1")
        self.assertFalse(self.allowed())
        with self.store._connection(write=True) as db:
            db.execute("UPDATE identities SET revision=revision-1,state='conflict'")
        self.assertFalse(self.allowed())

    def test_payload_and_semantic_conflict_never_refreshes(self):
        self.add(at(nid="1008", created="223", text="改过的正文"))
        self.assertFalse(self.allowed())

    def test_semantic_other_cid_never_refreshes(self):
        self.add(at(nid="1009", cid="2009", created="223", text="保存 到学习库"))
        self.assertFalse(self.allowed())

    def test_corrupt_immutable_payload_is_not_only_a_time_expiry(self):
        with self.store._connection() as db:
            stored = json.loads(db.execute("SELECT candidate FROM sources WHERE source_id=?", (self.claim["source_id"],)).fetchone()[0])
        stored["content"] = "fixture mutation without matching immutable hash"
        self.update(candidate=json.dumps(stored))
        self.assertFalse(self.allowed())


class IdentityCheckpointTests(ClaimFixture):
    def receiver(self, network):
        transport = NativeTransport(lambda: SecretSession("fake-cookie=x", "fake-UA", B, 10000), B,
                                    network=network, clock=lambda: self.now[0])
        return NativeReceiver(transport, self.store, receiver_uid=B, receiver_sec_uid=SEC_B,
                              profile_allowed=lambda profile: profile == PROFILE_A,
                              allow_live=True, clock=lambda: self.now[0])

    def test_checkpoint_is_one_query_user_get_no_groups_pages_or_cursor_mutation(self):
        network = FakeNetwork()
        receiver = self.receiver(network)
        receiver._next_channel = 7
        before = self.snapshot()
        self.assertTrue(self.allowed())
        with mock.patch.object(self.store, "set_verified", wraps=self.store.set_verified) as update:
            self.assertIs(receiver.reverify_identity_checkpoint(), True)
        self.assertEqual(update.call_count, 1)
        self.assertEqual([path for path, _ in network.calls], ["/aweme/v1/web/query/user/"])
        after = self.snapshot()
        self.assertEqual(after["channels"], before["channels"])
        self.assertEqual(after["sources"], before["sources"])
        self.assertEqual(after["identities"], before["identities"])
        self.assertEqual(receiver._next_channel, 7)
        self.assertEqual(after["meta"]["verified_at"], str(self.now[0]))
        self.assertTrue(self.store.check_candidate(self.claim["source_id"], self.claim["claim_token"])["valid"])
        self.assertFalse(self.allowed())  # It is no longer an age-only failure.

    def test_wrong_uid_pauses_and_never_marks_verified(self):
        network = FakeNetwork(); network.uid = "999999"
        receiver = self.receiver(network)
        before = self.snapshot()
        with mock.patch.object(self.store, "set_verified", wraps=self.store.set_verified) as update:
            with self.assertRaisesRegex(NativeReceiverError, "session_identity_mismatch"):
                receiver.reverify_identity_checkpoint()
        self.assertEqual(update.call_count, 0)
        self.assertEqual([path for path, _ in network.calls], ["/aweme/v1/web/query/user/"])
        after = self.snapshot()
        self.assertEqual(after["channels"], before["channels"])
        self.assertEqual(after["sources"], before["sources"])
        self.assertEqual(after["meta"]["verified_at"], "0")
        self.assertEqual(after["meta"]["pause_code"], "session_identity_mismatch")
        self.assertFalse(self.allowed())

    def test_network_timeout_drops_exception_detail_and_does_not_mark_verified(self):
        def network(*args): raise TimeoutError("invented confidential fixture detail")
        receiver = self.receiver(network)
        with mock.patch.object(self.store, "set_verified", wraps=self.store.set_verified) as update:
            with self.assertRaises(NativeReceiverError) as error: receiver.reverify_identity_checkpoint()
        self.assertEqual(str(error.exception), "network_timeout")
        self.assertEqual(update.call_count, 0)
        self.assertEqual(self.store.safe_status()["pause_code"], "network_timeout")

    def test_verification_reply_pauses_without_set_verified(self):
        network = FakeNetwork(); network.override = (200, b'{"status_code":0,"verify_required":true}')
        receiver = self.receiver(network)
        with mock.patch.object(self.store, "set_verified", wraps=self.store.set_verified) as update:
            with self.assertRaisesRegex(NativeReceiverError, "verification_required"):
                receiver.reverify_identity_checkpoint()
        self.assertEqual(update.call_count, 0)
        self.assertEqual(self.store.safe_status()["pause_code"], "verification_required")

    def test_pause_write_failure_does_not_replace_fixed_identity_error(self):
        network = FakeNetwork(); network.uid = "999999"
        receiver = self.receiver(network)
        with mock.patch.object(self.store, "pause", side_effect=RuntimeError("invented detail must not propagate")):
            with mock.patch.object(self.store, "set_verified", wraps=self.store.set_verified) as update:
                with self.assertRaisesRegex(NativeReceiverError, "session_identity_mismatch"):
                    receiver.reverify_identity_checkpoint()
        self.assertEqual(update.call_count, 0)


if __name__ == "__main__":
    import unittest
    unittest.main()
