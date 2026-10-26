"""Isolated scheduling/control/API tests. No real process, network or model."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.assistant.control import AssistantControl
from app.assistant.queue import AssistantQueue, QueueError
from app.assistant.runtime import AssistantRuntime
from app.assistant import routes
from tests.test_assistant_queue import binding, envelope, ready_proof


class AssistantRuntimeControlTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.queue = AssistantQueue(self.root / "queue.sqlite3")
        self.alive = set()
        self.spawns = []
        def fake_popen(command, **kwargs):
            pid = 90000 + len(self.spawns)
            self.spawns.append((list(command), kwargs, pid))
            self.alive.add(pid)
            return SimpleNamespace(pid=pid)
        self.control = AssistantControl(self.queue, popen_factory=fake_popen, process_probe=lambda pid: pid in self.alive)

    def running(self):
        value = self.control.start()
        self.control.register(value["run_id"], value["pid"])
        return value["run_id"]

    def task(self, **changes):
        return self.queue.enqueue(binding(), envelope(**changes))

    def test_constructor_never_launches_and_start_is_fixed_hidden_once(self):
        self.assertEqual(self.spawns, [])
        first = self.control.start()
        second = self.control.start()
        self.assertEqual(first["run_id"], second["run_id"])
        self.assertEqual(len(self.spawns), 1)
        command, kwargs, pid = self.spawns[0]
        self.assertEqual(command, [sys.executable, "-m", "app.assistant", "--state-dir", str(self.root), "--run-registered"])
        self.assertIs(kwargs["shell"], False)
        self.assertEqual(kwargs["stdout"], subprocess.DEVNULL)
        self.assertEqual(kwargs["stderr"], subprocess.DEVNULL)
        self.assertEqual(kwargs["creationflags"], getattr(subprocess, "CREATE_NO_WINDOW", 0))

    def test_child_actual_pid_registration_is_not_overwritten_by_launcher(self):
        def register_during_launch(command, **kwargs):
            pending = self.queue.get_control()
            self.alive.update({91001, 91002})
            self.control.register(pending["run_id"], 91002)
            return SimpleNamespace(pid=91001)
        self.control.popen_factory = register_during_launch
        result = self.control.start()
        self.assertEqual(result["pid"], 91002)

    def test_stop_only_creates_current_run_file_and_never_kills_or_restarts_live_owner(self):
        run_id = self.running()
        original_pid = self.control.snapshot()["pid"]
        stopped = self.control.stop()
        path = self.control.stop_path(run_id)
        self.assertTrue(path.is_file())
        self.assertEqual(json.loads(path.read_text())["run_id"], run_id)
        self.assertEqual(stopped["state"], "stopping")
        self.assertIn(original_pid, self.alive)
        self.assertTrue(self.control.should_stop(run_id))
        with self.assertRaisesRegex(QueueError, "runtime_stopping"):
            self.control.start()
        with self.assertRaisesRegex(QueueError, "runtime_stopping"):
            self.control.pause()
        self.assertEqual(len(self.spawns), 1)
        self.alive.remove(original_pid)
        replacement = self.control.start()
        self.assertNotEqual(replacement["run_id"], run_id)
        self.assertFalse(self.control.should_stop(replacement["run_id"]))
        self.assertTrue(path.is_file())

    def test_unknown_launch_is_not_blindly_repeated(self):
        self.control.popen_factory = lambda *args, **kwargs: (_ for _ in ()).throw(OSError("fictional raw secret"))
        with self.assertRaisesRegex(QueueError, "launch_result_unknown"):
            self.control.start()
        with self.assertRaisesRegex(QueueError, "launch_pending_requires_review"):
            self.control.start()
        self.assertNotIn("fictional raw secret", json.dumps(self.control.snapshot()))

    def test_start_after_confirmed_dead_owner_fences_interrupted_tasks(self):
        old_run = self.running()
        task = self.task()
        self.queue.claim_next(run_id=old_run)
        self.alive.clear()
        self.assertEqual(self.control.snapshot()["state"], "interrupted")
        new = self.control.start()
        self.assertNotEqual(old_run, new["run_id"])
        self.assertEqual(self.queue.get_task(task["id"])["state"], "needs_reconcile")

    def test_process_registration_does_not_claim_receiver_ready_and_heartbeat_keeps_verification(self):
        run_id = self.running()
        self.assertEqual(self.control.snapshot()["receiver_state"], "starting")
        self.control.set_receiver_health(run_id, "verification_required", "human_verification_required")
        self.control.pause()
        self.control.heartbeat(run_id)
        value = self.control.snapshot()
        self.assertEqual(value["receiver_state"], "verification_required")
        self.assertEqual(value["reason_code"], "human_verification_required")
        self.assertEqual(value["desired_state"], "paused")
        self.control.set_receiver_health(run_id, "ready")
        self.assertIsNone(self.control.snapshot()["reason_code"])

    def test_worker_error_is_independent_of_receiver_health_and_heartbeat(self):
        run_id = self.running()
        self.control.set_worker_health(run_id, "error", "worker_iteration_failed")
        self.control.set_receiver_health(run_id, "ready")
        self.control.heartbeat(run_id)
        current = self.control.snapshot()
        self.assertEqual(current["worker_state"], "error")
        self.assertEqual(current["worker_reason"], "worker_iteration_failed")
        self.assertEqual(current["worker_error_count"], 1)
        self.control.set_worker_health(run_id, "idle")
        recovered = self.control.snapshot()
        self.assertEqual(recovered["worker_state"], "idle")
        self.assertIsNone(recovered["worker_reason"])
        self.assertEqual(recovered["worker_error_count"], 1)
        with self.assertRaisesRegex(QueueError, "runtime_owner_changed"):
            self.control.set_worker_health("unrelated-run", "error", "worker_iteration_failed")

    def test_capture_observation_counts_only_explicit_batches_and_resumes_same_run(self):
        run_id = self.running()
        self.assertEqual(self.control.snapshot()["capture_cycles"], 0)
        self.assertIsNone(self.control.snapshot()["last_capture_at"])
        first = self.control.record_capture(run_id, admitted_count=0, baseline_created=True)
        self.assertEqual(first["capture_cycles"], 1)
        self.assertEqual(datetime.fromisoformat(first["last_capture_at"]).utcoffset(), timedelta(0))
        self.control.heartbeat(run_id)
        self.control.set_receiver_health(run_id, "ready")
        self.control.snapshot()
        self.assertEqual(self.control.snapshot()["capture_cycles"], 1)
        self.control.pause()
        resumed = self.control.start()
        self.assertEqual(resumed["run_id"], run_id)
        self.assertEqual(resumed["capture_cycles"], 1)
        latest = self.control.record_capture(run_id, admitted_count=3, baseline_created=False)
        self.assertEqual(latest["capture_cycles"], 2)
        self.assertEqual(latest["last_capture_admitted_count"], 3)
        self.assertIs(latest["last_capture_baseline_created"], False)
        self.alive.clear()
        fresh = self.control.start()
        self.assertNotEqual(fresh["run_id"], run_id)
        self.assertEqual(fresh["capture_cycles"], 0)
        for name in ("last_capture_at", "last_capture_admitted_count", "last_capture_baseline_created"):
            self.assertIsNone(fresh[name])
        with self.assertRaisesRegex(QueueError, "runtime_owner_changed"):
            self.control.record_capture(run_id, admitted_count=1, baseline_created=False)

    def test_capture_observation_strict_inputs_cannot_mutate_control(self):
        run_id = self.running()
        before = self.queue.get_control()
        for changes in ({"admitted_count": True}, {"admitted_count": -1}, {"admitted_count": 4},
                {"admitted_count": 1.0}, {"admitted_count": []}, {"baseline_created": 1},
                {"baseline_created": "true"}, {"run_id": []}, {"run_id": "wrong-run"}):
            values = {"run_id": run_id, "admitted_count": 0, "baseline_created": False} | changes
            with self.subTest(changes=changes), self.assertRaises(QueueError):
                self.control.record_capture(**values)
        self.assertEqual(self.queue.get_control(), before)

    def test_browser_lane_rejects_foreign_thread_and_worker_only_executes(self):
        run_id = self.running()
        task = self.task()
        calls = []
        runtime = AssistantRuntime(self.queue, self.control, run_id=run_id,
            execute=lambda item: calls.append(("execute", threading.get_ident())) or ready_proof(),
            publish=lambda item: calls.append(("publish", threading.get_ident())) or {"state": "b_view_visible"},
            intake=lambda: [])
        owner = threading.get_ident()
        with ThreadPoolExecutor(max_workers=1) as pool:
            result = pool.submit(runtime.execute_once).result(timeout=3)
            self.assertEqual(result["id"], task["id"])
            with self.assertRaisesRegex(QueueError, "browser_lane_wrong_thread"):
                pool.submit(runtime.publish_once).result(timeout=3)
            with self.assertRaisesRegex(QueueError, "browser_lane_wrong_thread"):
                pool.submit(runtime.receive_once).result(timeout=3)
        self.assertNotEqual(calls[0][1], owner)
        self.assertEqual(runtime.publish_once()["state"], "b_view_visible")
        self.assertEqual(calls[-1], ("publish", owner))

    def test_intake_still_persists_new_message_during_blocked_execution(self):
        run_id = self.running()
        first = self.task()
        entered, release = threading.Event(), threading.Event()
        def execute(item):
            entered.set()
            self.assertTrue(release.wait(timeout=3))
            return {"state": "waiting_ingestion", "agent_task_id": "pending-import", "import_state": "processing"}
        runtime = AssistantRuntime(self.queue, self.control, run_id=run_id, execute=execute,
            intake=lambda: [{"binding": binding(), "envelope": envelope(command_id="during-work", reference_id="during-work-ref")}])
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(runtime.execute_once)
            self.assertTrue(entered.wait(timeout=3))
            received = runtime.receive_once()
            self.assertEqual(len(received), 1)
            self.assertEqual(self.queue.get_task(received[0]["id"])["state"], "queued")
            release.set()
            self.assertEqual(future.result(timeout=3)["state"], "waiting_ingestion")
        self.assertEqual(self.queue.get_task(first["id"])["state"], "waiting_ingestion")

    def test_pause_captures_but_never_executes_or_publishes(self):
        run_id = self.running()
        self.control.pause()
        calls = []
        runtime = AssistantRuntime(self.queue, self.control, run_id=run_id,
            intake=lambda: [{"binding": binding(), "envelope": envelope()}],
            execute=lambda task: calls.append("execute"), publish=lambda task: calls.append("publish"))
        self.assertEqual(len(runtime.receive_once()), 1)
        self.assertIsNone(runtime.execute_once())
        self.assertIsNone(runtime.publish_once())
        self.assertEqual(calls, [])

    def test_execution_error_is_fixed_and_restart_does_not_call_paid_adapter_again(self):
        run_id = self.running()
        task = self.task()
        calls = []
        def fail(item):
            calls.append(item["id"])
            raise RuntimeError("fictional api_key=DO-NOT-PERSIST")
        runtime = AssistantRuntime(self.queue, self.control, run_id=run_id, execute=fail)
        result = runtime.execute_once()
        self.assertEqual(result["state"], "needs_reconcile")
        self.assertIsNone(runtime.execute_once())
        self.assertEqual(calls, [task["id"]])
        self.assertNotIn("DO-NOT-PERSIST", self.queue.path.read_bytes().decode(errors="ignore"))

    def test_waiting_import_poll_has_no_execute_or_publish_call_until_verified(self):
        run_id = self.running()
        task = self.task()
        execute_calls, publish_calls = [], []
        runtime = AssistantRuntime(self.queue, self.control, run_id=run_id,
            execute=lambda item: execute_calls.append(item["id"]) or {"state": "waiting_ingestion", "import_state": "processing"},
            publish=lambda item: publish_calls.append(item["id"]) or {"state": "b_view_visible"},
            reconcile=lambda item: ready_proof(import_state="succeeded"))
        waiting = runtime.execute_once()
        self.assertIsNone(runtime.publish_once())
        with patch("app.assistant.queue.time.time", return_value=waiting["updated_at"] + 11):
            polled = runtime.poll_waiting_once()
        self.assertEqual(polled["state"], "ready_to_publish")
        self.assertEqual(execute_calls, [task["id"]])
        self.assertEqual(publish_calls, [])
        self.assertEqual(runtime.publish_once()["state"], "b_view_visible")
        self.assertEqual(publish_calls, [task["id"]])

    def test_resume_never_trusts_http_proof_and_reconciles_just_before_resume(self):
        run_id = self.running()
        task = self.task()
        claim = self.queue.claim_next(run_id=run_id)
        self.queue.finish(task["id"], claim["execution_token"], "execute", {"state": "needs_review"})
        runtime = AssistantRuntime(self.queue, self.control, run_id=run_id, reconcile=lambda item: ready_proof(send_token_present=True))
        with self.assertRaisesRegex(QueueError, "resume_not_safe"):
            runtime.resume_task(task["id"])
        self.assertEqual(self.queue.get_task(task["id"])["state"], "needs_review")

    def test_operator_hold_release_adapter_is_only_called_by_explicit_safe_resume(self):
        run_id = self.running()
        task = self.task()
        claim = self.queue.claim_next(run_id=run_id)
        self.queue.finish(task["id"], claim["execution_token"], "execute", {"state": "needs_review", "reason_code": "preflight_failed"})
        held = {"value": True}
        releases = []
        def reconcile(item):
            if held["value"]:
                return {"state": "needs_review", "reason_code": "preflight_failed", "reply_state": "needs_review",
                        "can_operator_resume": True, "send_token_present": False, "binding_active": True, "public_reply_allowed": True}
            return ready_proof()
        def operator_resume(item):
            releases.append(item["id"])
            held["value"] = False
        runtime = AssistantRuntime(self.queue, self.control, run_id=run_id, reconcile=reconcile, resume_adapter=operator_resume)
        checked = runtime.reconcile_task(task["id"])
        self.assertTrue(self.queue.public_task(checked)["actions"]["can_resume"])
        self.assertEqual(releases, [])
        self.assertIsNone(runtime.poll_waiting_once())
        with self.assertRaisesRegex(QueueError, "resume_requires_operator_adapter"):
            self.queue.resume(task["id"])
        resumed = runtime.resume_task(task["id"])
        self.assertEqual(resumed["state"], "ready_to_publish")
        self.assertEqual(releases, [task["id"]])
        with self.assertRaisesRegex(QueueError, "resume_not_safe"):
            runtime.resume_task(task["id"])
        self.assertEqual(releases, [task["id"]])

    def test_terminal_unknown_never_calls_operator_resume_adapter(self):
        run_id = self.running()
        task = self.task()
        claim = self.queue.claim_next(run_id=run_id)
        self.queue.finish(task["id"], claim["execution_token"], "execute", {"state": "delivery_unknown", "send_token_present": True})
        releases = []
        runtime = AssistantRuntime(self.queue, self.control, run_id=run_id, reconcile=lambda item: ready_proof(),
                                   resume_adapter=lambda item: releases.append(item["id"]))
        with self.assertRaisesRegex(QueueError, "resume_not_safe"):
            runtime.resume_task(task["id"])
        self.assertEqual(releases, [])


class AssistantAPITests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.queue = AssistantQueue(self.root / "queue.sqlite3")
        self.spawns = []
        def popen(command, **kwargs):
            self.spawns.append(command)
            return SimpleNamespace(pid=98765)
        self.control = AssistantControl(self.queue, popen_factory=popen, process_probe=lambda pid: pid == 98765)
        self.service = routes.AssistantAPI(self.queue, self.control)
        self.app = FastAPI()
        self.app.include_router(routes.router, prefix="/api")
        self.app.dependency_overrides[routes.get_service] = lambda: self.service
        self.client = TestClient(self.app, base_url="http://127.0.0.1:8000", client=("127.0.0.1", 50000))
        self.addCleanup(self.client.close)

    def test_overview_and_filtered_tasks_are_local_safe_public_schema(self):
        task = self.queue.enqueue(binding(), envelope())
        self.queue.claim_next()
        overview = self.client.get("/api/assistant/overview").json()
        self.assertEqual(overview["counts"]["executing"], 1)
        self.assertEqual(overview["capabilities"]["public_visibility"], "b_view_only")
        response = self.client.get("/api/assistant/tasks", params={"page_size": 1, "q": "测试库"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["total"], 1)
        detail = self.client.get("/api/assistant/tasks/" + task["id"]).json()
        for key in ("execution_token", "lease_until", "envelope", "binding"):
            self.assertNotIn(key, detail)
        self.assertIn("timeline", detail)

    def test_control_accepts_only_fixed_json_action_and_no_arbitrary_command(self):
        rejected = self.client.post("/api/assistant/start", json={"cmd": "untrusted", "path": "C:/elsewhere"})
        self.assertEqual(rejected.status_code, 422)
        self.assertEqual(self.spawns, [])
        accepted = self.client.post("/api/assistant/start", json={}, headers={"Origin": "http://localhost:5173"})
        self.assertEqual(accepted.status_code, 200)
        self.assertTrue(accepted.json()["success"])
        self.assertEqual(len(self.spawns), 1)
        self.assertEqual(self.client.post("/api/assistant/stop", json={}).status_code, 200)

    def test_external_origin_null_origin_forms_and_rebinding_host_are_denied(self):
        for origin in ("null", "https://evil.invalid", "http://localhost.evil.invalid:5173", "http://127.0.0.1:9"):
            with self.subTest(origin=origin):
                self.assertEqual(self.client.post("/api/assistant/start", json={}, headers={"Origin": origin}).status_code, 403)
        self.assertEqual(self.client.post("/api/assistant/start", content="action=start").status_code, 415)
        self.assertEqual(self.client.get("/api/assistant/overview", headers={"Host": "evil.invalid"}).status_code, 403)
        with TestClient(self.app, base_url="http://127.0.0.1:8000", client=("198.51.100.42", 50000)) as remote:
            self.assertEqual(remote.get("/api/assistant/overview").status_code, 403)
        self.assertEqual(self.spawns, [])

    def test_action_requests_are_durable_idempotent_and_resume_cannot_supply_fake_proof(self):
        task = self.queue.enqueue(binding(), envelope())
        url = "/api/assistant/tasks/" + task["id"]
        first = self.client.post(url + "/reconcile", json={})
        second = self.client.post(url + "/reconcile", json={})
        self.assertEqual(first.status_code, 200)
        self.assertTrue(first.json()["accepted"])
        self.assertEqual(first.json()["action_id"], second.json()["action_id"])
        self.assertEqual(self.client.post(url + "/resume", json={"safe_to_publish": True}).status_code, 422)
        self.assertEqual(self.client.post(url + "/resume", json={}).status_code, 409)

    def test_missing_task_and_provider_failure_return_fixed_error_not_raw_exception(self):
        self.assertEqual(self.client.get("/api/assistant/tasks/missing").status_code, 404)
        self.service.binding_provider = lambda: (_ for _ in ()).throw(RuntimeError("fiction-only-secret"))
        response = self.client.get("/api/assistant/overview")
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.json()["binding"])
        self.assertNotIn("fiction-only-secret", response.text)

    def test_start_guard_runs_before_creating_starting_state_or_spawning_process(self):
        self.service.start_guard = lambda: False
        response = self.client.post("/api/assistant/start", json={})
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["detail"]["code"], "assistant_not_configured")
        self.assertEqual(self.spawns, [])
        self.assertEqual(self.queue.get_control()["state"], "stopped")
        self.assertIsNone(self.queue.get_control()["run_id"])


if __name__ == "__main__":
    unittest.main()
