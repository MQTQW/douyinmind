"""Lifecycle tests use temporary directories only; no browser or app database."""
from __future__ import annotations

from datetime import datetime, timedelta
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from app.mention_receiver.runtime import RUNTIME_FILES, RunHistory, request_stop


class RunHistoryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve() / "receiver"

    def read(self, name):
        return json.loads((self.root / name).read_text(encoding="utf-8"))

    def lines(self, name):
        return [json.loads(line) for line in (self.root / name).read_text(encoding="utf-8").splitlines()]

    def test_run_identity_active_snapshot_and_summary(self):
        history = RunHistory(self.root, "41258822921")
        self.assertEqual(history.pid, os.getpid())
        self.assertEqual(len(history.run_id), 32)
        self.assertEqual(datetime.fromisoformat(history.started_at).utcoffset(), timedelta(0))
        self.assertEqual(self.read("active-run.json")["status"], "running")
        self.assertEqual(self.read("active-run.json")["binding_public_number"], "41258822921")
        first = history.record_cycle("success", "cycle", 123.4567, notice_count=2,
            new_event_count=1, open_method="hover", recovery_count=1,
            detail_retry_count=1, open_attempts=2,
            ui_state={"visibility_state": "visible", "focused": False,
                      "panel_visible": True, "entry_hit": None, "navigation": False, "page_count": 1})
        self.assertEqual(first["duration_ms"], 123.457)
        history.record_cycle("success", "cycle", 10)
        self.assertEqual(history.summary()["consecutive_success"], 2)
        success_at = history.summary()["last_success_at"]
        history.record_cycle("timeout", "notifications", 20)
        history.record_cycle("error", "details", 30)
        self.assertEqual(history.summary()["consecutive_failure"], 2)
        history.record_cycle("paused", "identity", 40)
        self.assertEqual(history.summary(), {
            "cycle_count": 5, "success_count": 2, "timeout_count": 1, "error_count": 1,
            "paused_count": 1, "consecutive_success": 0, "consecutive_failure": 0,
            "last_success_at": success_at,
        })
        records = self.lines("cycle-history.jsonl")
        self.assertEqual([item["seq"] for item in records], [1, 2, 3, 4, 5])
        self.assertTrue(all(item["run_id"] == history.run_id for item in records))
        self.assertEqual(self.read("active-run.json")["summary"], history.summary())

    def test_context_retains_closed_lifecycle_without_cycles(self):
        with RunHistory(self.root, "41258822921") as history:
            self.assertFalse(history.stop_requested())
        active = self.read("active-run.json")
        self.assertEqual(active["status"], "closed")
        self.assertEqual(active["reason"], "completed")
        self.assertEqual([item["event"] for item in self.lines("run-history.jsonl")], ["started", "closed"])
        before = (self.root / "run-history.jsonl").read_bytes()
        self.assertEqual(history.finish("stopped"), active)
        self.assertEqual((self.root / "run-history.jsonl").read_bytes(), before)
        self.assertFalse(request_stop(self.root))
        with self.assertRaises(RuntimeError):
            history.record_cycle("success", "cycle", 0)

    def test_context_exception_has_fixed_reason_and_no_exception_text(self):
        with self.assertRaisesRegex(RuntimeError, "secret-example"):
            with RunHistory(self.root, "41258822921"):
                raise RuntimeError("secret-example")
        self.assertEqual(self.read("active-run.json")["reason"], "error")
        self.assertNotIn("secret-example", (self.root / "run-history.jsonl").read_text())

    def test_stop_targets_current_run_and_stale_request_does_not_stop_restart(self):
        self.assertFalse(request_stop(self.root))
        old = RunHistory(self.root, "41258822921")
        self.assertTrue(request_stop(self.root))
        request = self.read("stop-request.json")
        self.assertEqual(request["target_run_id"], old.run_id)
        self.assertTrue(old.stop_requested())
        old.finish("stopped")
        history_before = (self.root / "run-history.jsonl").read_bytes()
        new = RunHistory(self.root, "41258822921")
        self.assertNotEqual(new.run_id, old.run_id)
        self.assertFalse(new.stop_requested())
        self.assertEqual(self.read("stop-request.json"), request)
        self.assertTrue((self.root / "run-history.jsonl").read_bytes().startswith(history_before))
        self.assertTrue(request_stop(self.root))
        self.assertTrue(new.stop_requested())
        new.finish("stopped")

    def test_new_process_appends_and_preserves_prior_process_records(self):
        backend = Path(__file__).resolve().parents[1]
        script = (
            "from pathlib import Path\n"
            "import sys\n"
            "from app.mention_receiver.runtime import RunHistory\n"
            "with RunHistory(Path(sys.argv[1]), '41258822921') as history:\n"
            "    history.record_cycle('success', 'cycle', 1, notice_count=1)\n"
            "    print(history.run_id)\n"
        )
        env = {**os.environ, "PYTHONPATH": str(backend)}
        runs = []
        for _ in range(2):
            result = subprocess.run([sys.executable, "-c", script, str(self.root)],
                                    cwd=self.temporary.name, env=env, capture_output=True, text=True, check=True)
            runs.append(result.stdout.strip())
        self.assertNotEqual(*runs)
        cycles = self.lines("cycle-history.jsonl")
        self.assertEqual([row["run_id"] for row in cycles], runs)
        self.assertEqual([row["seq"] for row in cycles], [1, 1])
        lifecycle = self.lines("run-history.jsonl")
        self.assertEqual([row["event"] for row in lifecycle], ["started", "closed", "started", "closed"])
        self.assertNotEqual(lifecycle[0]["pid"], lifecycle[2]["pid"])
        self.assertEqual(self.read("active-run.json")["run_id"], runs[-1])

    def test_unknown_or_sensitive_fields_are_rejected_without_writing(self):
        history = RunHistory(self.root, "41258822921")
        before = (self.root / "active-run.json").read_bytes()
        for key in ("url", "raw_url", "command_text", "cookie", "authorization", "password", "error_message"):
            with self.subTest(key=key), self.assertRaises(TypeError):
                history.record_cycle("success", "cycle", 1, **{key: "sensitive-content"})
            with self.subTest(ui_key=key), self.assertRaises(ValueError):
                history.record_cycle("success", "cycle", 1, ui_state={key: "sensitive-content"})
        self.assertEqual((self.root / "active-run.json").read_bytes(), before)
        self.assertFalse((self.root / "cycle-history.jsonl").exists())
        self.assertNotIn("sensitive-content", (self.root / "run-history.jsonl").read_text())

    def test_fixed_values_and_measurements_reject_content_or_invalid_numbers(self):
        history = RunHistory(self.root, "41258822921")
        invalid = [
            {"outcome": "https://example.invalid/token"}, {"stage": "raw body"},
            {"open_method": "cookie=data"}, {"duration_ms": float("nan")},
            {"duration_ms": float("inf")}, {"duration_ms": -1}, {"duration_ms": True},
            {"duration_ms": 10 ** 1000}, {"notice_count": -1}, {"new_event_count": 1.5},
            {"recovery_count": True}, {"detail_retry_count": "2"}, {"open_attempts": None},
            {"ui_state": {"focused": "credentials"}}, {"ui_state": {"page_count": True}},
            {"ui_state": {"navigation": 1}}, {"ui_state": {"visibility_state": "URL"}},
        ]
        for overrides in invalid:
            values = {"outcome": "success", "stage": "cycle", "duration_ms": 0, **overrides}
            with self.subTest(overrides=overrides), self.assertRaises(ValueError):
                history.record_cycle(**values)
        with self.assertRaises(ValueError):
            history.finish("password=data")
        self.assertEqual(history.summary()["cycle_count"], 0)
        self.assertFalse((self.root / "cycle-history.jsonl").exists())

    def test_binding_is_public_number_only(self):
        for value in ("https://douyin.com/user/example", "1234", "12cookie", None, True):
            with self.subTest(value=value), self.assertRaises(ValueError):
                RunHistory(self.root, value)
        self.assertFalse(self.root.exists())

    def test_atomic_snapshot_is_replaced_from_exclusive_random_temporary(self):
        history = RunHistory(self.root, "41258822921")
        active = self.root / "active-run.json"
        original_bytes = active.read_bytes()
        real_replace = os.replace
        real_open = os.open
        temporary_paths = []
        def inspect_open(path, flags, *args, **kwargs):
            path = Path(path)
            if path.name.endswith(".tmp"):
                self.assertTrue(flags & os.O_EXCL)
                self.assertTrue(flags & os.O_CREAT)
                temporary_paths.append(path)
            return real_open(path, flags, *args, **kwargs)
        def inspect_replace(source, destination):
            self.assertEqual(Path(destination), active)
            self.assertEqual(active.read_bytes(), original_bytes)
            self.assertNotEqual(Path(source).name, ".active-run.json.tmp")
            self.assertEqual(json.loads(Path(source).read_text())["summary"]["cycle_count"], 1)
            return real_replace(source, destination)
        with patch("app.mention_receiver.runtime.os.open", side_effect=inspect_open), \
                patch("app.mention_receiver.runtime.os.replace", side_effect=inspect_replace):
            history.record_cycle("success", "cycle", 0)
        self.assertEqual(len(temporary_paths), 1)
        self.assertFalse(temporary_paths[0].exists())
        self.assertEqual(self.read("active-run.json")["summary"]["cycle_count"], 1)

    def test_hardlinks_are_refused_for_all_runtime_files_without_touching_target(self):
        self.root.mkdir()
        target = Path(self.temporary.name) / "original.json"
        target.write_text("{\"safe\":true}\n", encoding="utf-8")
        for name in RUNTIME_FILES:
            with self.subTest(name=name):
                path = self.root / name
                os.link(target, path)
                with self.assertRaises(ValueError):
                    RunHistory(self.root, "41258822921")
                self.assertEqual(target.read_text(), '{"safe":true}\n')
                path.unlink()

    def test_late_hardlink_for_append_snapshot_and_stop_is_refused(self):
        history = RunHistory(self.root, "41258822921")
        active_link = Path(self.temporary.name) / "active-link"
        os.link(self.root / "active-run.json", active_link)
        before = active_link.read_bytes()
        with self.assertRaises(ValueError):
            request_stop(self.root)
        with self.assertRaises(ValueError):
            history.record_cycle("success", "cycle", 0)
        self.assertEqual(active_link.read_bytes(), before)
        active_link.unlink()
        target = Path(self.temporary.name) / "append-target"
        target.write_text("untouched", encoding="utf-8")
        (self.root / "cycle-history.jsonl").unlink(missing_ok=True)
        os.link(target, self.root / "cycle-history.jsonl")
        with self.assertRaises(ValueError):
            history.record_cycle("success", "cycle", 0)
        self.assertEqual(target.read_text(), "untouched")

    def test_symlinks_in_files_and_root_are_refused(self):
        self.root.mkdir()
        target = Path(self.temporary.name) / "target.json"
        target.write_text("{}", encoding="utf-8")
        link = self.root / "active-run.json"
        try:
            link.symlink_to(target)
        except OSError as error:
            self.skipTest("symbolic links unavailable: " + str(error))
        with self.assertRaises(ValueError):
            RunHistory(self.root, "41258822921")
        with self.assertRaises(ValueError):
            request_stop(self.root)
        link.unlink()
        root_link = Path(self.temporary.name) / "linked-root"
        root_link.symlink_to(self.root, target_is_directory=True)
        with self.assertRaises(ValueError):
            RunHistory(root_link, "41258822921")
        self.assertEqual(target.read_text(), "{}")


if __name__ == "__main__":
    unittest.main()
