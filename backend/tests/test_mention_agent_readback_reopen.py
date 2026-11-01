"""Readback-confirmed reopen of a verification-held, never-sent reply."""
from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile
import time
import unittest

from app.mention_agent.store import AgentStore, AgentStoreError

SENDER = "https://www.douyin.com/user/sender-fixture"
RECEIVER = "https://www.douyin.com/user/receiver-fixture"


def utc(value=None):
    return datetime.fromtimestamp(time.time() if value is None else value, timezone.utc).isoformat()


def binding():
    return {"binding_id": "fixture-binding", "instance_id": "fixture-desktop",
            "sender_profile_url": SENDER, "receiver_profile_url": RECEIVER,
            "knowledge_base_id": 7, "public_reply_allowed": True, "active": True}


def envelope(**changes):
    return {"command_id": "fixture-command", "reference_id": "fixture-reference",
            "reference_quality": "verified_ui", "native_comment_id": None,
            "sender_profile_url": SENDER, "receiver_profile_url": RECEIVER,
            "video_id": "7999999000000000011", "comment_text": "@虚构助手 根据测试库回答问题",
            "source_mode": "live_ui", "evidence": {"real_mention": True, "unique_count": 1,
                "sender_verified": True, "video_verified": True, "observed_at_utc": utc()}, **changes}


def proposal():
    return {"text": "仅供隔离验证的回答", "source_ids": ["fixture-source"], "kind": "answer",
            "sources": [{"id": "fixture-source", "title": "虚构来源"}]}


def verification():
    value = envelope()
    return {key: value[key] for key in ("sender_profile_url", "receiver_profile_url", "video_id",
                                        "comment_text", "reference_id")} | {
        "unique_count": 1, "real_mention": True, "current_receiver_verified": True,
        "observed_at_utc": utc()}


def proof(observed=None, count=0):
    return {"outcome": "not_visible",
            "counts": {"matching_reply_count": count, "existing_matching_reply_count": count},
            "observed_at": time.time() if observed is None else observed}


class ReadbackReopenTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.store = AgentStore(self.root / "agent.sqlite3")
        self.store.put_binding(binding())
        task = self.store.enqueue("fixture-binding", envelope())
        claim = self.store.claim(task["task_id"])
        self.store.complete(claim["task_id"], claim["claim_token"], proposal())
        reservation = self.store.reserve_send(claim["task_id"], verification())
        self.task_id = claim["task_id"]
        self.store.finish_send(self.task_id, reservation["send_token"],
                               "verification_required", {"reason_code": "human_verification_required"})

    def test_reopen_restores_answer_ready_ready_and_drops_token(self):
        reply = self.store.reopen_after_readback(self.task_id, proof())
        self.assertEqual(reply["state"], "ready")
        self.assertIsNone(reply["send_token"])
        self.assertEqual(self.store.get_task(self.task_id)["state"], "answer_ready")

    def test_reopen_is_allowed_only_once(self):
        import sqlite3
        self.store.reopen_after_readback(self.task_id, proof())
        # Defense in depth: even if the state were put back, the audit table
        # refuses a second operator reopen for the same agent task.
        db = sqlite3.connect(self.store.path)
        try:
            db.execute("UPDATE agent_tasks SET state='verification_required' WHERE task_id=?", (self.task_id,))
            db.execute("UPDATE agent_replies SET state='verification_required' WHERE task_id=?", (self.task_id,))
            db.commit()
        finally:
            db.close()
        with self.assertRaises(AgentStoreError) as raised:
            self.store.reopen_after_readback(self.task_id, proof())
        self.assertEqual(str(raised.exception), "readback_reopen_limit_reached")

    def test_stale_or_invisible_proof_is_refused(self):
        with self.assertRaises(AgentStoreError) as raised:
            self.store.reopen_after_readback(self.task_id, proof(observed=time.time() - 3600))
        self.assertEqual(str(raised.exception), "stale_readback_proof")

    def test_visible_proof_is_refused(self):
        visible = proof()
        visible["outcome"] = "visible"
        with self.assertRaises(AgentStoreError):
            self.store.reopen_after_readback(self.task_id, visible)

    def test_wrong_state_is_refused(self):
        task = self.store.enqueue("fixture-binding", envelope(command_id="fresh-command",
                                                               reference_id="fresh-reference",
                                                               video_id="7999999000000000022"))
        with self.assertRaises(AgentStoreError) as raised:
            self.store.reopen_after_readback(task["task_id"], proof())
        self.assertEqual(str(raised.exception), "reply_not_readback_reopenable")


if __name__ == "__main__":
    unittest.main()
