"""Public status regressions: in-process TestClient + temporary queue only.

No real backend, browser, account, model, registry, or business database is used.
"""
from datetime import datetime, timezone
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.assistant import routes
from app.assistant.queue import AssistantQueue, QueueError
from app.assistant.runtime import AssistantRuntime
from app.mention_agent.contracts import Binding


class PublicStatusTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name).resolve() / "queue.sqlite3"
        self.queue = AssistantQueue(self.path)
        self.control = SimpleNamespace(snapshot=lambda: {"state": "stopped", "desired_state": "stopped"})
        self.service = routes.AssistantAPI(self.queue, self.control)
        app = FastAPI()
        app.include_router(routes.router, prefix="/api")
        app.dependency_overrides[routes.get_service] = lambda: self.service
        self.client = TestClient(app, base_url="http://127.0.0.1:8000", client=("127.0.0.1", 50001))
        self.addCleanup(self.client.close)

    def task(self):
        binding = Binding.from_dict({"binding_id": "status-fixture", "instance_id": "status-local",
            "sender_profile_url": "https://www.douyin.com/user/status-A", "receiver_profile_url": "https://www.douyin.com/user/status-B",
            "knowledge_base_id": 7, "public_reply_allowed": True, "active": True})
        envelope = {"command_id": "status-command", "reference_id": "status-reference", "reference_quality": "verified_ui", "native_comment_id": None,
            "sender_profile_url": binding.sender_profile_url, "receiver_profile_url": binding.receiver_profile_url,
            "video_id": "7999999000000000009", "comment_text": "@虚构助手 测试状态", "source_mode": "live_ui",
            "evidence": {"real_mention": True, "unique_count": 1, "sender_verified": True, "video_verified": True,
                         "observed_at_utc": datetime.now(timezone.utc).isoformat()}}
        return self.queue.enqueue(binding, envelope)

    def test_unavailable_existing_configuration_is_not_first_use(self):
        def broken():
            raise QueueError("registry_required_marker_missing")
        self.service.binding_provider = broken
        value = self.client.get("/api/assistant/overview").json()
        self.assertIsNone(value["binding"])
        self.assertEqual(value["configuration"]["state"], "unavailable")
        self.assertEqual(value["configuration"]["reason_code"], "registry_required_marker_missing")
        self.assertIn("恢复原授权状态", value["configuration"]["message"])
        self.assertFalse(value["capabilities"]["start"])
        self.assertTrue(value["capabilities"]["stop"])

    def test_unknown_provider_exception_never_projects_exception_body_or_code(self):
        secret = "SYNTHETIC_PRIVATE_PROVIDER_DETAIL"
        self.service.binding_provider = lambda: (_ for _ in ()).throw(RuntimeError(secret))
        response = self.client.get("/api/assistant/overview")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["configuration"]["reason_code"], "assistant_configuration_unavailable")
        self.assertNotIn(secret, response.text)

    def test_unconfigured_and_readable_configuration_remain_distinct(self):
        self.assertEqual(self.service.overview()["configuration"]["state"], "not_configured")
        self.service.binding_provider = lambda: {"binding_id": "public-fixture", "knowledge_base_id": 7, "credentials": "PRIVATE"}
        value = self.service.overview()
        self.assertEqual(value["configuration"]["state"], "ready")
        self.assertNotIn("credentials", value["binding"])

    def test_accepted_action_progress_is_readable_without_claiming_or_executing(self):
        task = self.task()
        url = "/api/assistant/tasks/" + task["id"]
        accepted = self.client.post(url + "/reconcile", json={}).json()
        self.assertTrue(accepted["accepted"])
        before = self.queue.latest_action(task["id"])
        value = self.client.get(url).json()
        self.assertEqual(value["latest_action"]["id"], accepted["action_id"])
        self.assertEqual(value["latest_action"]["state"], "queued")
        self.assertEqual(self.queue.latest_action(task["id"]), before)
        self.assertEqual(value["state"], "queued")

    def test_failed_async_action_survives_reopen_and_does_not_claim_task_success(self):
        task = self.task()
        url = "/api/assistant/tasks/" + task["id"]
        self.client.post(url + "/reconcile", json={})
        def unavailable(item):
            raise RuntimeError("SYNTHETIC_PRIVATE_FAILURE")
        runtime = AssistantRuntime(self.queue, self.control, run_id="status-run", reconcile=unavailable)
        result = runtime.process_action_once()
        self.assertEqual(result["state"], "failed")
        self.service.queue = AssistantQueue(self.path)
        response = self.client.get(url)
        value = response.json()
        self.assertEqual(value["state"], "queued")
        self.assertEqual(value["latest_action"]["state"], "failed")
        self.assertEqual(value["latest_action"]["reason_code"], "reconciliation_unavailable")
        self.assertIn("暂时无法核对", value["latest_action"]["message"])
        self.assertNotIn("SYNTHETIC_PRIVATE_FAILURE", response.text)
        self.assertNotIn("execution_token", response.text)

    def test_projection_has_no_raw_tokens_or_unknown_reason_text(self):
        raw = {"id": "public-action", "task_id": "task", "action": "resume", "state": "failed",
               "reason_code": "SYNTHETIC_PRIVATE_CODE", "send_token": "SYNTHETIC_PRIVATE_TOKEN",
               "claim_token": "SYNTHETIC_PRIVATE_CLAIM", "created_at": 1.0, "updated_at": 2.0}
        value = routes.public_action(raw)
        self.assertEqual(set(value), {"id", "action", "state", "reason_code", "message", "created_at", "updated_at"})
        self.assertEqual(value["reason_code"], "action_unavailable")
        self.assertNotIn("SYNTHETIC_PRIVATE", str(value))
        self.assertIn("1970-01-01", value["created_at"])

    def test_done_action_message_is_not_a_publication_receipt(self):
        value = routes.public_action({"id": "action", "action": "resume", "state": "done", "reason_code": None})
        self.assertIn("后续阶段", value["message"])
        self.assertNotIn("发送成功", value["message"])


if __name__ == "__main__":
    unittest.main()
