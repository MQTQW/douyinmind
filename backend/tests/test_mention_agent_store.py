"""Durable agent-state boundaries, only temporary SQLite and fictional users."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from app.mention_agent.store import AgentStore, AgentStoreError


SENDER = "https://www.douyin.com/user/sender-fixture"
RECEIVER = "https://www.douyin.com/user/receiver-fixture"


def utc(value=None):
    return datetime.fromtimestamp(time.time() if value is None else value, timezone.utc).isoformat()


def binding(**changes):
    return {"binding_id": "fixture-binding", "instance_id": "fixture-desktop",
            "sender_profile_url": SENDER, "receiver_profile_url": RECEIVER,
            "knowledge_base_id": 7, "public_reply_allowed": True, "active": True, **changes}


def envelope(**changes):
    return {"command_id": "fixture-command", "reference_id": "fixture-reference",
            "reference_quality": "verified_ui", "native_comment_id": None,
            "sender_profile_url": SENDER, "receiver_profile_url": RECEIVER,
            "video_id": "7999999000000000011", "comment_text": "@虚构助手 根据测试库回答问题",
            "source_mode": "live_ui", "evidence": {"real_mention": True, "unique_count": 1,
                "sender_verified": True, "video_verified": True, "observed_at_utc": utc()}, **changes}


def proposal(**changes):
    return {"text": "仅供隔离验证的回答", "source_ids": ["fixture-source"], "kind": "answer",
            "sources": [{"id": "fixture-source", "title": "虚构来源"}], **changes}


def verification(**changes):
    value = envelope()
    return {key: value[key] for key in ("sender_profile_url", "receiver_profile_url", "video_id", "comment_text", "reference_id")} | {
        "unique_count": 1, "real_mention": True, "current_receiver_verified": True,
        "observed_at_utc": utc(), **changes}


def ingestion_observation(job, **changes):
    return {"backend_request_id": job["job_id"], "binding_id": job["binding_id"],
            "instance_id": job["instance_id"], "knowledge_base_id": job["knowledge_base_id"],
            "video_id": job["video_id"], "backend_state": "succeeded", "membership_exists": True,
            "cache_status": "done", "indexed_content_chunks": 2, **changes}


def import_proposal(job, **changes):
    return {"text": "隔离夹具程序生成的入库状态", "source_ids": [], "sources": [], "kind": "import_status",
            "import_job_id": job["job_id"], "import_state": job["state"], **changes}


class AgentStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.path = self.root / "agent.sqlite3"
        self.store = AgentStore(self.path)
        self.store.put_binding(binding())

    def queued(self, **changes):
        return self.store.enqueue("fixture-binding", envelope(**changes))

    def processing(self, **changes):
        task = self.queued(**changes)
        return self.store.claim(task["task_id"])

    def ready(self, **changes):
        claim = self.processing(**changes)
        return self.store.complete(claim["task_id"], claim["claim_token"], proposal())

    def legacy_duplicate(self, task_id, *, command_id="legacy-command", comment_text=None, with_reply=False):
        """Represent rows written by the pre-reference-dedup store, unchanged."""
        with closing(sqlite3.connect(self.path)) as db:
            db.row_factory = sqlite3.Row
            row = dict(db.execute("SELECT * FROM agent_tasks WHERE task_id=?", (task_id,)).fetchone())
            original_reply = db.execute("SELECT * FROM agent_replies WHERE task_id=?", (task_id,)).fetchone()
            value = json.loads(row["envelope"])
            value["command_id"] = command_id
            if comment_text is not None:
                value["comment_text"] = comment_text
            row["task_id"] = "task-" + command_id
            row["command_id"] = command_id
            row["envelope"] = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            immutable = {key: item for key, item in value.items() if key != "evidence"}
            row["immutable_hash"] = hashlib.sha256(json.dumps(immutable, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
            row["created_at"] += 1
            row["updated_at"] += 1
            columns = ",".join(row)
            db.execute("INSERT INTO agent_tasks(" + columns + ") VALUES(" + ",".join("?" for _ in row) + ")", tuple(row.values()))
            if with_reply:
                copied_reply = dict(original_reply)
                copied_reply["task_id"] = row["task_id"]
                copied_reply["reply_id"] = "reply-" + command_id
                copied_reply["created_at"] += 1
                db.execute("INSERT INTO agent_replies(" + ",".join(copied_reply) + ") VALUES(" + ",".join("?" for _ in copied_reply) + ")", tuple(copied_reply.values()))
            db.commit()
            return row["task_id"]

    def raw_tasks(self):
        with closing(sqlite3.connect(self.path)) as db:
            db.row_factory = sqlite3.Row
            return [dict(row) for row in db.execute("SELECT * FROM agent_tasks ORDER BY created_at,task_id")]

    def test_binding_is_durable_and_conflicting_reconfiguration_is_rejected(self):
        restarted = AgentStore(self.path)
        self.assertEqual(restarted.get_binding("fixture-binding"), binding())
        self.assertEqual(restarted.put_binding(binding()), binding())
        with self.assertRaisesRegex(AgentStoreError, "binding_conflict"):
            restarted.put_binding(binding(knowledge_base_id=8))
        self.assertEqual(self.store.get_binding("fixture-binding")["knowledge_base_id"], 7)

    def test_sender_and_receiver_must_match_single_binding(self):
        for key in ("sender_profile_url", "receiver_profile_url"):
            with self.subTest(key=key), self.assertRaisesRegex(AgentStoreError, "envelope_binding_mismatch"):
                self.queued(**{key: "https://www.douyin.com/user/other-fixture"})
        self.assertEqual(self.store.list_tasks(), [])

    def test_replay_ignores_observation_time_but_not_original_target_or_source_mode(self):
        original = envelope()
        first = self.store.enqueue("fixture-binding", original)
        later = deepcopy(original)
        later["evidence"]["observed_at_utc"] = utc(time.time() + 1)
        self.assertEqual(self.store.enqueue("fixture-binding", later)["task_id"], first["task_id"])
        for field, value in (("comment_text", "不同正文"), ("reference_id", "other-reference"),
                             ("video_id", "7999999000000000022"), ("source_mode", "isolated")):
            changed = deepcopy(original)
            changed[field] = value
            with self.subTest(field=field), self.assertRaisesRegex(AgentStoreError, "command_conflict"):
                self.store.enqueue("fixture-binding", changed)
        self.assertEqual(len(AgentStore(self.path).list_tasks()), 1)

    def test_same_command_cannot_move_to_another_binding(self):
        self.queued()
        self.store.put_binding(binding(binding_id="other-binding", knowledge_base_id=8))
        with self.assertRaisesRegex(AgentStoreError, "command_conflict"):
            self.store.enqueue("other-binding", envelope())

    def test_only_one_concurrent_consumer_claims_the_task(self):
        task = self.queued()
        other = AgentStore(self.path)
        gate = threading.Barrier(2)
        def claim(store):
            gate.wait(timeout=3)
            return store.claim(task["task_id"])
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(claim, (self.store, other)))
        self.assertEqual(sum(value is not None for value in results), 1)
        self.assertEqual(self.store.get_task(task["task_id"])["attempts"], 1)

    def test_expired_model_claim_is_reviewed_instead_of_reexecuted(self):
        now = time.time()
        with patch("app.mention_agent.store.time.time", return_value=now):
            task = self.queued()
            claim = self.store.claim(task["task_id"], lease_seconds=5)
        with patch("app.mention_agent.store.time.time", return_value=now + 6):
            restarted = AgentStore(self.path)
            self.assertIsNone(restarted.claim(task["task_id"]))
            with self.assertRaises(AgentStoreError):
                restarted.complete(task["task_id"], claim["claim_token"], proposal())
        final = self.store.get_task(task["task_id"])
        self.assertEqual(final["state"], "needs_review")
        self.assertEqual(final["reason_code"], "processing_lease_expired")
        self.assertEqual(final["attempts"], 1)
        self.assertIsNone(self.store.get_reply(task["task_id"]))

    def test_late_complete_directly_after_expiry_also_persists_review(self):
        now = time.time()
        with patch("app.mention_agent.store.time.time", return_value=now):
            claim = self.processing()
        with patch("app.mention_agent.store.time.time", return_value=now + 181):
            with self.assertRaisesRegex(AgentStoreError, "claim_expired"):
                self.store.complete(claim["task_id"], claim["claim_token"], proposal())
        self.assertEqual(self.store.get_task(claim["task_id"])["state"], "needs_review")

    def test_steps_are_bounded_and_never_persist_credentials_or_reasoning(self):
        claim = self.processing()
        for secret in ("api_key", "cookie", "password", "authorization", "reasoning_content", "raw_messages"):
            with self.subTest(secret=secret), self.assertRaisesRegex(AgentStoreError, "forbidden_metadata_field"):
                self.store.record_step(claim["task_id"], claim["claim_token"], "tool_result", {"nested": {secret: "fiction-only"}})
        with self.assertRaisesRegex(AgentStoreError, "metadata_too_large"):
            self.store.record_step(claim["task_id"], claim["claim_token"], "tool_result", {"summary": "x" * 5000})
        self.store.record_step(claim["task_id"], claim["claim_token"], "tool_call", {"name": "query_library", "count": 1})
        self.assertEqual(len(self.store.list_steps(claim["task_id"])), 1)

    def test_wrong_claim_token_does_not_change_steps_or_result(self):
        claim = self.processing()
        with self.assertRaisesRegex(AgentStoreError, "stale_claim_token"):
            self.store.record_step(claim["task_id"], "wrong-token", "decision", {})
        with self.assertRaisesRegex(AgentStoreError, "stale_claim_token"):
            self.store.complete(claim["task_id"], "wrong-token", proposal())
        self.assertEqual(self.store.list_steps(claim["task_id"]), [])
        self.assertIsNone(self.store.get_reply(claim["task_id"]))

    def test_proposal_cannot_override_target_and_reply_is_idempotent(self):
        claim = self.processing()
        forged = proposal() | {"receiver_profile_url": "https://www.douyin.com/user/other-fixture"}
        with self.assertRaisesRegex(AgentStoreError, "invalid_proposal_fields"):
            self.store.complete(claim["task_id"], claim["claim_token"], forged)
        reply = self.store.complete(claim["task_id"], claim["claim_token"], proposal())
        self.assertEqual(reply["target"]["receiver_profile_url"], RECEIVER)
        self.assertEqual(reply["target"]["knowledge_base_id"], 7)
        again = self.store.complete(claim["task_id"], claim["claim_token"], proposal())
        self.assertEqual(again["reply_id"], reply["reply_id"])
        with self.assertRaisesRegex(AgentStoreError, "reply_conflict"):
            self.store.complete(claim["task_id"], claim["claim_token"], proposal(text="不同结果"))

    def test_answer_ready_and_reply_intent_commit_atomically(self):
        claim = self.processing()
        with closing(sqlite3.connect(self.path)) as db:
            db.execute("""CREATE TRIGGER fixture_abort_answer BEFORE UPDATE OF state ON agent_tasks
              WHEN NEW.state='answer_ready' BEGIN SELECT RAISE(ABORT,'fixture_abort'); END""")
            db.commit()
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.complete(claim["task_id"], claim["claim_token"], proposal())
        self.assertEqual(self.store.get_task(claim["task_id"])["state"], "processing")
        self.assertIsNone(self.store.get_reply(claim["task_id"]))
        with closing(sqlite3.connect(self.path)) as db:
            db.execute("DROP TRIGGER fixture_abort_answer")
            db.commit()
        self.assertEqual(self.store.complete(claim["task_id"], claim["claim_token"], proposal())["state"], "ready")

    def test_review_terminal_does_not_resurrect_from_late_callback(self):
        claim = self.processing()
        self.store.needs_review(claim["task_id"], claim["claim_token"], "ambiguous_target")
        self.assertIsNone(self.store.claim(claim["task_id"]))
        with self.assertRaises(AgentStoreError):
            self.store.complete(claim["task_id"], claim["claim_token"], proposal())
        self.assertIsNone(self.store.get_reply(claim["task_id"]))

    def test_replay_and_isolated_inputs_can_answer_but_never_reserve_send(self):
        for index, mode in enumerate(("replay", "isolated")):
            ref = "mode-reference-" + str(index)
            reply = self.ready(command_id="mode-" + str(index), source_mode=mode, reference_id=ref)
            self.assertEqual(reply["state"], "ready")
            self.assertIsNone(self.store.reserve_send(reply["task_id"], verification(reference_id=ref)))
            self.assertEqual(self.store.get_reply(reply["task_id"])["state"], "ready")

    def test_reserve_requires_fresh_same_original_comment_and_receiver(self):
        reply = self.ready()
        invalid = [verification(observed_at_utc=utc(time.time() - 121)),
                   verification(observed_at_utc=utc(time.time() - 30)),
                   verification(reference_id="wrong-reference"), verification(unique_count=2),
                   verification(current_receiver_verified=False), verification(real_mention=False),
                   verification(comment_text="同视频另一条评论"), verification(native_comment_id="wrong-native")]
        missing_time = verification()
        missing_time.pop("observed_at_utc")
        invalid.append(missing_time)
        for value in invalid:
            self.assertIsNone(self.store.reserve_send(reply["task_id"], value))
        reserved = self.store.reserve_send(reply["task_id"], verification())
        self.assertEqual(reserved["state"], "reserved")
        self.assertTrue(reserved["send_token"])
        self.assertIsNone(self.store.reserve_send(reply["task_id"], verification()))

    def test_only_one_concurrent_send_reservation(self):
        reply = self.ready()
        other = AgentStore(self.path)
        gate = threading.Barrier(2)
        proof = verification()
        def reserve(store):
            gate.wait(timeout=3)
            return store.reserve_send(reply["task_id"], proof)
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(reserve, (self.store, other)))
        self.assertEqual(sum(item is not None for item in results), 1)

    def test_binding_revocation_blocks_work_and_ready_publication(self):
        processing = self.processing()
        self.store.deactivate("fixture-binding")
        self.assertEqual(self.store.get_task(processing["task_id"])["state"], "needs_review")
        with self.assertRaises(AgentStoreError):
            self.store.complete(processing["task_id"], processing["claim_token"], proposal())
        with self.assertRaisesRegex(AgentStoreError, "binding_conflict"):
            self.store.put_binding(binding())

    def test_deactivation_after_answer_and_public_consent_false_block_reserve(self):
        reply = self.ready()
        self.store.deactivate("fixture-binding")
        self.assertIsNone(self.store.reserve_send(reply["task_id"], verification()))
        self.store.put_binding(binding(binding_id="private", public_reply_allowed=False))
        task = self.store.enqueue("private", envelope(command_id="private-command"))
        claim = self.store.claim(task["task_id"])
        self.store.complete(task["task_id"], claim["claim_token"], proposal())
        self.assertIsNone(self.store.reserve_send(task["task_id"], verification()))

    def test_all_send_outcomes_are_durable_terminal_and_never_automatically_retried(self):
        for index, outcome in enumerate(("b_view_visible", "delivery_unknown", "verification_required", "failed_no_submit")):
            ref = "send-reference-" + str(index)
            reply = self.ready(command_id="send-" + str(index), reference_id=ref)
            reservation = self.store.reserve_send(reply["task_id"], verification(reference_id=ref))
            evidence = {"fixture": True, "status": outcome}
            result = self.store.finish_send(reply["task_id"], reservation["send_token"], outcome, evidence)
            self.assertEqual(result["state"], outcome)
            self.assertEqual(AgentStore(self.path).get_task(reply["task_id"])["state"], outcome)
            self.assertEqual(self.store.finish_send(reply["task_id"], reservation["send_token"], outcome, evidence)["state"], outcome)
            self.assertIsNone(self.store.reserve_send(reply["task_id"], verification(reference_id=ref)))
            with self.assertRaisesRegex(AgentStoreError, "send_result_conflict"):
                self.store.finish_send(reply["task_id"], reservation["send_token"], "b_view_visible" if outcome != "b_view_visible" else "delivery_unknown", evidence)

    def test_wrong_send_token_cannot_finish_a_reserved_intent(self):
        reply = self.ready()
        self.store.reserve_send(reply["task_id"], verification())
        with self.assertRaisesRegex(AgentStoreError, "stale_send_token"):
            self.store.finish_send(reply["task_id"], "wrong-token", "b_view_visible", {})
        self.assertEqual(self.store.get_reply(reply["task_id"])["state"], "reserved")

    def test_foreign_and_protected_database_paths_are_not_modified(self):
        foreign = self.root / "business-fixture.sqlite3"
        with closing(sqlite3.connect(foreign)) as db:
            db.execute("CREATE TABLE original_data(id INTEGER)")
            db.execute("INSERT INTO original_data VALUES(1)")
            db.commit()
        before = hashlib.sha256(foreign.read_bytes()).hexdigest()
        with self.assertRaisesRegex(AgentStoreError, "foreign_database"):
            AgentStore(foreign)
        with self.assertRaisesRegex(AgentStoreError, "protected_store_path"):
            AgentStore(foreign, protected_paths=(foreign,))
        self.assertEqual(hashlib.sha256(foreign.read_bytes()).hexdigest(), before)
        untouched = self.root / "protected-directory" / "store.sqlite3"
        with self.assertRaisesRegex(AgentStoreError, "protected_store_path"):
            AgentStore(untouched, protected_paths=(untouched.parent,))
        self.assertFalse(untouched.exists())

    def test_hardlink_to_foreign_database_is_rejected(self):
        original = self.root / "original.sqlite3"
        original.write_bytes(b"fixture original must remain unchanged")
        alias = self.root / "hardlink.sqlite3"
        os.link(original, alias)
        with self.assertRaisesRegex(AgentStoreError, "unsafe_store_file"):
            AgentStore(alias)
        self.assertEqual(original.read_bytes(), b"fixture original must remain unchanged")

    def test_symlink_store_is_rejected_if_platform_supports_it(self):
        alias = self.root / "alias.sqlite3"
        try:
            alias.symlink_to(self.path)
        except OSError:
            self.skipTest("fixture symlinks unavailable")
        with self.assertRaises(AgentStoreError):
            AgentStore(alias)

    def test_redirected_journal_sidecar_is_rejected_before_write(self):
        original = self.root / "journal-target"
        original.write_bytes(b"fixture original journal data")
        os.link(original, Path(str(self.path) + "-journal"))
        with self.assertRaisesRegex(AgentStoreError, "unsafe_store_file"):
            self.queued()
        self.assertEqual(original.read_bytes(), b"fixture original journal data")

    def test_incomplete_or_ambiguous_mention_proof_is_rejected(self):
        for field, value in (("real_mention", False), ("unique_count", 2), ("unique_count", True),
                             ("sender_verified", False), ("video_verified", False)):
            proof = envelope()["evidence"] | {field: value}
            with self.subTest(field=field, value=value), self.assertRaisesRegex(AgentStoreError, "incomplete_mention_evidence"):
                self.queued(evidence=proof)
        with self.assertRaisesRegex(AgentStoreError, "native_comment_id_required"):
            self.queued(reference_quality="native", native_comment_id=None)

    def test_reasoning_in_sources_is_not_persisted_in_reply_or_task(self):
        claim = self.processing()
        invalid = proposal(sources=[{"id": "fixture-source", "reasoning_content": "fictional hidden reasoning"}])
        with self.assertRaisesRegex(AgentStoreError, "forbidden_metadata_field"):
            self.store.complete(claim["task_id"], claim["claim_token"], invalid)
        self.assertIsNone(self.store.get_reply(claim["task_id"]))
        self.assertEqual(self.store.get_task(claim["task_id"])["state"], "processing")

    def test_malformed_enum_values_are_rejected_as_store_errors(self):
        for value in (envelope(reference_quality=[]), envelope(source_mode={})):
            with self.assertRaises(AgentStoreError):
                self.store.enqueue("fixture-binding", value)
        claim = self.processing()
        with self.assertRaises(AgentStoreError):
            self.store.complete(claim["task_id"], claim["claim_token"], proposal(kind=[]))
        with self.assertRaises(AgentStoreError):
            self.store.record_step(claim["task_id"], claim["claim_token"], [], {})

    def test_explicit_unsent_revision_preserves_old_reply_and_original_task_identity(self):
        original_envelope = envelope()
        queued = self.store.enqueue("fixture-binding", original_envelope)
        old_claim = self.store.claim(queued["task_id"])
        old_reply = self.store.complete(old_claim["task_id"], old_claim["claim_token"], proposal())
        revised = self.store.requeue_unsent(old_reply["task_id"], "citation_mismatch")
        self.assertEqual(revised["task_id"], queued["task_id"])
        self.assertEqual(revised["state"], "queued")
        self.assertIsNone(revised["claim_token"])
        self.assertIsNone(revised["lease_until"])
        self.assertIsNone(self.store.get_reply(queued["task_id"]))
        history = AgentStore(self.path).list_reply_revisions(queued["task_id"])
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["reply"], old_reply)
        self.assertEqual(history[0]["proposal"], proposal())
        self.assertEqual(history[0]["target"], old_reply["target"])
        self.assertEqual(history[0]["reason_code"], "citation_mismatch")
        replay = self.store.enqueue("fixture-binding", original_envelope)
        self.assertEqual(replay["task_id"], queued["task_id"])
        self.assertEqual(len(self.store.list_tasks()), 1)
        with self.assertRaises(AgentStoreError):
            self.store.complete(queued["task_id"], old_claim["claim_token"], proposal(text="late old result"))
        new_claim = self.store.claim(queued["task_id"])
        self.assertNotEqual(new_claim["claim_token"], old_claim["claim_token"])
        self.assertIsNone(self.store.claim(queued["task_id"]))
        with self.assertRaisesRegex(AgentStoreError, "stale_claim_token"):
            self.store.complete(queued["task_id"], old_claim["claim_token"], proposal(text="late old result"))
        replacement = self.store.complete(queued["task_id"], new_claim["claim_token"], proposal(text="修订后的隔离回答"))
        self.assertNotEqual(replacement["reply_id"], old_reply["reply_id"])
        self.assertEqual(replacement["target"], old_reply["target"])
        with self.assertRaisesRegex(AgentStoreError, "revision_limit_reached"):
            self.store.requeue_unsent(queued["task_id"], "citation_mismatch")
        self.assertEqual(self.store.get_reply(queued["task_id"]), replacement)
        self.assertEqual(len(self.store.list_reply_revisions(queued["task_id"])), 1)

    def test_reserved_and_all_send_terminal_results_cannot_be_requeued(self):
        for index, outcome in enumerate((None, "b_view_visible", "delivery_unknown", "verification_required", "failed_no_submit")):
            ref = "no-requeue-reference-" + str(index)
            reply = self.ready(command_id="no-requeue-" + str(index), reference_id=ref)
            reservation = self.store.reserve_send(reply["task_id"], verification(reference_id=ref))
            if outcome:
                self.store.finish_send(reply["task_id"], reservation["send_token"], outcome, {"fixture": True})
            before = self.store.get_reply(reply["task_id"])
            with self.subTest(outcome=outcome), self.assertRaisesRegex(AgentStoreError, "reply_not_unsent_ready"):
                self.store.requeue_unsent(reply["task_id"], "citation_mismatch")
            self.assertEqual(self.store.get_reply(reply["task_id"]), before)
            self.assertEqual(self.store.list_reply_revisions(reply["task_id"]), [])

    def test_requeue_requires_active_binding_and_fixed_reason_code(self):
        reply = self.ready()
        for reason in ("这里包含完整原文，不应作为错误码", "has spaces", "Cookie: fake"):
            with self.subTest(reason=reason), self.assertRaisesRegex(AgentStoreError, "invalid_reason_code"):
                self.store.requeue_unsent(reply["task_id"], reason)
        self.store.deactivate("fixture-binding")
        with self.assertRaisesRegex(AgentStoreError, "binding_inactive"):
            self.store.requeue_unsent(reply["task_id"], "citation_mismatch")
        self.assertEqual(self.store.list_reply_revisions(reply["task_id"]), [])

    def test_listing_revisions_is_readonly_for_pre_revision_ledgers(self):
        before = hashlib.sha256(self.path.read_bytes()).hexdigest()
        self.assertEqual(self.store.list_reply_revisions("missing-task"), [])
        self.assertEqual(hashlib.sha256(self.path.read_bytes()).hexdigest(), before)
        with closing(sqlite3.connect(self.path)) as db:
            self.assertIsNone(db.execute("SELECT name FROM sqlite_master WHERE name='agent_reply_revisions'").fetchone())

    def test_requeue_archive_delete_and_task_transition_are_atomic(self):
        reply = self.ready()
        with closing(sqlite3.connect(self.path)) as db:
            db.execute("""CREATE TRIGGER fixture_abort_requeue BEFORE UPDATE OF state ON agent_tasks
              WHEN NEW.state='queued' BEGIN SELECT RAISE(ABORT,'fixture_abort'); END""")
            db.commit()
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.requeue_unsent(reply["task_id"], "citation_mismatch")
        self.assertEqual(self.store.get_reply(reply["task_id"]), reply)
        self.assertEqual(self.store.get_task(reply["task_id"])["state"], "answer_ready")
        self.assertEqual(self.store.list_reply_revisions(reply["task_id"]), [])

    def test_whitespace_only_new_command_reuses_reference_and_persists_alias(self):
        original = envelope(comment_text="@虚构助手\n 根据 测试库回答问题")
        first = self.store.enqueue("fixture-binding", original)
        old_rows = self.raw_tasks()
        changed = deepcopy(original)
        changed["command_id"] = "whitespace-alias"
        changed["comment_text"] = "@虚构助手 根据测试库回答问题"
        changed["evidence"]["observed_at_utc"] = utc(time.time() + 1)
        same = self.store.enqueue("fixture-binding", changed)
        self.assertEqual(same["task_id"], first["task_id"])
        self.assertEqual(same["envelope"], original)
        self.assertEqual(self.raw_tasks(), old_rows)
        restarted = AgentStore(self.path)
        self.assertEqual(restarted.enqueue("fixture-binding", changed)["task_id"], first["task_id"])
        with self.assertRaisesRegex(AgentStoreError, "command_conflict"):
            restarted.enqueue("fixture-binding", changed | {"reference_id": "other-reference"})
        self.assertEqual(len(restarted.list_tasks()), 1)

    def test_same_command_whitespace_variation_does_not_rewrite_old_envelope(self):
        original = envelope(comment_text="@虚构助手\n根据 测试库回答问题")
        first = self.store.enqueue("fixture-binding", original)
        repeated = original | {"comment_text": "@虚构助手 根据测试库回答问题"}
        self.assertEqual(self.store.enqueue("fixture-binding", repeated)["task_id"], first["task_id"])
        self.assertEqual(self.store.get_task(first["task_id"])["envelope"], original)

    def test_same_reference_changed_body_video_or_source_is_conflict_not_silently_reused(self):
        self.queued()
        changes = ({"comment_text": "真正不同的问题"}, {"video_id": "7999999000000000099"},
                   {"native_comment_id": "other-native"}, {"source_mode": "isolated"})
        for index, change in enumerate(changes):
            with self.subTest(change=change), self.assertRaisesRegex(AgentStoreError, "reference_conflict"):
                self.queued(command_id="conflicting-reference-" + str(index), **change)
        self.assertEqual(len(self.store.list_tasks()), 1)

    def test_concurrent_whitespace_commands_create_only_one_reference_task(self):
        gate = threading.Barrier(2)
        other = AgentStore(self.path)
        inputs = [(self.store, envelope(command_id="first-alias", comment_text="@虚构助手 根据测试库回答问题")),
                  (other, envelope(command_id="second-alias", comment_text="@虚构助手\n根据 测试库回答问题"))]
        def enqueue(value):
            store, item = value
            gate.wait(timeout=3)
            return store.enqueue("fixture-binding", item)
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(enqueue, inputs))
        self.assertEqual(results[0]["task_id"], results[1]["task_id"])
        self.assertEqual(len(self.store.list_tasks()), 1)

    def test_legacy_equivalent_reference_rows_remain_unchanged_and_only_earliest_claims(self):
        first = self.queued()
        duplicate = self.legacy_duplicate(first["task_id"], comment_text="@虚构助手\n根据 测试库回答问题")
        before = self.raw_tasks()
        incoming = envelope(command_id="third-alias")
        self.assertEqual(self.store.enqueue("fixture-binding", incoming)["task_id"], first["task_id"])
        self.assertEqual(self.raw_tasks(), before)
        self.assertIsNone(self.store.claim(duplicate))
        self.assertIsNotNone(self.store.claim(first["task_id"]))
        self.assertEqual(len(self.store.list_tasks()), 2)
        group = self.store.recovery_snapshot()["duplicate_reference_groups"][0]
        self.assertEqual(group["canonical_task_id"], first["task_id"])

    def test_legacy_same_reference_conflicting_rows_fail_closed_without_modification(self):
        first = self.queued()
        self.legacy_duplicate(first["task_id"], comment_text="另一个真正不同的问题")
        before = self.raw_tasks()
        with self.assertRaisesRegex(AgentStoreError, "reference_conflict"):
            self.queued(command_id="new-input")
        with self.assertRaisesRegex(AgentStoreError, "reference_conflict"):
            self.store.claim(first["task_id"])
        self.assertEqual(self.raw_tasks(), before)
        group = self.store.recovery_snapshot()["duplicate_reference_groups"][0]
        self.assertIsNone(group["canonical_task_id"])
        self.assertEqual(group["barrier_reason"], "reference_conflict")

    def test_legacy_reference_with_reserved_or_sent_peer_cannot_send_another_ready_reply(self):
        for index, outcome in enumerate((None, "b_view_visible", "delivery_unknown", "verification_required", "failed_no_submit")):
            ref = "legacy-delivery-" + str(index)
            reply = self.ready(command_id="origin-" + str(index), reference_id=ref)
            duplicate = self.legacy_duplicate(reply["task_id"], command_id="legacy-delivery-copy-" + str(index), with_reply=True)
            reserved = self.store.reserve_send(reply["task_id"], verification(reference_id=ref))
            if outcome:
                self.store.finish_send(reply["task_id"], reserved["send_token"], outcome, {"fixture": True})
            restarted = AgentStore(self.path)
            before = self.raw_tasks()
            with self.subTest(outcome=outcome), self.assertRaisesRegex(AgentStoreError, "reference_delivery_conflict"):
                restarted.enqueue("fixture-binding", envelope(command_id="new-legacy-" + str(index), reference_id=ref))
            with self.assertRaisesRegex(AgentStoreError, "reference_delivery_conflict"):
                restarted.reserve_send(duplicate, verification(reference_id=ref))
            with self.assertRaisesRegex(AgentStoreError, "reference_delivery_conflict"):
                restarted.requeue_unsent(duplicate, "publisher_preflight_failed")
            self.assertEqual(self.raw_tasks(), before)
            self.assertEqual(restarted.get_reply(duplicate)["state"], "ready")

    def test_reserved_crash_survives_restart_and_reference_alias_cannot_create_resend(self):
        reply = self.ready()
        reserved = self.store.reserve_send(reply["task_id"], verification())
        restarted = AgentStore(self.path)
        duplicate = restarted.enqueue("fixture-binding", envelope(command_id="after-crash-alias", comment_text="@虚构助手\n根据测试库回答问题"))
        self.assertEqual(duplicate["task_id"], reply["task_id"])
        self.assertIsNone(restarted.claim(reply["task_id"]))
        self.assertIsNone(restarted.reserve_send(reply["task_id"], verification()))
        self.assertEqual(restarted.get_reply(reply["task_id"])["send_token"], reserved["send_token"])
        self.assertEqual(restarted.recovery_snapshot()["reserved_unconfirmed_task_ids"], [reply["task_id"]])

    def test_hold_ready_reply_preserves_content_and_target_but_stops_publication(self):
        reply = self.ready()
        held = self.store.hold_reply(reply["task_id"], "existing_receiver_reply")
        self.assertEqual(held["state"], "needs_review")
        self.assertEqual(held["reason_code"], "existing_receiver_reply")
        self.assertEqual(held["proposal"], reply["proposal"])
        self.assertEqual(held["target"], reply["target"])
        self.assertEqual(self.store.get_task(reply["task_id"])["reason_code"], "existing_receiver_reply")
        self.assertIsNone(self.store.reserve_send(reply["task_id"], verification()))
        self.assertIsNone(self.store.claim(reply["task_id"]))
        with self.assertRaisesRegex(AgentStoreError, "reply_not_unsent_ready"):
            self.store.requeue_unsent(reply["task_id"], "citation_mismatch")

    def test_hold_cannot_change_reserved_reply_and_is_atomic_on_storage_error(self):
        reply = self.ready()
        with closing(sqlite3.connect(self.path)) as db:
            db.execute("""CREATE TRIGGER fixture_abort_hold BEFORE UPDATE OF state ON agent_replies
              WHEN NEW.state='needs_review' BEGIN SELECT RAISE(ABORT,'fixture_abort'); END""")
            db.commit()
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.hold_reply(reply["task_id"], "publisher_preflight_failed")
        self.assertEqual(self.store.get_task(reply["task_id"])["state"], "answer_ready")
        self.assertEqual(self.store.get_reply(reply["task_id"]), reply)
        with closing(sqlite3.connect(self.path)) as db:
            db.execute("DROP TRIGGER fixture_abort_hold")
            db.commit()
        self.store.reserve_send(reply["task_id"], verification())
        with self.assertRaisesRegex(AgentStoreError, "reply_not_unsent_ready"):
            self.store.hold_reply(reply["task_id"], "publisher_preflight_failed")

    def test_native_comment_cannot_be_omitted_from_fresh_verification(self):
        reply = self.ready(reference_quality="native", native_comment_id="native-comment-fixture")
        self.assertIsNone(self.store.reserve_send(reply["task_id"], verification()))
        self.assertIsNone(self.store.reserve_send(reply["task_id"], verification(native_comment_id="wrong-native")))
        self.assertIsNotNone(self.store.reserve_send(reply["task_id"], verification(native_comment_id="native-comment-fixture")))

    def test_release_preflight_hold_restores_same_proposal_and_archives_complete_hold(self):
        original = self.ready()
        original_task = self.store.get_task(original["task_id"])
        held = self.store.hold_reply(original["task_id"], "preflight_failed")
        released = self.store.release_held_reply(original["task_id"], "notification_reacquire_fixed")
        self.assertEqual(released["state"], "ready")
        self.assertEqual(released["reply_id"], original["reply_id"])
        self.assertEqual(released["proposal"], original["proposal"])
        self.assertEqual(released["target"], original["target"])
        self.assertEqual(released["created_at"], original["created_at"])
        self.assertIsNone(released["send_token"])
        task = self.store.get_task(original["task_id"])
        self.assertEqual(task["state"], "answer_ready")
        for key in ("claim_token", "lease_until", "attempts", "envelope", "command_id"):
            self.assertEqual(task[key], original_task[key])
        archive = self.store.list_reply_hold_releases(original["task_id"])
        self.assertEqual(len(archive), 1)
        self.assertEqual(archive[0]["reply"], held)
        self.assertEqual(archive[0]["reason_code"], "notification_reacquire_fixed")
        self.assertEqual(released["evidence"]["release_id"], archive[0]["release_id"])
        self.assertIsNone(self.store.claim(original["task_id"]))

    def test_release_is_persistently_once_even_if_publisher_holds_again(self):
        reply = self.ready()
        self.store.hold_reply(reply["task_id"], "preflight_failed")
        self.store.release_held_reply(reply["task_id"], "operator_verified_reacquire_fix")
        restarted = AgentStore(self.path)
        held_again = restarted.hold_reply(reply["task_id"], "preflight_failed")
        with self.assertRaisesRegex(AgentStoreError, "hold_release_limit_reached"):
            restarted.release_held_reply(reply["task_id"], "operator_second_release")
        self.assertEqual(restarted.get_reply(reply["task_id"]), held_again)
        self.assertEqual(restarted.get_task(reply["task_id"])["state"], "needs_review")
        self.assertEqual(len(restarted.list_reply_hold_releases(reply["task_id"])), 1)

    def test_release_requires_preflight_hold_exactly_and_rejects_other_review_reasons(self):
        for index, reason in enumerate(("existing_receiver_reply", "target_conflict", "delivery_unknown", "publisher_preflight_failed")):
            reply = self.ready(command_id="hold-reason-" + str(index), reference_id="hold-reason-ref-" + str(index))
            held = self.store.hold_reply(reply["task_id"], reason)
            with self.subTest(reason=reason), self.assertRaisesRegex(AgentStoreError, "hold_reason_not_releasable"):
                self.store.release_held_reply(reply["task_id"], "operator_reviewed")
            self.assertEqual(self.store.get_reply(reply["task_id"]), held)
            self.assertEqual(self.store.list_reply_hold_releases(reply["task_id"]), [])

    def test_release_rejects_ready_reserved_and_all_delivery_outcomes(self):
        for index, outcome in enumerate(("ready", "reserved", "b_view_visible", "delivery_unknown", "verification_required", "failed_no_submit")):
            ref = "release-send-ref-" + str(index)
            reply = self.ready(command_id="release-send-" + str(index), reference_id=ref)
            if outcome != "ready":
                reserved = self.store.reserve_send(reply["task_id"], verification(reference_id=ref))
                if outcome != "reserved":
                    self.store.finish_send(reply["task_id"], reserved["send_token"], outcome, {"fixture": True})
            before = self.store.get_reply(reply["task_id"])
            with self.subTest(outcome=outcome), self.assertRaisesRegex(AgentStoreError, "reply_not_held_unsent"):
                self.store.release_held_reply(reply["task_id"], "operator_reviewed")
            self.assertEqual(self.store.get_reply(reply["task_id"]), before)
            self.assertEqual(self.store.list_reply_hold_releases(reply["task_id"]), [])

    def test_release_requires_activity_and_fixed_operator_reason(self):
        reply = self.ready()
        held = self.store.hold_reply(reply["task_id"], "preflight_failed")
        for reason in ("not a code", "完整原文不能当原因", "Cookie: fiction"):
            with self.subTest(reason=reason), self.assertRaisesRegex(AgentStoreError, "invalid_reason_code"):
                self.store.release_held_reply(reply["task_id"], reason)
        self.store.deactivate("fixture-binding")
        with self.assertRaisesRegex(AgentStoreError, "binding_inactive"):
            self.store.release_held_reply(reply["task_id"], "operator_reviewed")
        self.assertEqual(self.store.get_reply(reply["task_id"]), held)
        self.assertEqual(self.store.list_reply_hold_releases(reply["task_id"]), [])

    def test_release_never_changes_completed_ingestion_or_original_tool_history(self):
        claim = self.processing()
        job = self.store.ensure_ingestion(claim["task_id"], claim["claim_token"])
        succeeded = self.store.observe_ingestion(job["job_id"], ingestion_observation(job))
        self.store.record_step(claim["task_id"], claim["claim_token"], "tool_call", {"name": "save_current_video"})
        reply = self.store.complete(claim["task_id"], claim["claim_token"], import_proposal(succeeded))
        before_events = self.store.list_ingestion_events(job["job_id"])
        before_steps = self.store.list_steps(claim["task_id"])
        self.store.hold_reply(claim["task_id"], "preflight_failed")
        released = self.store.release_held_reply(claim["task_id"], "notification_reacquire_fixed")
        self.assertEqual(released["proposal"], reply["proposal"])
        self.assertEqual(self.store.get_ingestion(job["job_id"]), succeeded)
        self.assertEqual(self.store.get_task_ingestion(claim["task_id"]), succeeded)
        self.assertEqual(self.store.list_ingestion_events(job["job_id"]), before_events)
        self.assertEqual(self.store.list_steps(claim["task_id"]), before_steps)
        self.assertIsNone(self.store.reserve_send(claim["task_id"], verification(comment_text="错误原评论")))
        self.assertIsNotNone(self.store.reserve_send(claim["task_id"], verification()))

    def test_release_archive_and_state_change_rollback_together(self):
        reply = self.ready()
        held = self.store.hold_reply(reply["task_id"], "preflight_failed")
        with closing(sqlite3.connect(self.path)) as db:
            db.execute("""CREATE TRIGGER fixture_abort_release BEFORE UPDATE OF state ON agent_tasks
              WHEN NEW.state='answer_ready' BEGIN SELECT RAISE(ABORT,'fixture_abort'); END""")
            db.commit()
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.release_held_reply(reply["task_id"], "operator_reviewed")
        self.assertEqual(self.store.get_reply(reply["task_id"]), held)
        self.assertEqual(self.store.get_task(reply["task_id"])["state"], "needs_review")
        self.assertEqual(self.store.list_reply_hold_releases(reply["task_id"]), [])

    def test_hold_release_audit_read_does_not_migrate_existing_ledger(self):
        before = hashlib.sha256(self.path.read_bytes()).hexdigest()
        self.assertEqual(self.store.list_reply_hold_releases("missing"), [])
        self.assertEqual(hashlib.sha256(self.path.read_bytes()).hexdigest(), before)

    def test_recovery_snapshot_is_readonly_bounded_metadata_not_automatic_retry(self):
        queued = self.queued(command_id="queued-observation", reference_id="queued-observation")
        now = time.time()
        with patch("app.mention_agent.store.time.time", return_value=now):
            processing = self.processing(command_id="expired-observation", reference_id="expired-observation")
        reserved_reply = self.ready(command_id="reserved-observation", reference_id="reserved-observation")
        self.store.reserve_send(reserved_reply["task_id"], verification(reference_id="reserved-observation"))
        before = hashlib.sha256(self.path.read_bytes()).hexdigest()
        with patch("app.mention_agent.store.time.time", return_value=now + 181):
            result = AgentStore(self.path).recovery_snapshot("fixture-binding")
        self.assertEqual(result["queued_task_ids"], [queued["task_id"]])
        self.assertEqual(result["expired_processing_task_ids"], [processing["task_id"]])
        self.assertEqual(result["reserved_unconfirmed_task_ids"], [reserved_reply["task_id"]])
        self.assertEqual(hashlib.sha256(self.path.read_bytes()).hexdigest(), before)
        self.assertEqual(self.store.get_task(processing["task_id"])["state"], "processing")
        self.assertNotIn("comment_text", json.dumps(result))
        self.assertNotIn("claim_token", json.dumps(result))

    def test_ingestion_reads_do_not_migrate_or_mutate_existing_ledger(self):
        ready = self.ready()
        before = hashlib.sha256(self.path.read_bytes()).hexdigest()
        self.assertIsNone(self.store.get_ingestion("missing"))
        self.assertIsNone(self.store.get_task_ingestion(ready["task_id"]))
        self.assertEqual(self.store.list_ingestions(), [])
        self.assertEqual(self.store.list_ingestion_events("missing"), [])
        self.assertTrue(all(not ids for ids in self.store.recovery_snapshot()["ingestion_job_ids_by_state"].values()))
        self.assertEqual(hashlib.sha256(self.path.read_bytes()).hexdigest(), before)
        self.assertEqual(self.store.get_reply(ready["task_id"]), ready)

    def test_ingestion_intention_uses_fixed_task_target_and_survives_restart(self):
        claim = self.processing(comment_text="@虚构助手 请保存这个视频到测试库")
        before_tasks = self.raw_tasks()
        job = self.store.ensure_ingestion(claim["task_id"], claim["claim_token"])
        self.assertEqual(job["binding_id"], "fixture-binding")
        self.assertEqual(job["instance_id"], "fixture-desktop")
        self.assertEqual(job["video_id"], envelope()["video_id"])
        self.assertEqual(job["knowledge_base_id"], 7)
        self.assertEqual(job["backend_request_id"], job["job_id"])
        self.assertEqual(job["state"], "queued")
        self.assertIsNone(job["dispatch_token"])
        self.assertEqual(self.raw_tasks(), before_tasks)
        restarted = AgentStore(self.path)
        self.assertEqual(restarted.get_task_ingestion(claim["task_id"]), job)
        self.assertEqual(restarted.ensure_ingestion(claim["task_id"], claim["claim_token"]), job)
        self.assertNotIn("comment_text", json.dumps(restarted.list_ingestions()))

    def test_same_reference_replay_and_new_comment_share_one_ingestion(self):
        first = self.processing()
        job = self.store.ensure_ingestion(first["task_id"], first["claim_token"])
        repeated = self.queued(command_id="save-alias", comment_text="@虚构助手\n根据测试库回答问题")
        self.assertEqual(repeated["task_id"], first["task_id"])
        second = self.processing(command_id="save-other-comment", reference_id="save-other-reference")
        self.assertEqual(self.store.ensure_ingestion(second["task_id"], second["claim_token"])["job_id"], job["job_id"])
        self.assertEqual(len(self.store.list_ingestions()), 1)
        reserved = self.store.reserve_ingestion(first["task_id"], first["claim_token"])
        self.assertIsNotNone(reserved["dispatch_token"])
        self.assertIsNone(self.store.reserve_ingestion(second["task_id"], second["claim_token"]))

    def test_concurrent_comments_have_one_durable_job_and_one_dispatch(self):
        claims = [self.processing(command_id="concurrent-save-" + str(i), reference_id="concurrent-save-ref-" + str(i)) for i in range(2)]
        stores = [self.store, AgentStore(self.path)]
        gate = threading.Barrier(2)
        def run(value):
            store, claim = value
            gate.wait(timeout=3)
            job = store.ensure_ingestion(claim["task_id"], claim["claim_token"])
            reservation = store.reserve_ingestion(claim["task_id"], claim["claim_token"])
            return job, reservation
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(run, zip(stores, claims)))
        self.assertEqual(results[0][0]["job_id"], results[1][0]["job_id"])
        self.assertEqual(sum(row[1] is not None for row in results), 1)
        self.assertEqual(len(self.store.list_ingestions()), 1)
        events = self.store.list_ingestion_events(results[0][0]["job_id"])
        self.assertEqual(sum(row["kind"] == "dispatch_reserved" for row in events), 1)

    def test_ingestion_dedup_does_not_cross_binding_library_or_source_boundary(self):
        live = self.processing()
        isolated = self.processing(command_id="isolated-save", reference_id="isolated-save-ref", source_mode="isolated")
        replay = self.processing(command_id="replay-save", reference_id="replay-save-ref", source_mode="replay")
        values = [self.store.ensure_ingestion(row["task_id"], row["claim_token"]) for row in (live, isolated, replay)]
        for row in (isolated, replay):
            self.assertIsNone(self.store.reserve_ingestion(row["task_id"], row["claim_token"]))
        self.store.put_binding(binding(binding_id="other-library-binding", knowledge_base_id=8))
        other_task = self.store.enqueue("other-library-binding", envelope(command_id="other-library-save", reference_id="other-library-ref"))
        other = self.store.claim(other_task["task_id"])
        values.append(self.store.ensure_ingestion(other["task_id"], other["claim_token"]))
        self.assertEqual(len({row["job_id"] for row in values}), 4)
        self.assertEqual(len(self.store.list_ingestions("fixture-binding")), 3)
        self.assertIsNotNone(self.store.reserve_ingestion(live["task_id"], live["claim_token"]))

    def test_ingestion_requires_current_claim_and_binding_and_accepts_1200_second_lease(self):
        now = time.time()
        with patch("app.mention_agent.store.time.time", return_value=now):
            task = self.queued()
            claim = self.store.claim(task["task_id"], lease_seconds=1200)
            self.assertEqual(claim["lease_until"], now + 1200)
        with self.assertRaisesRegex(AgentStoreError, "stale_claim_token"):
            self.store.ensure_ingestion(task["task_id"], "wrong")
        with patch("app.mention_agent.store.time.time", return_value=now + 1199):
            self.store.ensure_ingestion(task["task_id"], claim["claim_token"])
        with patch("app.mention_agent.store.time.time", return_value=now + 1201):
            with self.assertRaisesRegex(AgentStoreError, "claim_expired"):
                self.store.reserve_ingestion(task["task_id"], claim["claim_token"])
        self.assertEqual(self.store.get_task(task["task_id"])["state"], "needs_review")
        self.assertIsNone(self.store.get_task_ingestion(task["task_id"])["dispatch_token"])
        second = self.processing(command_id="deactivated-save", reference_id="deactivated-save-ref")
        self.store.ensure_ingestion(second["task_id"], second["claim_token"])
        self.store.deactivate("fixture-binding")
        with self.assertRaisesRegex(AgentStoreError, "task_not_processing"):
            self.store.reserve_ingestion(second["task_id"], second["claim_token"])

    def test_reserved_ingestion_crash_never_reopens_dispatch_on_restart(self):
        claim = self.processing()
        job = self.store.ensure_ingestion(claim["task_id"], claim["claim_token"])
        reserved = self.store.reserve_ingestion(claim["task_id"], claim["claim_token"])
        restarted = AgentStore(self.path)
        self.assertIsNone(restarted.reserve_ingestion(claim["task_id"], claim["claim_token"]))
        pending = restarted.observe_ingestion(job["job_id"], ingestion_observation(job, backend_state="pending",
            membership_exists=False, cache_status="pending", indexed_content_chunks=0))
        self.assertEqual(pending["state"], "reserved")
        self.assertEqual(pending["dispatch_token"], reserved["dispatch_token"])
        self.assertIsNone(restarted.reserve_ingestion(claim["task_id"], claim["claim_token"]))
        self.assertEqual(restarted.recovery_snapshot()["ingestion_job_ids_by_state"]["reserved"], [job["job_id"]])

    def test_ingestion_ack_is_not_success_and_result_is_idempotent(self):
        claim = self.processing()
        self.store.ensure_ingestion(claim["task_id"], claim["claim_token"])
        reserved = self.store.reserve_ingestion(claim["task_id"], claim["claim_token"])
        evidence = {"backend_request_id": reserved["job_id"], "backend_task_id": "fixture-backend-task", "http_status": 202}
        accepted = self.store.finish_ingestion_submission(reserved["job_id"], reserved["dispatch_token"], "accepted", evidence)
        self.assertEqual(accepted["state"], "accepted")
        self.assertIsNone(accepted["observation"])
        self.assertEqual(self.store.finish_ingestion_submission(reserved["job_id"], reserved["dispatch_token"], "accepted", evidence), accepted)
        with self.assertRaisesRegex(AgentStoreError, "ingestion_submission_conflict"):
            self.store.finish_ingestion_submission(reserved["job_id"], reserved["dispatch_token"], "submission_unknown", evidence)
        with self.assertRaisesRegex(AgentStoreError, "stale_ingestion_dispatch_token"):
            self.store.finish_ingestion_submission(reserved["job_id"], "wrong", "accepted", evidence)
        self.assertEqual(self.store.get_ingestion(reserved["job_id"]), accepted)

    def test_unknown_http_submission_only_reconciles_and_does_not_repeat(self):
        claim = self.processing()
        self.store.ensure_ingestion(claim["task_id"], claim["claim_token"])
        reserved = self.store.reserve_ingestion(claim["task_id"], claim["claim_token"])
        unknown = self.store.finish_ingestion_submission(reserved["job_id"], reserved["dispatch_token"], "submission_unknown",
            {"backend_request_id": reserved["job_id"], "reason_code": "http_timeout"})
        self.assertEqual(unknown["state"], "submission_unknown")
        second = self.processing(command_id="after-timeout-save", reference_id="after-timeout-ref")
        restarted = AgentStore(self.path)
        self.assertEqual(restarted.ensure_ingestion(second["task_id"], second["claim_token"])["job_id"], reserved["job_id"])
        self.assertIsNone(restarted.reserve_ingestion(second["task_id"], second["claim_token"]))
        success = restarted.observe_ingestion(reserved["job_id"], ingestion_observation(reserved))
        self.assertEqual(success["state"], "succeeded")
        self.assertEqual(success["submission_outcome"], "submission_unknown")
        self.assertEqual(AgentStore(self.path).get_ingestion(reserved["job_id"]), success)

    def test_completion_needs_membership_done_and_actual_content_index(self):
        claim = self.processing()
        job = self.store.ensure_ingestion(claim["task_id"], claim["claim_token"])
        failures = ({"membership_exists": False}, {"cache_status": "embedding"}, {"indexed_content_chunks": 0})
        for change in failures:
            before = self.store.get_ingestion(job["job_id"])
            with self.subTest(change=change), self.assertRaisesRegex(AgentStoreError, "incomplete_ingestion_success_evidence"):
                self.store.observe_ingestion(job["job_id"], ingestion_observation(job, **change))
            self.assertEqual(self.store.get_ingestion(job["job_id"]), before)
        ready = self.store.observe_ingestion(job["job_id"], ingestion_observation(job))
        self.assertEqual(ready["state"], "succeeded")
        self.assertIsNone(ready["dispatch_token"])  # Reuse already indexed content without a new request.
        self.assertIsNone(self.store.reserve_ingestion(claim["task_id"], claim["claim_token"]))

    def test_pending_failed_or_unknown_never_infer_success_from_content_fields(self):
        for index, state in enumerate(("pending", "accepted", "processing", "failed", "unknown")):
            claim = self.processing(command_id="backend-state-" + str(index), reference_id="backend-state-ref-" + str(index),
                                    video_id="79999990000000000" + str(20 + index))
            job = self.store.ensure_ingestion(claim["task_id"], claim["claim_token"])
            result = self.store.observe_ingestion(job["job_id"], ingestion_observation(job, backend_state=state))
            with self.subTest(state=state):
                self.assertNotEqual(result["state"], "succeeded")
                with self.assertRaisesRegex(AgentStoreError, "import_proposal_state_mismatch"):
                    self.store.complete(claim["task_id"], claim["claim_token"], import_proposal(result, import_state="succeeded"))

    def test_observation_rejects_wrong_target_and_unbounded_or_nonexact_evidence(self):
        claim = self.processing()
        job = self.store.ensure_ingestion(claim["task_id"], claim["claim_token"])
        changes = ({"backend_request_id": "wrong"}, {"binding_id": "other"}, {"instance_id": "other"},
                   {"knowledge_base_id": 8}, {"video_id": "7999999000000000099"}, {"knowledge_base_id": True},
                   {"membership_exists": 1}, {"indexed_content_chunks": True}, {"indexed_content_chunks": -1},
                   {"raw_response": "must not persist"}, {"backend_state": "done"})
        for change in changes:
            with self.subTest(change=change), self.assertRaises(AgentStoreError):
                self.store.observe_ingestion(job["job_id"], ingestion_observation(job, **change))
        self.assertEqual(self.store.get_ingestion(job["job_id"]), job)

    def test_late_submission_receipt_cannot_downgrade_verified_index_success(self):
        claim = self.processing()
        self.store.ensure_ingestion(claim["task_id"], claim["claim_token"])
        reserved = self.store.reserve_ingestion(claim["task_id"], claim["claim_token"])
        self.store.observe_ingestion(reserved["job_id"], ingestion_observation(reserved))
        result = self.store.finish_ingestion_submission(reserved["job_id"], reserved["dispatch_token"], "accepted",
            {"backend_request_id": reserved["job_id"]})
        self.assertEqual(result["state"], "succeeded")
        with self.assertRaisesRegex(AgentStoreError, "ingestion_observation_conflict"):
            self.store.observe_ingestion(reserved["job_id"], ingestion_observation(reserved, backend_state="failed", cache_status="failed"))
        self.assertEqual(self.store.get_ingestion(reserved["job_id"]), result)

    def test_import_status_reply_is_task_scoped_and_has_no_question_sources(self):
        first = self.processing()
        job = self.store.ensure_ingestion(first["task_id"], first["claim_token"])
        other = self.processing(command_id="other-video-save", reference_id="other-video-ref", video_id="7999999000000000099")
        other_job = self.store.ensure_ingestion(other["task_id"], other["claim_token"])
        for forged in (import_proposal(other_job), import_proposal(job, import_state="succeeded")):
            with self.assertRaises(AgentStoreError):
                self.store.complete(first["task_id"], first["claim_token"], forged)
        for forged in (import_proposal(job, source_ids=["fake-source"]), import_proposal(job, sources=[{"id": "fake"}]),
                       proposal(import_job_id=job["job_id"], import_state="queued")):
            with self.assertRaises(AgentStoreError):
                self.store.complete(first["task_id"], first["claim_token"], forged)
        reply = self.store.complete(first["task_id"], first["claim_token"], import_proposal(job))
        self.assertEqual(reply["kind"], "import_status")
        self.assertEqual(reply["import_state"], "queued")
        self.assertEqual(reply["sources"], [])
        self.assertEqual(reply["source_ids"], [])
        self.assertEqual(reply["target"]["video_id"], envelope()["video_id"])

    def test_import_reply_success_is_allowed_only_after_exact_job_success(self):
        claim = self.processing()
        job = self.store.ensure_ingestion(claim["task_id"], claim["claim_token"])
        done = self.store.observe_ingestion(job["job_id"], ingestion_observation(job))
        reply = self.store.complete(claim["task_id"], claim["claim_token"], import_proposal(done))
        self.assertEqual(reply["import_state"], "succeeded")
        self.assertEqual(self.store.complete(claim["task_id"], claim["claim_token"], import_proposal(done)), reply)
        self.assertIsNotNone(self.store.reserve_send(claim["task_id"], verification()))

    def test_import_reply_state_is_rechecked_before_public_send(self):
        claim = self.processing()
        job = self.store.ensure_ingestion(claim["task_id"], claim["claim_token"])
        self.store.complete(claim["task_id"], claim["claim_token"], import_proposal(job))
        self.store.observe_ingestion(job["job_id"], ingestion_observation(job))
        self.assertIsNone(self.store.reserve_send(claim["task_id"], verification()))
        self.assertEqual(self.store.get_reply(claim["task_id"])["state"], "ready")

    def test_failed_submission_and_failed_backend_are_persistent_no_retry_barriers(self):
        for index, outcome in enumerate(("failed_no_submit", "submission_unknown")):
            claim = self.processing(command_id="failed-submit-" + str(index), reference_id="failed-submit-ref-" + str(index),
                                    video_id="79999990000000000" + str(40 + index))
            job = self.store.ensure_ingestion(claim["task_id"], claim["claim_token"])
            reserved = self.store.reserve_ingestion(claim["task_id"], claim["claim_token"])
            self.store.finish_ingestion_submission(job["job_id"], reserved["dispatch_token"], outcome,
                {"backend_request_id": job["job_id"], "reason_code": "fixture_failure"})
            if outcome == "submission_unknown":
                self.store.observe_ingestion(job["job_id"], ingestion_observation(job, backend_state="failed", cache_status="failed"))
            restarted = AgentStore(self.path)
            before = restarted.get_ingestion(job["job_id"])
            restarted.observe_ingestion(job["job_id"], ingestion_observation(job, backend_state="pending", cache_status="pending",
                                                                           membership_exists=False, indexed_content_chunks=0))
            self.assertEqual(restarted.get_ingestion(job["job_id"])["state"], before["state"])
            self.assertIsNone(restarted.reserve_ingestion(claim["task_id"], claim["claim_token"]))

    def test_ingestion_additive_tables_preserve_old_reserved_send_and_steps(self):
        old = self.ready()
        reserved_reply = self.store.reserve_send(old["task_id"], verification())
        old_task = self.store.get_task(old["task_id"])
        claim = self.processing(command_id="new-ingestion", reference_id="new-ingestion-ref")
        self.store.record_step(claim["task_id"], claim["claim_token"], "tool_call", {"name": "save_video"})
        old_steps = self.store.list_steps(claim["task_id"])
        self.store.ensure_ingestion(claim["task_id"], claim["claim_token"])
        self.assertEqual(self.store.get_task(old["task_id"]), old_task)
        self.assertEqual(self.store.get_reply(old["task_id"]), reserved_reply)
        self.assertEqual(self.store.list_steps(claim["task_id"]), old_steps)
        self.assertIsNone(self.store.reserve_send(old["task_id"], verification()))

    def test_ingestion_dispatch_and_observation_are_atomic_with_audit_events(self):
        claim = self.processing()
        job = self.store.ensure_ingestion(claim["task_id"], claim["claim_token"])
        before_events = self.store.list_ingestion_events(job["job_id"])
        with closing(sqlite3.connect(self.path)) as db:
            db.execute("""CREATE TRIGGER fixture_abort_ingestion_event BEFORE INSERT ON agent_ingestion_events
              BEGIN SELECT RAISE(ABORT,'fixture_abort'); END""")
            db.commit()
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.reserve_ingestion(claim["task_id"], claim["claim_token"])
        self.assertEqual(self.store.get_ingestion(job["job_id"]), job)
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.observe_ingestion(job["job_id"], ingestion_observation(job))
        self.assertEqual(self.store.get_ingestion(job["job_id"]), job)
        self.assertEqual(self.store.list_ingestion_events(job["job_id"]), before_events)

    def test_ingestion_creation_link_and_schema_rollback_together_on_storage_failure(self):
        # Initialize the additive schema with an unrelated fixture intention.
        first = self.processing()
        self.store.ensure_ingestion(first["task_id"], first["claim_token"])
        second = self.processing(command_id="atomic-create", reference_id="atomic-create-ref", video_id="7999999000000000099")
        before_jobs = self.store.list_ingestions()
        with closing(sqlite3.connect(self.path)) as db:
            db.execute("""CREATE TRIGGER fixture_abort_ingestion_link BEFORE INSERT ON agent_task_ingestions
              BEGIN SELECT RAISE(ABORT,'fixture_abort'); END""")
            db.commit()
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.ensure_ingestion(second["task_id"], second["claim_token"])
        self.assertEqual(self.store.list_ingestions(), before_jobs)
        self.assertIsNone(self.store.get_task_ingestion(second["task_id"]))

    def test_ingestion_submission_only_stores_fixed_nonsensitive_fields(self):
        claim = self.processing()
        job = self.store.ensure_ingestion(claim["task_id"], claim["claim_token"])
        reserved = self.store.reserve_ingestion(claim["task_id"], claim["claim_token"])
        for change in ({"api_key": "fiction-only"}, {"raw_response": "not retained"}, {"reason_code": "has spaces"},
                       {"http_status": True}, {"backend_request_id": "wrong"}, {"backend_task_id": {"cookie": "fiction-only"}}):
            evidence = {"backend_request_id": job["job_id"], **change}
            with self.subTest(change=change), self.assertRaises(AgentStoreError):
                self.store.finish_ingestion_submission(job["job_id"], reserved["dispatch_token"], "accepted", evidence)
        self.assertEqual(self.store.get_ingestion(job["job_id"]), reserved)


if __name__ == "__main__":
    unittest.main()
