"""Orchestration for enabling/disabling the opt-in native intake channel."""
from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from app.assistant.native_flow import NativeIntakeFlow, _failure_message


class FakeControl:
    def __init__(self):
        self.stopped = 0
        self.started = 0
        self.state = "running"

    def snapshot(self):
        return {"state": "stopped", "process_alive": False}

    def stop(self):
        self.stopped += 1
        self.state = "stopped"

    def start(self):
        self.started += 1
        self.state = "starting"


class FakeProcess:
    def __init__(self, result_path, result, *, timeout=False):
        self.result_path = Path(result_path)
        self.result = result
        self.timeout = timeout
        self.killed = False

    def wait(self, timeout=None):
        if self.timeout:
            raise TimeoutError()
        if self.result is not None:
            self.result_path.parent.mkdir(parents=True, exist_ok=True)
            self.result_path.write_text(json.dumps(self.result), encoding="utf-8")
        return 0

    def kill(self):
        self.killed = True


class FlowTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.result_path = self.root / "native-provision-result.json"
        self.control = FakeControl()
        self.sent = []

    def make_flow(self, result, *, timeout=False):
        def spawn(command, **kwargs):
            self.sent.append(command)
            return FakeProcess(self.result_path, result, timeout=timeout)

        return NativeIntakeFlow(self.control, backend=self.root, result_path=self.result_path,
                                spawn=spawn)

    def test_enable_success_restarts_assistant(self):
        flow = self.make_flow({"ok": True, "enabled": True})
        stage = flow.run_once("enable")
        self.assertEqual(stage, "enabled")
        self.assertEqual(self.control.stopped, 1)
        self.assertEqual(self.control.started, 1)
        self.assertIn("--action", self.sent[0])
        self.assertIn("enable", self.sent[0])
        status = flow.status()
        self.assertEqual(status["stage"], "enabled")
        self.assertTrue(status["last_result"]["ok"])

    def test_enable_identity_mismatch_reports_actionable_message(self):
        flow = self.make_flow({"ok": False, "code": "native_provision_identity_changed"})
        stage = flow.run_once("enable")
        self.assertEqual(stage, "provision_failed")
        self.assertEqual(flow.status()["last_result"]["code"], "native_provision_identity_changed")
        self.assertIn("未覆盖原配置", flow.status()["message"])
        # The assistant must still be restarted on the normal-page lane.
        self.assertEqual(self.control.started, 1)

    def test_provision_timeout_restarts_assistant(self):
        flow = self.make_flow(None, timeout=True)
        stage = flow.run_once("enable")
        self.assertEqual(stage, "provision_timeout")
        self.assertEqual(self.control.started, 1)

    def test_disable_success(self):
        flow = self.make_flow({"ok": True, "enabled": False})
        stage = flow.run_once("disable")
        self.assertEqual(stage, "disabled")
        self.assertIn("disable", self.sent[0])
        self.assertEqual(self.control.started, 1)

    def test_second_start_is_rejected_while_running(self):
        busy = self.make_flow({"ok": True})
        busy._thread = type("Thread", (), {"is_alive": lambda self: True})()
        self.assertFalse(busy.start_enable())

    def test_failure_messages_are_fixed_not_raw(self):
        message = _failure_message("enable", "some_unexpected_raw_error")
        self.assertIn("原网页收件保持不变", message)
        self.assertNotIn("some_unexpected_raw_error", message)


if __name__ == "__main__":
    unittest.main()
