"""Explicit recovery for replies that were never sent.

Covers only the new ``retry_unsent`` queue contract: safe pre-send states can be
re-processed once, while visible/unknown deliveries stay locked.
"""
from datetime import datetime, timezone
from pathlib import Path
import tempfile
import unittest

from app.assistant.queue import AssistantQueue, QueueError
from app.mention_agent.contracts import Binding


def binding():
    return Binding.from_dict({
        "binding_id": "retry-fixture", "instance_id": "retry-local",
        "sender_profile_url": "https://www.douyin.com/user/retry-A",
        "receiver_profile_url": "https://www.douyin.com/user/retry-B",
        "knowledge_base_id": 7, "public_reply_allowed": True, "active": True,
    })


def envelope(binding_value):
    return {
        "command_id": "retry-command", "reference_id": "retry-reference",
        "reference_quality": "verified_ui", "native_comment_id": None,
        "sender_profile_url": binding_value.sender_profile_url,
        "receiver_profile_url": binding_value.receiver_profile_url,
        "video_id": "7999999000000000011", "comment_text": "@虚构助手 加入知识库",
        "source_mode": "live_ui",
        "evidence": {"real_mention": True, "unique_count": 1, "sender_verified": True,
                     "video_verified": True,
                     "observed_at_utc": datetime.now(timezone.utc).isoformat()},
    }


class RetryUnsentTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.queue = AssistantQueue(Path(temporary.name).resolve() / "queue.sqlite3")
        self.binding = binding()
        self.task = self.queue.enqueue(self.binding, envelope(self.binding),
                                       video_title="测试视频", kind="clarify")

    def reach_publish(self, result):
        claimed = self.queue.claim_next("execute", run_id="retry-run")
        self.assertEqual(claimed["id"], self.task["id"])
        self.queue.finish(claimed["id"], claimed["execution_token"], "execute", {
            "state": "answer_ready", "reply_state": "ready", "agent_task_id": "agent-1",
            "details": {"reply_text": "你目前有1个知识库，请回复库名。"}})
        publish = self.queue.claim_next("publish", run_id="retry-run")
        self.assertEqual(publish["id"], self.task["id"])
        return self.queue.finish(publish["id"], publish["publication_token"], "publish", result)

    def test_verification_pause_is_retryable_and_resets_send_state(self):
        task = self.reach_publish({"state": "verification_required",
                                   "reply_state": "verification_required",
                                   "agent_task_id": "agent-1"})
        self.assertEqual(task["state"], "verification_required")
        self.assertTrue(self.queue.can_retry_unsent(task))
        self.assertTrue(self.queue.public_task(task)["actions"]["can_retry_unsent"])

        retried = self.queue.retry_unsent(task["id"])
        self.assertEqual(retried["state"], "queued")
        self.assertIsNone(retried["reason_code"])
        self.assertIsNone(retried["reply_state"])
        self.assertIsNone(retried["publication_token"])
        self.assertIsNone(retried["execution_token"])
        self.assertIsNone(retried["agent_task_id"])
        self.assertTrue(any(event["kind"] == "retry_unsent"
                            for event in self.queue.list_events(task["id"])))

    def test_original_lookup_limit_is_retryable(self):
        task = self.reach_publish({"state": "preflight_blocked",
                                   "reason_code": "original_notification_candidates_exceed_limit",
                                   "reply_state": "needs_review",
                                   "agent_task_id": "agent-1"})
        self.assertEqual(task["state"], "needs_review")
        self.assertEqual(task["reason_code"], "original_notification_candidates_exceed_limit")
        self.assertTrue(self.queue.can_retry_unsent(task))
        self.assertEqual(self.queue.retry_unsent(task["id"])["state"], "queued")

    def test_visible_or_unknown_delivery_is_never_retryable(self):
        for result in (
            {"state": "b_view_visible", "reply_state": "b_view_visible", "agent_task_id": "agent-1"},
            {"state": "delivery_unknown", "reply_state": "delivery_unknown", "agent_task_id": "agent-1"},
            {"state": "failed_no_submit", "reply_state": "failed_no_submit"},
        ):
            with self.subTest(state=result["state"]):
                self.setUp()
                task = self.reach_publish(result)
                self.assertFalse(self.queue.can_retry_unsent(task))
                with self.assertRaises(QueueError) as raised:
                    self.queue.retry_unsent(task["id"])
                self.assertEqual(str(raised.exception), "retry_unsent_not_safe")

    def test_other_review_reasons_are_not_retryable(self):
        task = self.reach_publish({"state": "preflight_blocked",
                                   "reason_code": "publication_paused",
                                   "reply_state": "needs_review"})
        self.assertFalse(self.queue.can_retry_unsent(task))
        with self.assertRaises(QueueError):
            self.queue.retry_unsent(task["id"])


    def test_dismiss_never_sent_marks_unsubmitted(self):
        task = self.reach_publish({"state": "preflight_blocked",
                                   "reason_code": "original_notification_candidates_exceed_limit",
                                   "reply_state": "needs_review"})
        self.assertTrue(self.queue.can_dismiss_unsent(task))
        closed = self.queue.dismiss_unsent(task["id"])
        self.assertEqual(closed["state"], "failed_no_submit")
        self.assertEqual(closed["reason_code"], "operator_confirmed_unsent")
        self.assertFalse(self.queue.can_dismiss_unsent(closed))
        self.assertFalse(self.queue.can_retry_unsent(closed))

    def test_dismiss_refuses_visible_reply(self):
        task = self.reach_publish({"state": "b_view_visible",
                                   "reply_state": "b_view_visible", "agent_task_id": "agent-1"})
        with self.assertRaises(QueueError):
            self.queue.dismiss_unsent(task["id"])

    def test_retry_is_limited_to_one_attempt(self):
        task = self.reach_publish({"state": "verification_required",
                                   "reply_state": "verification_required",
                                   "agent_task_id": "agent-1"})
        retried = self.queue.retry_unsent(task["id"])
        self.assertEqual(retried["state"], "queued")
        claimed = self.queue.claim_next("execute", run_id="retry-run-2")
        self.assertEqual(claimed["id"], task["id"])
        self.queue.finish(claimed["id"], claimed["execution_token"], "execute", {
            "state": "verification_required", "reply_state": "verification_required",
            "agent_task_id": "agent-2"})
        again = self.queue.get_task(task["id"])
        self.assertFalse(self.queue.can_retry_unsent(again))
        self.assertTrue(self.queue.can_dismiss_unsent(again))


    def test_readback_visible_marks_visible_without_resend(self):
        task = self.reach_publish({"state": "verification_required",
                                   "reply_state": "verification_required",
                                   "agent_task_id": "agent-1"})
        self.assertTrue(self.queue.can_readback_unsent(task))
        updated = self.queue.apply_unsent_readback(task["id"], {"outcome": "visible",
                                                                "counts": {"existing_matching_reply_count": 1}})
        self.assertEqual(updated["state"], "b_view_visible")
        self.assertEqual(updated["reason_code"], "operator_readback_visible")
        self.assertEqual(updated["reply_state"], "b_view_visible")
        self.assertFalse(self.queue.can_retry_unsent(updated))
        self.assertFalse(self.queue.can_dismiss_unsent(updated))

    def test_readback_not_visible_leaves_actionable_task(self):
        task = self.reach_publish({"state": "verification_required",
                                   "reply_state": "verification_required",
                                   "agent_task_id": "agent-1"})
        updated = self.queue.apply_unsent_readback(task["id"], {"outcome": "not_visible",
                                                                "counts": {"existing_matching_reply_count": 0}})
        self.assertEqual(updated["state"], "verification_required")
        self.assertEqual(updated["reason_code"], "readback_confirmed_unsent")
        self.assertTrue(self.queue.can_dismiss_unsent(updated))

    def test_readback_refuses_delivered_task(self):
        task = self.reach_publish({"state": "b_view_visible", "reply_state": "b_view_visible",
                                   "agent_task_id": "agent-1"})
        with self.assertRaises(QueueError):
            self.queue.apply_unsent_readback(task["id"], {"outcome": "visible"})
        with self.assertRaises(QueueError):
            self.queue.apply_unsent_readback(task["id"], {"outcome": "invalid"})


    def test_retry_after_readback_reopens_publish_phase(self):
        task = self.reach_publish({"state": "verification_required",
                                   "reply_state": "verification_required",
                                   "agent_task_id": "agent-1"})
        self.queue.apply_unsent_readback(task["id"], {"outcome": "not_visible",
                                                      "counts": {"existing_matching_reply_count": 0}})
        current = self.queue.get_task(task["id"])
        self.assertEqual(current["reason_code"], "readback_confirmed_unsent")
        self.assertTrue(self.queue.can_retry_after_readback(current))
        proof = self.queue.latest_readback_unsent(task["id"])
        self.assertIsNotNone(proof)
        self.assertEqual(proof["outcome"], "not_visible")
        retried = self.queue.retry_after_readback(task["id"])
        self.assertEqual(retried["state"], "ready_to_publish")
        self.assertEqual(retried["reply_state"], "ready")
        self.assertIsNone(retried["reason_code"])
        self.assertTrue(any(event["kind"] == "retry_after_readback"
                            for event in self.queue.list_events(task["id"])))

    def test_retry_after_readback_requires_confirmed_readback(self):
        task = self.reach_publish({"state": "verification_required",
                                   "reply_state": "verification_required",
                                   "agent_task_id": "agent-1"})
        self.assertFalse(self.queue.can_retry_after_readback(task))
        with self.assertRaises(QueueError):
            self.queue.retry_after_readback(task["id"])

    def test_retry_after_readback_is_one_time_only(self):
        task = self.reach_publish({"state": "verification_required",
                                   "reply_state": "verification_required",
                                   "agent_task_id": "agent-1"})
        self.queue.apply_unsent_readback(task["id"], {"outcome": "not_visible",
                                                      "counts": {"existing_matching_reply_count": 0}})
        self.queue.retry_after_readback(task["id"])
        # The reopen leads to a real publish attempt that is intercepted again.
        publish = self.queue.claim_next("publish", run_id="retry-after-readback")
        self.assertEqual(publish["id"], task["id"])
        self.queue.finish(publish["id"], publish["publication_token"], "publish", {
            "state": "verification_required", "reply_state": "verification_required",
            "agent_task_id": "agent-1"})
        # A fresh readback may set the reason again, but the one reopen is spent.
        self.queue.apply_unsent_readback(task["id"], {"outcome": "not_visible",
                                                      "counts": {"existing_matching_reply_count": 0}})
        current = self.queue.get_task(task["id"])
        self.assertEqual(current["reason_code"], "readback_confirmed_unsent")
        self.assertFalse(self.queue.can_retry_after_readback(current))
        with self.assertRaises(QueueError) as raised:
            self.queue.retry_after_readback(task["id"])
        self.assertEqual(str(raised.exception), "retry_after_readback_exhausted")

    def test_action_request_is_validated_and_queued(self):
        with self.assertRaises(QueueError) as raised:
            self.queue.request_action(self.task["id"], "retry_unsent")
        self.assertEqual(str(raised.exception), "retry_unsent_not_safe")

        task = self.reach_publish({"state": "verification_required",
                                   "reply_state": "verification_required",
                                   "agent_task_id": "agent-1"})
        action = self.queue.request_action(task["id"], "retry_unsent")
        self.assertEqual(action["action"], "retry_unsent")
        self.assertEqual(action["state"], "queued")
        taken = self.queue.take_action()
        self.assertEqual(taken["id"], action["id"])


if __name__ == "__main__":
    unittest.main()
