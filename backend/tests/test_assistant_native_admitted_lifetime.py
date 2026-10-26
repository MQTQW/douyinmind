"""Actual temporary NativeSourceStore tests; never account/network/production data."""
import json
import sqlite3

from app.assistant.native_source_store import NativeSourceStore, NativeStoreError
from tests.test_assistant_native_receiver import Fixture, at, B, PROFILE_B


class AdmittedLifetimeTests(Fixture):
    def make_claim(self):
        self.arm()
        candidate = self.add(at(created="102", text="请保存这个视频"))[0]
        claim = self.store.claim_ui_candidate("fixture-owner", lease_seconds=300)
        return candidate, claim

    def make_admitted(self):
        candidate, claim = self.make_claim()
        envelope = {"reference_id": "fixture-ui-reference", "source_mode": "live_ui"}
        plan = {"task_id": None, "reference_id": envelope["reference_id"], "envelope": envelope,
                "thread": {"quality": "ui_verified", "root_reference_id": "fixture-root"},
                "queue_item": {"binding": {"id": "fixture-binding"}, "envelope": envelope,
                               "authorization": {"user_revision": 1}},
                "native_correlation": {"source_native_comment_id": candidate["native_comment_id"],
                                       "observed_native_comment_id": None, "quality": "semantic_only"}}
        self.store.record_ui_plan(claim["source_id"], claim["claim_token"], plan)
        self.store.finish_ui_admission(claim["source_id"], claim["claim_token"], "fixture-task", envelope["reference_id"])
        return candidate, claim

    def test_claimed_still_needs_120_second_fresh_intake_identity(self):
        candidate, claim = self.make_claim()
        self.assertTrue(self.store.check_candidate(claim["source_id"], claim["claim_token"])["valid"])
        self.now[0] += 121
        checked = self.store.check_candidate(claim["source_id"], claim["claim_token"])
        self.assertFalse(checked["valid"])
        self.assertEqual(checked["reason"], "native_identity_unverified")
        self.assertFalse(self.store.admitted_source_current(candidate["source_id"]))

    def test_admitted_intent_survives_transport_age_without_freshness_fabrication(self):
        candidate, _ = self.make_admitted()
        with self.store._connection() as db:
            original_verified = db.execute("SELECT value FROM meta WHERE key='verified_at'").fetchone()[0]
        self.now[0] += 121
        self.assertFalse(self.store.admission_current(candidate["source_id"]))
        self.assertTrue(self.store.admitted_source_current(candidate["source_id"]))
        reopened = NativeSourceStore(self.path, B, PROFILE_B, clock=lambda: self.now[0])
        self.assertTrue(reopened.admitted_source_current(candidate["source_id"]))
        with self.store._connection() as db:
            self.assertEqual(db.execute("SELECT value FROM meta WHERE key='verified_at'").fetchone()[0], original_verified)

    def test_admitted_intent_survives_ordinary_poll_pause(self):
        candidate, _ = self.make_admitted()
        self.store.pause("network_timeout")
        self.assertFalse(self.store.admission_current(candidate["source_id"]))
        self.assertTrue(self.store.admitted_source_current(candidate["source_id"]))
        with self.store._connection() as db:
            meta = dict(db.execute("SELECT key,value FROM meta WHERE key IN ('verified_at','pause_code')"))
        self.assertEqual(meta, {"verified_at": "0", "pause_code": "network_timeout"})

    def test_payload_conflict_revokes_admitted_source(self):
        candidate, _ = self.make_admitted()
        self.add(at(nid="1008", created="103", text="另一个正文"))
        self.assertEqual(self.store.get_source(candidate["source_id"])["state"], "held_conflict")
        self.assertFalse(self.store.admitted_source_current(candidate["source_id"]))

    def test_identity_conflict_revokes_admitted_source(self):
        candidate, _ = self.make_admitted()
        self.add(at(nid="1009", cid="2009", created="103", uid="8009", text="另一个问题"))
        self.assertFalse(self.store.admitted_source_current(candidate["source_id"]))

    def test_semantic_multi_cid_conflict_revokes_admitted_source(self):
        candidate, _ = self.make_admitted()
        self.add(at(nid="1009", cid="2009", created="103", text="请 保存 这个视频"))
        self.assertFalse(self.store.admitted_source_current(candidate["source_id"]))

    def test_identity_revision_change_revokes_admitted_source(self):
        candidate, _ = self.make_admitted()
        with self.store._connection(write=True) as db:
            db.execute("UPDATE identities SET revision=revision+1")
        self.assertFalse(self.store.admitted_source_current(candidate["source_id"]))

    def test_planned_unlinked_and_historical_never_gain_persistent_authority(self):
        candidate, claim = self.make_claim()
        self.assertFalse(self.store.admitted_source_current(candidate["source_id"]))
        self.store.hold(claim["source_id"], claim["claim_token"], "native_ui_unverified")
        self.assertFalse(self.store.admitted_source_current(candidate["source_id"]))
        self.assertFalse(self.store.admitted_source_current("native:nonexistent"))

    def test_lost_store_fails_closed_without_recreation(self):
        candidate, _ = self.make_admitted()
        self.path.unlink()
        with self.assertRaisesRegex(NativeStoreError, "source_store_missing"):
            self.store.admitted_source_current(candidate["source_id"])
        self.assertFalse(self.path.exists())

    def test_cached_foreign_replacement_fails_before_admitted_authority(self):
        candidate, _ = self.make_admitted()
        replacement = self.path.with_name("foreign-fixture.sqlite3")
        db = sqlite3.connect(replacement)
        try:
            db.execute("CREATE TABLE unrelated_fixture(value TEXT)")
            db.execute("INSERT INTO unrelated_fixture VALUES('unrelated fixture')")
            db.commit()
        finally:
            db.close()
        original = replacement.read_bytes()
        self.path.unlink()
        replacement.rename(self.path)
        with self.assertRaisesRegex(NativeStoreError, "source_store_owner_unverified"):
            self.store.admitted_source_current(candidate["source_id"])
        self.assertEqual(self.path.read_bytes(), original)


if __name__ == "__main__":
    import unittest
    unittest.main()
