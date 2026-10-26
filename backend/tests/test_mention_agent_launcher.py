"""Foreground launcher with fake library description and receiver only."""
from contextlib import redirect_stdout
from datetime import datetime, timezone
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from app.mention_agent import launcher


def binding(**overrides):
    return {"binding_id": "fixture-binding", "instance_id": "fixture-desktop",
            "sender_profile_url": "https://www.douyin.com/user/fixture-A",
            "receiver_profile_url": "https://www.douyin.com/user/fixture-B",
            "knowledge_base_id": 7, "public_reply_allowed": True, "active": True, **overrides}


class AgentLauncherTests(unittest.TestCase):
    def setUp(self):
        legacy_guard=patch.object(launcher,'legacy_testing_blocked',return_value=False)
        legacy_guard.start()
        self.addCleanup(legacy_guard.stop)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.path = self.root / "local-binding.json"
        self.path.write_text(json.dumps(binding()), encoding="utf-8")
        self.state = self.root / "agent.sqlite3"
        self.description = {"id": 7, "name": "虚构学习库", "ready_count": 2, "total_count": 2}

    def invoke(self, *, description=None, code=0, marker="DM-20261021T120000Z-AUTOQA-A1B2C3D4", allow_import=False):
        output = io.StringIO()
        with redirect_stdout(output), patch.object(launcher, "_describe_library",
                return_value=self.description if description is None else description) as describe, \
                patch.object(launcher, "_receiver_main", return_value=code) as receive, \
                patch.object(launcher, "generate_marker", return_value=marker):
            arguments = ["--binding", str(self.path), "--state", str(self.state)]
            if allow_import:
                arguments.append("--allow-import")
            result = launcher.main(arguments)
        return result, output.getvalue(), describe, receive

    def test_valid_existing_binding_prints_question_and_passes_bounded_auto_flags(self):
        before = self.path.read_bytes()
        result, output, describe, receive = self.invoke()
        self.assertEqual(result, 0)
        self.assertIn("虚构学习库", output)
        self.assertIn("请根据你绑定的知识库", output)
        self.assertIn("DM-20261021T120000Z-AUTOQA-A1B2C3D4", output)
        self.assertIn("从 @ 列表选中 B", output)
        self.assertEqual(describe.call_args.args[0].knowledge_base_id, 7)
        receive.assert_called_once_with([
            "--agent-binding", str(self.path), "--agent-state", str(self.state),
            "--agent-marker", "DM-20261021T120000Z-AUTOQA-A1B2C3D4",
            "--agent-max-tasks", "1", "--agent-auto-reply", "--duration-minutes", "20"])
        self.assertEqual(self.path.read_bytes(), before)
        self.assertFalse(self.state.exists(), "fake receiver must not create a real task store")
        self.assertEqual(list(self.root.iterdir()), [self.path])

    def test_generated_marker_is_new_ascii_datetime_and_random_not_a_credential(self):
        with patch.object(launcher, "datetime") as clock, \
                patch.object(launcher.secrets, "token_hex", side_effect=["a1b2c3d4", "e5f6a7b8"]):
            clock.now.return_value = datetime(2026, 10, 21, 12, 34, 56, tzinfo=timezone.utc)
            first, second = launcher.generate_marker(), launcher.generate_marker()
        self.assertRegex(first, r"^DM-20261021T123456Z-AUTOQA-[0-9A-F]{8}$")
        self.assertTrue(first.isascii())
        self.assertLessEqual(len(first), 80)
        self.assertNotEqual(first, second)

    def test_missing_binding_is_not_created_and_receiver_is_not_started(self):
        self.path.unlink()
        result, output, describe, receive = self.invoke()
        self.assertEqual(result, 1)
        self.assertIn("不会自动创建绑定", output)
        describe.assert_not_called()
        receive.assert_not_called()
        self.assertFalse(self.path.exists())

    def test_inactive_binding_is_not_enabled(self):
        self.path.write_text(json.dumps(binding(active=False)), encoding="utf-8")
        before = self.path.read_bytes()
        result, output, describe, receive = self.invoke()
        self.assertEqual(result, 1)
        self.assertIn("绑定已停用", output)
        describe.assert_not_called()
        receive.assert_not_called()
        self.assertEqual(self.path.read_bytes(), before)

    def test_missing_public_permission_is_not_silently_granted(self):
        self.path.write_text(json.dumps(binding(public_reply_allowed=False)), encoding="utf-8")
        before = self.path.read_bytes()
        result, output, describe, receive = self.invoke()
        self.assertEqual(result, 1)
        self.assertIn("尚未允许公开回复", output)
        describe.assert_not_called()
        receive.assert_not_called()
        self.assertEqual(self.path.read_bytes(), before)

    def test_malformed_binding_is_not_printed_or_used(self):
        self.path.write_text('{"unexpected_private_value":"fixture-do-not-print"}', encoding="utf-8")
        result, output, describe, receive = self.invoke()
        self.assertEqual(result, 1)
        self.assertNotIn("fixture-do-not-print", output)
        describe.assert_not_called()
        receive.assert_not_called()

    def test_wrong_library_or_empty_ready_scope_stops_before_receiver(self):
        for description in ({**self.description, "id": 8}, {**self.description, "ready_count": 0},
                            {**self.description, "name": ""}):
            with self.subTest(description=description):
                result, _, _, receive = self.invoke(description=description)
                self.assertEqual(result, 1)
                receive.assert_not_called()

    def test_receiver_failure_is_returned_without_retry(self):
        result, output, _, receive = self.invoke(code=2)
        self.assertEqual(result, 2)
        self.assertIn("不会自动重启或重发", output)
        receive.assert_called_once()

    def test_description_exception_is_sanitized_and_never_starts_receiver(self):
        output = io.StringIO()
        with redirect_stdout(output), patch.object(launcher, "_describe_library",
                side_effect=RuntimeError("fixture-sensitive-detail")), \
                patch.object(launcher, "_receiver_main") as receive:
            result = launcher.main(["--binding", str(self.path), "--state", str(self.state)])
        self.assertEqual(result, 1)
        self.assertIn("RuntimeError", output.getvalue())
        self.assertNotIn("fixture-sensitive-detail", output.getvalue())
        receive.assert_not_called()

    def test_import_mode_accepts_existing_empty_library_and_prints_explicit_current_video_command(self):
        before = self.path.read_bytes()
        marker = "DM-20261021T120000Z-IMPORT-A1B2C3D4"
        result, output, describe, receive = self.invoke(
            description={**self.description, "ready_count": 0, "total_count": 0},
            marker=marker, allow_import=True)
        self.assertEqual(result, 0)
        self.assertIn("请把这个视频加入“虚构学习库”知识库。测试编号 " + marker + "。", output)
        self.assertIn("视频入库测试", output)
        self.assertNotIn("请根据你绑定的知识库", output)
        self.assertEqual(describe.call_args.args[0].knowledge_base_id, 7)
        receive.assert_called_once_with([
            "--agent-binding", str(self.path), "--agent-state", str(self.state),
            "--agent-marker", marker, "--agent-max-tasks", "1", "--agent-auto-reply",
            "--duration-minutes", "20", "--agent-allow-import"])
        self.assertEqual(self.path.read_bytes(), before)
        self.assertFalse(self.state.exists())

    def test_import_instruction_preserves_exact_library_name_including_internal_spaces(self):
        name = "虚构  学习库"
        marker = "DM-20261021T120000Z-IMPORT-FFEEDDCC"
        result, output, _, receive = self.invoke(description={**self.description, "name": name},
                                                marker=marker, allow_import=True)
        self.assertEqual(result, 0)
        self.assertIn(f"请把这个视频加入“{name}”知识库。测试编号 {marker}。", output)
        self.assertIn("--agent-allow-import", receive.call_args.args[0])

    def test_import_marker_is_fresh_ascii_and_distinct_from_qa_mode(self):
        with patch.object(launcher, "datetime") as clock, \
                patch.object(launcher.secrets, "token_hex", side_effect=["a1b2c3d4", "e5f6a7b8"]):
            clock.now.return_value = datetime(2026, 10, 21, 12, 34, 56, tzinfo=timezone.utc)
            first = launcher.generate_marker(allow_import=True)
            second = launcher.generate_marker(allow_import=True)
        self.assertRegex(first, r"^DM-20261021T123456Z-IMPORT-[0-9A-F]{8}$")
        self.assertTrue(first.isascii())
        self.assertLessEqual(len(first), 80)
        self.assertNotEqual(first, second)
        self.assertNotIn("AUTOQA", first)

    def test_import_mode_still_rejects_missing_or_wrong_library(self):
        for description in ({**self.description, "id": 8}, {**self.description, "id": True},
                            {**self.description, "name": ""}):
            with self.subTest(description=description):
                result, _, _, receive = self.invoke(description=description, allow_import=True)
                self.assertEqual(result, 1)
                receive.assert_not_called()

    def test_import_mode_never_enables_inactive_or_nonpublic_binding(self):
        for changes in ({"active": False}, {"public_reply_allowed": False}):
            with self.subTest(binding=changes):
                self.path.write_text(json.dumps(binding(**changes)), encoding="utf-8")
                before = self.path.read_bytes()
                result, _, describe, receive = self.invoke(allow_import=True)
                self.assertEqual(result, 1)
                describe.assert_not_called()
                receive.assert_not_called()
                self.assertEqual(self.path.read_bytes(), before)

    def test_import_mode_does_not_create_missing_binding(self):
        self.path.unlink()
        result, output, describe, receive = self.invoke(allow_import=True)
        self.assertEqual(result, 1)
        self.assertIn("不会自动创建绑定", output)
        describe.assert_not_called()
        receive.assert_not_called()
        self.assertFalse(self.path.exists())

    def test_import_receiver_failure_returns_without_retry(self):
        result, output, _, receive = self.invoke(code=2, allow_import=True,
                                                marker="DM-20261021T120000Z-IMPORT-12345678")
        self.assertEqual(result, 2)
        self.assertIn("不会自动重启或重发", output)
        receive.assert_called_once()


if __name__ == "__main__":
    unittest.main()
