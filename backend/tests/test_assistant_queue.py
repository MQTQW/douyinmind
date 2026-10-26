"""Persistent product queue tests: temporary SQLite and fictional identities."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
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

from app.assistant.queue import AssistantQueue, QueueError
from app.mention_agent.contracts import Binding


def binding(**changes):
    return Binding.from_dict({"binding_id": "assistant-fixture", "instance_id": "fixture-desktop",
        "sender_profile_url": "https://www.douyin.com/user/fixture-A", "receiver_profile_url": "https://www.douyin.com/user/fixture-B",
        "knowledge_base_id": 7, "public_reply_allowed": True, "active": True, **changes})


def envelope(**changes):
    selected = binding()
    return {"command_id": "fixture-command", "reference_id": "fixture-reference", "reference_quality": "verified_ui", "native_comment_id": None,
        "sender_profile_url": selected.sender_profile_url, "receiver_profile_url": selected.receiver_profile_url,
        "video_id": "7999999000000000001", "comment_text": "@虚构助手 根据测试库回答问题", "source_mode": "live_ui",
        "evidence": {"real_mention": True, "unique_count": 1, "sender_verified": True, "video_verified": True,
                     "observed_at_utc": datetime.now(timezone.utc).isoformat()}, **changes}


def ready_proof(**changes):
    return {"state": "answer_ready", "agent_task_id": "fixture-agent-task", "reply_state": "ready",
        "send_token_present": False, "safe_to_publish": True, "binding_active": True, "public_reply_allowed": True, **changes}


class AssistantQueueTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.path = self.root / "queue.sqlite3"
        self.queue = AssistantQueue(self.path)

    def received(self, **changes):
        return self.queue.enqueue(binding(), envelope(**changes), kind="query")

    def finished(self, result, **changes):
        task = self.received(**changes)
        claim = self.queue.claim_next(run_id="fixture-run")
        return self.queue.finish(task["id"], claim["execution_token"], "execute", result)

    def test_whitespace_command_replay_survives_restart_with_original_observation(self):
        first = self.received()
        before = self.queue.get_task(first["id"])
        again = AssistantQueue(self.path).enqueue(binding(), envelope(command_id="different-fingerprint", comment_text="@虚构助手\n根据 测试库回答问题"))
        self.assertEqual(first["id"], again["id"])
        self.assertEqual(before, again)
        self.assertEqual(self.queue.counts()["queued"], 1)

    def test_latest_action_exposes_durable_progress_without_internal_claims(self):
        task = self.received()
        self.assertIsNone(self.queue.latest_action(task["id"]))
        action = self.queue.request_action(task["id"], "reconcile")
        self.assertEqual(set(self.queue.latest_action(task["id"])),
            {"id", "task_id", "action", "state", "reason_code", "created_at", "updated_at"})
        self.assertEqual(self.queue.latest_action(task["id"])["state"], "queued")
        self.queue.take_action()
        self.assertEqual(AssistantQueue(self.path).latest_action(task["id"])["state"], "processing")
        self.queue.finish_action(action["id"], "reconciliation_unavailable")
        latest = AssistantQueue(self.path).latest_action(task["id"])
        self.assertEqual(latest["state"], "failed")
        self.assertEqual(latest["reason_code"], "reconciliation_unavailable")
        self.assertIsNone(self.queue.latest_action("not-a-task"))

    def test_source_reference_lookup_is_readonly_canonical_and_preserves_frozen_task(self):
        task = self.received(reference_id=" exact opaque reference ")
        selected = binding()
        before = hashlib.sha256(self.path.read_bytes()).hexdigest()
        restarted = AssistantQueue(self.path)
        found = restarted.lookup_by_source_reference(selected.sender_profile_url.replace("www.", "") + "/?ignored=1",
            selected.receiver_profile_url, "live_ui", " exact opaque reference ")
        self.assertEqual(found, task)
        self.assertEqual(found["binding"], selected.to_dict())
        self.assertIsNone(restarted.lookup_by_source_reference(selected.sender_profile_url,
            selected.receiver_profile_url, "live_ui", "exact opaque reference"))
        self.assertEqual(hashlib.sha256(self.path.read_bytes()).hexdigest(), before)

    def test_source_reference_lookup_does_not_cross_sender_receiver_or_mode(self):
        task = self.received()
        selected = binding()
        for field, value in (("sender_profile_url", "https://www.douyin.com/user/different-sender"),
                ("receiver_profile_url", "https://www.douyin.com/user/different-receiver"),
                ("source_mode", "isolated"), ("source_mode", "replay")):
            arguments = {"sender_profile_url": selected.sender_profile_url, "receiver_profile_url": selected.receiver_profile_url,
                "source_mode": "live_ui", "reference_id": task["envelope"]["reference_id"], field: value}
            self.assertIsNone(self.queue.lookup_by_source_reference(**arguments))
        with self.assertRaisesRegex(QueueError, "original_comment_conflict"):
            self.queue.enqueue(binding(binding_id="replacement-binding", public_reply_allowed=False),
                envelope(command_id="replacement-command"))
        self.assertEqual(self.queue.get_task(task["id"]), task)

    def test_source_reference_lookup_rejects_malformed_values_without_mutation(self):
        selected = binding()
        arguments = {"sender_profile_url": selected.sender_profile_url, "receiver_profile_url": selected.receiver_profile_url,
            "source_mode": "live_ui", "reference_id": "fixture-reference"}
        for field, value in (("source_mode", []), ("source_mode", {}), ("source_mode", "network"),
                ("reference_id", []), ("reference_id", ""), ("reference_id", "x" * 257),
                ("sender_profile_url", "http://127.0.0.1/private"), ("receiver_profile_url", [])):
            with self.subTest(field=field, value=value), self.assertRaisesRegex(QueueError, "invalid_source_reference"):
                self.queue.lookup_by_source_reference(**(arguments | {field: value}))
        self.assertEqual(self.queue.list_tasks()["total"], 0)

    def test_same_original_comment_cannot_retarget_library_binding_video_or_body(self):
        self.received()
        for selected, changes in ((binding(knowledge_base_id=8, binding_id="new-binding"), {}),
                (binding(), {"video_id": "7999999000000000099"}), (binding(), {"comment_text": "真正不同的问题"})):
            with self.subTest(changes=changes), self.assertRaisesRegex(QueueError, "original_comment_conflict"):
                self.queue.enqueue(selected, envelope(command_id="changed-command", **changes))
        self.assertEqual(self.queue.list_tasks()["total"], 1)

    def test_incomplete_or_wrong_sender_envelope_never_enters_queue(self):
        for changes in ({"sender_profile_url": "https://www.douyin.com/user/other"},
                        {"evidence": {}}, {"reference_quality": "native"}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.received(**changes)
        self.assertEqual(self.queue.list_tasks()["total"], 0)

    def test_concurrent_intake_and_claim_are_once(self):
        other = AssistantQueue(self.path)
        gate = threading.Barrier(2)
        def enqueue(queue):
            gate.wait(timeout=3)
            return queue.enqueue(binding(), envelope())
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(enqueue, (self.queue, other)))
        self.assertEqual(results[0]["id"], results[1]["id"])
        gate = threading.Barrier(2)
        def claim(queue):
            gate.wait(timeout=3)
            return queue.claim_next(run_id="fixture-run")
        with ThreadPoolExecutor(max_workers=2) as pool:
            claims = list(pool.map(claim, (self.queue, other)))
        self.assertEqual(sum(item is not None for item in claims), 1)

    def test_waiting_import_releases_worker_for_new_query_and_poll_is_leased(self):
        waiting = self.finished({"state": "waiting_ingestion", "agent_task_id": "paid-import", "import_state": "processing"})
        query = self.received(command_id="query-2", reference_id="reference-2")
        self.assertEqual(self.queue.claim_next()["id"], query["id"])
        self.assertIsNone(self.queue.claim_waiting())
        with patch("app.assistant.queue.time.time", return_value=waiting["updated_at"] + 11):
            poll = self.queue.claim_waiting()
            self.assertEqual(poll["id"], waiting["id"])
            self.assertIsNone(AssistantQueue(self.path).claim_waiting())

    def test_poll_requires_real_import_success_and_ready_unsent_reply_before_publication(self):
        waiting = self.finished({"state": "waiting_ingestion", "agent_task_id": "import-agent", "import_state": "processing"})
        incomplete = self.queue.apply_reconciliation(waiting["id"], {"state": "waiting_ingestion", "import_state": "processing"})
        self.assertEqual(incomplete["state"], "waiting_ingestion")
        self.assertIsNone(self.queue.claim_next("publish"))
        result = self.queue.apply_reconciliation(waiting["id"], ready_proof(import_state="succeeded"))
        self.assertEqual(result["state"], "ready_to_publish")
        self.assertEqual(self.queue.claim_next("publish")["id"], waiting["id"])

    def test_verified_failed_import_can_publish_its_fixed_failure_receipt_once(self):
        for index, state in enumerate(("failed", "failed_no_submit")):
            task = self.finished({"state": "waiting_ingestion", "import_state": "processing"},
                                 command_id="failed-job-" + str(index), reference_id="failed-job-ref-" + str(index))
            result = self.queue.apply_reconciliation(task["id"], ready_proof(import_state=state))
            self.assertEqual(result["state"], "ready_to_publish")
            self.assertEqual(result["import_state"], state)

    def test_pending_processing_or_unknown_import_never_becomes_publishable_from_ready_claim(self):
        for index, state in enumerate(("pending", "accepted", "processing", "submission_unknown", "unknown")):
            task = self.finished({"state": "waiting_ingestion", "import_state": "processing"},
                                 command_id="unfinished-job-" + str(index), reference_id="unfinished-job-ref-" + str(index))
            result = self.queue.apply_reconciliation(task["id"], ready_proof(import_state=state))
            self.assertNotEqual(result["state"], "ready_to_publish")
        self.assertIsNone(self.queue.claim_next("publish"))

    def test_restart_never_requeues_paid_or_possibly_published_work(self):
        first = self.received()
        self.queue.claim_next(run_id="old-owner")
        second = self.received(command_id="comment-2", reference_id="ref-2")
        restarted = AssistantQueue(self.path)
        self.assertEqual(restarted.recover_interrupted(current_run_id="new-owner"), [first["id"]])
        self.assertEqual(restarted.get_task(first["id"])["state"], "needs_reconcile")
        self.assertEqual(restarted.claim_next(run_id="new-owner")["id"], second["id"])
        with self.assertRaisesRegex(QueueError, "resume_not_safe"):
            restarted.resume(first["id"])

    def test_publish_crash_needs_reconcile_and_unknown_is_permanent_terminal(self):
        task = self.finished(ready_proof())
        publication = self.queue.claim_next("publish", run_id="old-owner")
        self.queue.recover_interrupted(current_run_id="new-owner")
        with self.assertRaisesRegex(QueueError, "stale_task_claim"):
            self.queue.finish(task["id"], publication["publication_token"], "publish", {"state": "b_view_visible"})
        self.queue.apply_reconciliation(task["id"], {"state": "delivery_unknown", "send_token_present": True})
        late = self.queue.apply_reconciliation(task["id"], ready_proof())
        self.assertEqual(late["state"], "delivery_unknown")
        with self.assertRaisesRegex(QueueError, "resume_not_safe"):
            self.queue.resume(task["id"])
        self.assertIsNone(self.queue.claim_next("publish"))

    def test_resume_execution_requires_fresh_proof_no_paid_or_ingestion_effect(self):
        task = self.received()
        self.queue.claim_next(run_id="old-owner")
        self.queue.recover_interrupted(current_run_id="new-owner")
        proof = {"state": "queued", "safe_to_execute": True, "model_started": False,
                 "ingestion_submitted": False, "send_token_present": False, "binding_active": True}
        self.queue.apply_reconciliation(task["id"], proof | {"ingestion_submitted": True})
        with self.assertRaisesRegex(QueueError, "resume_not_safe"):
            self.queue.resume(task["id"])
        checked = self.queue.apply_reconciliation(task["id"], proof)
        with patch("app.assistant.queue.time.time", return_value=checked["reconciled_at"] + 121):
            with self.assertRaisesRegex(QueueError, "resume_not_safe"):
                self.queue.resume(task["id"])
        self.assertEqual(self.queue.resume(task["id"])["state"], "queued")
        self.assertEqual([event["kind"] for event in self.queue.list_events(task["id"])],
                         ["received", "reserved_execute", "restart_fence", "reconciled", "reconciled", "operator_resume"])

    def test_ready_reply_resume_requires_new_authorization_and_no_send_token(self):
        task = self.finished({"state": "needs_review", "agent_task_id": "old-agent"})
        self.queue.apply_reconciliation(task["id"], ready_proof(send_token_present=True))
        with self.assertRaisesRegex(QueueError, "resume_not_safe"):
            self.queue.resume(task["id"])
        self.queue.apply_reconciliation(task["id"], ready_proof())
        self.assertEqual(self.queue.resume(task["id"])["state"], "ready_to_publish")

    def test_first_capture_only_is_baseline_and_pause_restart_does_not_discard_new_notices(self):
        with self.assertRaisesRegex(QueueError, "capture_snapshot_not_ready"):
            self.queue.ensure_capture_baseline("fixture", ["old"])
        first = self.queue.ensure_capture_baseline("fixture", ["old-1", "old-2"], ready=True)
        self.assertTrue(first["created"])
        self.assertTrue(self.queue.has_seen_notice("fixture", "old-1"))
        restarted = AssistantQueue(self.path)
        self.assertFalse(restarted.ensure_capture_baseline("fixture", ["old-1", "pause-period-new"], ready=True)["created"])
        self.assertFalse(restarted.has_seen_notice("fixture", "pause-period-new"))
        restarted.remember_notice("fixture", "pause-period-new")
        self.assertTrue(restarted.has_seen_notice("fixture", "pause-period-new"))
        self.assertEqual(restarted.list_tasks()["total"], 0)

    def test_notice_received_with_task_is_atomic_and_scoped(self):
        task = self.queue.enqueue(binding(), envelope(), notice_fingerprint="new-notice")
        self.assertTrue(self.queue.has_seen_notice(binding().binding_id, "new-notice"))
        with self.assertRaisesRegex(QueueError, "notice_task_mismatch"):
            self.queue.remember_notice("other", "notice", task_id=task["id"])

    def test_public_projection_never_returns_tokens_or_private_binding_envelope(self):
        task = self.received()
        claim = self.queue.claim_next()
        self.queue.finish(task["id"], claim["execution_token"], "execute", {"state": "needs_review", "agent_task_id": "exists-but-not-done"})
        public = self.queue.public_task(self.queue.get_task(task["id"]), detail=True)
        serialized = json.dumps(public)
        for name in ("execution_token", "publication_token", "lease_until", "binding\"", "envelope\""):
            self.assertNotIn(name, serialized)
        self.assertEqual(public["stages"]["processing"], "not_confirmed")
        self.assertNotIn("A已收到", public["status_label"])

    def test_details_are_bounded_safe_and_only_returned_in_detail(self):
        details = {"reply_text": "虚构回复", "library_name": "虚构库", "sources": [{"title": "虚构资料", "time_range": "00:10-00:20"}],
                   "steps": [{"kind": "tool_call", "metadata": {"tool_name": "query_knowledge_base"}, "created_at": "fixture"}]}
        task = self.finished(ready_proof(details=details))
        self.assertEqual(self.queue.public_task(task, detail=True)["details"], details)
        self.assertNotIn("details", self.queue.list_tasks()["items"][0])
        with self.assertRaises(QueueError):
            self.queue.apply_reconciliation(task["id"], ready_proof(details={"reply_text": "x", "api_key": "fiction"}))

    def test_publication_rejects_isolated_input_before_browser_lane(self):
        task = self.finished(ready_proof(), source_mode="isolated")
        self.assertIsNone(self.queue.claim_next("publish"))
        self.assertEqual(self.queue.get_task(task["id"])["state"], "needs_review")

    def test_listing_filters_pages_and_literal_search(self):
        for index in range(3):
            self.queue.enqueue(binding(), envelope(command_id="list-" + str(index), reference_id="list-ref-" + str(index)), video_title="标题_" + str(index))
        result = self.queue.list_tasks(q="标题_", page=2, page_size=2)
        self.assertEqual((result["total"], len(result["items"])), (3, 1))
        self.assertEqual(self.queue.list_tasks(q="%missing")["total"], 0)

    def test_authorization_snapshot_is_fixed_scoped_and_internal_only(self):
        selected = binding()
        authorization = {"registry_id": "registry-fixture", "user_id": "user-fixture", "device_id": "device-fixture",
            "grant_id": "grant-fixture", "user_revision": 1, "device_revision": 1, "grant_revision": 1,
            "grant_version": 1, "binding_id": selected.binding_id, "instance_id": selected.instance_id,
            "knowledge_base_id": 7, "action": "query", "public_reply_allowed": True}
        task = self.queue.enqueue(selected, envelope(), user_id="user-fixture", device_id="device-fixture", authorization=authorization)
        self.assertEqual(AssistantQueue(self.path).get_task(task["id"])["authorization"], authorization)
        self.assertNotIn("authorization", self.queue.public_task(task, detail=True))
        with self.assertRaisesRegex(QueueError, "authorization_scope_mismatch"):
            self.queue.enqueue(selected, envelope(command_id="auth-2", reference_id="auth-ref-2"), authorization=authorization | {"knowledge_base_id": 8})
        for invalid in ([], {}, True, 7, None):
            with self.subTest(invalid=invalid), self.assertRaisesRegex(QueueError, "authorization_scope_mismatch"):
                self.queue.enqueue(selected, envelope(command_id="auth-invalid", reference_id="auth-invalid-ref"),
                                   authorization=authorization | {"action": invalid})
        with self.assertRaisesRegex(QueueError, "original_comment_conflict"):
            self.queue.enqueue(selected, envelope(command_id="auth-replay"), user_id="user-fixture", device_id="device-fixture",
                               authorization=authorization | {"grant_revision": 2})

    def test_remote_waiting_task_never_falls_back_to_local_execution_or_resume(self):
        remote = self.queue.enqueue(binding(), envelope(), initial_state="waiting_desktop", device_id="offline-device")
        self.assertIsNone(self.queue.claim_next())
        public = self.queue.public_task(remote, detail=True)
        self.assertEqual(public["stages"]["processing"], "not_started")
        self.assertFalse(public["actions"]["can_resume"])
        result = self.queue.apply_reconciliation(remote["id"], ready_proof())
        self.assertEqual(result["state"], "waiting_desktop")
        with self.assertRaisesRegex(QueueError, "resume_not_safe"):
            self.queue.resume(remote["id"])
        self.assertIsNone(self.queue.claim_next("publish"))

    def test_expired_poll_worker_cannot_apply_late_success(self):
        waiting = self.finished({"state": "waiting_ingestion", "import_state": "processing"})
        with patch("app.assistant.queue.time.time", return_value=waiting["updated_at"] + 11):
            poll = self.queue.claim_waiting()
        with patch("app.assistant.queue.time.time", return_value=poll["poll_until"] + 1):
            with self.assertRaisesRegex(QueueError, "stale_poll_claim"):
                self.queue.apply_reconciliation(waiting["id"], ready_proof(import_state="succeeded"), poll_token=poll["poll_token"])
        self.assertEqual(self.queue.get_task(waiting["id"])["state"], "waiting_ingestion")

    def test_terminal_public_reply_stage_overrides_old_ready_draft_state(self):
        task = self.finished(ready_proof())
        publication = self.queue.claim_next("publish")
        final = self.queue.finish(task["id"], publication["publication_token"], "publish", {"state": "b_view_visible"})
        self.assertEqual(final["reply_state"], "b_view_visible")
        self.assertEqual(self.queue.public_task(final)["stages"]["reply"], "b_view_visible")

    def test_publish_receipt_read_failure_only_returns_to_waiting_with_all_no_send_proof(self):
        task = self.finished(ready_proof(import_state="succeeded"))
        publication = self.queue.claim_next("publish")
        result = self.queue.finish(task["id"], publication["publication_token"], "publish", {
            "state": "waiting_ingestion", "send_token_present": False, "reply_state": "ready",
            "binding_active": True, "ingestion_submitted": True, "reason_code": "ingestion_status_unavailable"})
        self.assertEqual(result["state"], "waiting_ingestion")
        self.assertGreater(result["next_poll_at"], result["updated_at"])
        self.assertIsNone(self.queue.claim_next("publish"))

    def test_publish_waiting_without_no_send_proof_keeps_reconciliation_fence(self):
        task = self.finished(ready_proof())
        publication = self.queue.claim_next("publish")
        result = self.queue.finish(task["id"], publication["publication_token"], "publish", {
            "state": "waiting_ingestion", "send_token_present": True, "reply_state": "ready",
            "binding_active": True, "ingestion_submitted": True})
        self.assertEqual(result["state"], "needs_reconcile")
        self.assertIsNone(self.queue.claim_waiting())

    def test_private_completion_is_terminal_and_never_later_upgrades_to_publication(self):
        task = self.finished({"state": "completed_private", "agent_task_id": "private-agent", "reply_state": "ready"})
        self.assertIsNone(self.queue.claim_next("publish"))
        public = self.queue.public_task(task)
        self.assertEqual(public["status_label"], "已处理，未公开回复")
        self.assertEqual(public["stages"]["reply"], "not_public")
        self.assertFalse(public["actions"]["can_resume"])
        self.assertEqual(self.queue.apply_reconciliation(task["id"], ready_proof())["state"], "completed_private")
        with self.assertRaisesRegex(QueueError, "resume_not_safe"):
            self.queue.resume(task["id"])

    def test_preflight_verification_operator_resume_needs_explicit_proof_and_is_not_delivery_unknown(self):
        task = self.finished({"state": "needs_review", "reason_code": "preflight_verification_required"})
        self.queue.apply_reconciliation(task["id"], {"state": "needs_review", "reason_code": "preflight_verification_required",
            "reply_state": "needs_review", "send_token_present": False, "binding_active": True,
            "public_reply_allowed": True, "can_operator_resume": True})
        self.assertTrue(self.queue.public_task(self.queue.get_task(task["id"]))["actions"]["can_resume"])
        with self.assertRaisesRegex(QueueError, "resume_requires_operator_adapter"):
            self.queue.resume(task["id"])

    def test_waiting_private_import_can_complete_without_public_reply(self):
        task = self.finished({"state": "waiting_ingestion", "import_state": "processing"})
        final = self.queue.apply_reconciliation(task["id"], {"state": "completed_private", "import_state": "succeeded",
            "reply_state": "ready", "send_token_present": False, "public_reply_allowed": False})
        self.assertEqual(final["state"], "completed_private")
        self.assertEqual(self.queue.public_task(final)["stages"]["reply"], "not_public")
        self.assertIsNone(self.queue.claim_next("publish"))

    def test_intake_issues_are_verified_nonexecutable_and_idempotent(self):
        observed = envelope()
        evidence = {"real_mention": True, "sender_verified": True, "receiver_verified": True, "video_verified": True, "unique_count": 1}
        args = ("issue-fingerprint", observed["sender_profile_url"], observed["receiver_profile_url"], observed["comment_text"])
        issue = self.queue.record_intake_issue(*args, video_id=observed["video_id"], evidence=evidence)
        same = AssistantQueue(self.path).record_intake_issue(*args, video_id=observed["video_id"], evidence=evidence)
        self.assertEqual(issue, same)
        self.assertEqual(self.queue.intake_issue_summary()["unresolved_count"], 1)
        self.assertEqual(self.queue.list_tasks()["total"], 0)
        self.assertIsNone(self.queue.claim_next())
        self.assertNotIn("evidence", issue)
        with self.assertRaisesRegex(QueueError, "incomplete_intake_issue_evidence"):
            self.queue.record_intake_issue("unverified", *args[1:], video_id=observed["video_id"], evidence=evidence | {"sender_verified": False})
        self.assertEqual(self.queue.intake_issue_summary()["unresolved_count"], 1)

    def test_capture_can_remember_fixed_issue_reasons_but_never_raw_error_text(self):
        self.queue.remember_notice("receiver-scope", "unknown-sender", reason="sender_not_verified")
        self.queue.remember_notice("receiver-scope", "no-local-binding", reason="verified_local_binding_required")
        self.assertTrue(self.queue.has_seen_notice("receiver-scope", "unknown-sender"))
        self.assertTrue(self.queue.has_seen_notice("receiver-scope", "no-local-binding"))
        with self.assertRaisesRegex(QueueError, "invalid_notice_reason"):
            self.queue.remember_notice("receiver-scope", "untrusted-error", reason="private error and raw provider payload")
        self.assertEqual(self.queue.list_tasks()["total"], 0)
        self.assertIsNone(self.queue.claim_next())

    def test_issue_resolution_requires_fresh_same_comment_task_and_preserves_issue_audit(self):
        observed = envelope()
        issue = self.queue.record_intake_issue("old-issue", observed["sender_profile_url"], observed["receiver_profile_url"], observed["comment_text"],
            video_id=observed["video_id"], evidence={"real_mention": True, "sender_verified": True, "receiver_verified": True, "video_verified": True, "unique_count": 1})
        different = self.received(command_id="different-question", reference_id="different-question-ref", comment_text="另一个问题")
        with self.assertRaisesRegex(QueueError, "fresh_intake_required"):
            self.queue.resolve_intake_issue(issue["id"], different["id"])
        fresh = self.received(command_id="fresh-verified", reference_id="fresh-verified-ref")
        resolved = self.queue.resolve_intake_issue(issue["id"], fresh["id"])
        self.assertEqual(resolved["state"], "resolved")
        self.assertEqual(resolved["resolved_task_id"], fresh["id"])
        self.assertEqual(self.queue.resolve_intake_issue(issue["id"], fresh["id"]), resolved)
        self.assertEqual(self.queue.intake_issue_summary()["unresolved_count"], 0)
        self.assertEqual(self.queue.list_events(fresh["id"])[-1]["kind"], "resolved_intake_issue")

    def test_foreign_protected_and_hardlinked_paths_are_rejected_without_modification(self):
        foreign = self.root / "business.sqlite3"
        with closing(sqlite3.connect(foreign)) as db:
            db.execute("CREATE TABLE private_fixture(x)")
            db.commit()
        before = hashlib.sha256(foreign.read_bytes()).hexdigest()
        with self.assertRaisesRegex(QueueError, "foreign_queue_database"):
            AssistantQueue(foreign)
        with self.assertRaisesRegex(QueueError, "protected_queue_path"):
            AssistantQueue(foreign, protected_paths=(foreign,))
        linked = self.root / "alias.sqlite3"
        os.link(foreign, linked)
        with self.assertRaisesRegex(QueueError, "unsafe_queue_file"):
            AssistantQueue(linked)
        self.assertEqual(hashlib.sha256(foreign.read_bytes()).hexdigest(), before)

    def test_queue_new_main_rejects_orphan_journal_wal_or_shm_without_touching_them(self):
        for suffix in ("-journal", "-wal", "-shm"):
            with self.subTest(suffix=suffix):
                root = Path(tempfile.mkdtemp(prefix="dm-queue-orphan-preserved-")).resolve()
                path = root / "queue.sqlite3"
                sidecar = Path(str(path) + suffix)
                sidecar.write_bytes(b"synthetic preserved orphan")
                with self.assertRaisesRegex(QueueError, "orphan_sqlite_sidecar"):
                    AssistantQueue(path)
                self.assertFalse(path.exists())
                self.assertEqual(sidecar.read_bytes(), b"synthetic preserved orphan")

    def test_existing_empty_main_is_not_recreated_by_cached_or_new_queue(self):
        root = Path(tempfile.mkdtemp(prefix="dm-queue-empty-preserved-")).resolve()
        path = root / "queue.sqlite3"
        cached = AssistantQueue(path)
        path.write_bytes(b"")
        for operation in (cached.counts, lambda: AssistantQueue(path)):
            with self.assertRaisesRegex(QueueError, "foreign_queue_database"):
                operation()
            self.assertEqual(path.read_bytes(), b"")

    def test_cached_queue_cannot_request_initialization_owner_bypass(self):
        before = hashlib.sha256(self.path.read_bytes()).hexdigest()
        with self.assertRaisesRegex(QueueError, "queue_bootstrap_not_new"):
            with self.queue._connection(check_owner=False):
                self.fail("cached object used the new-file bootstrap bypass")
        self.assertEqual(hashlib.sha256(self.path.read_bytes()).hexdigest(), before)

    def test_vanished_zero_link_sidecar_is_not_mistaken_for_foreign_database(self):
        journal = Path(str(self.path) + "-journal")
        journal.write_bytes(b"synthetic expired journal")
        original = Path.lstat
        observed = []
        def deleting(path, *args, **kwargs):
            info = original(path, *args, **kwargs)
            if path == journal and not observed:
                observed.append(True)
                journal.unlink()
                return type("UnlinkedStat", (), {"st_mode": info.st_mode, "st_nlink": 0})()
            return info
        before = hashlib.sha256(self.path.read_bytes()).hexdigest()
        with patch.object(Path, "lstat", deleting):
            self.queue._guard()
        self.assertEqual(observed, [True])
        self.assertEqual(hashlib.sha256(self.path.read_bytes()).hexdigest(), before)

    def test_zero_link_retry_still_rejects_foreign_hardlinked_replacement_sidecar(self):
        foreign = self.root / "precious-fixture.sqlite3"
        with closing(sqlite3.connect(foreign)) as db:
            db.execute("CREATE TABLE precious_fixture(x)")
            db.commit()
        journal = Path(str(self.path) + "-journal")
        journal.write_bytes(b"synthetic transient journal")
        original = Path.lstat
        observed = []
        def replaced(path, *args, **kwargs):
            info = original(path, *args, **kwargs)
            if path == journal and not observed:
                observed.append(True)
                journal.unlink()
                os.link(foreign, journal)
                return type("UnlinkedStat", (), {"st_mode": info.st_mode, "st_nlink": 0})()
            return info
        before = hashlib.sha256(foreign.read_bytes()).hexdigest()
        with patch.object(Path, "lstat", replaced), self.assertRaisesRegex(QueueError, "unsafe_queue_file"):
            self.queue._guard()
        self.assertEqual(hashlib.sha256(foreign.read_bytes()).hexdigest(), before)


if __name__ == "__main__":
    unittest.main()
