"""First-run setup: fresh install vs. unreadable existing configuration.

These checks use temporary directories, synthetic registry snapshots and direct
handler calls; no real run, browser, account, model, network socket or external
service is used. File-type attacks (hard links, symlinks) live in
``test_assistant_setup_links.py`` because the portable guard intentionally denies
those operations.
"""
from pathlib import Path
import tempfile
import unittest

from fastapi import HTTPException
from pydantic import ValidationError

from app.assistant import routes, setup
from app.assistant.queue import AssistantQueue, QueueError


def _snapshot(users=()):
    return {"users": list(users), "local_catalog": []}


def _user(receiver="https://www.douyin.com/user/B-public", *, verified=True, active=True, devices=()):
    return {"user_id": "user-1", "label": "本人账号",
            "sender_profile_url": "https://www.douyin.com/user/A-public",
            "receiver_profile_url": receiver, "verification_status":
            "verified" if verified else "unverified", "active": active,
            "devices": list(devices)}


def _device(*, is_local=True, verified=True, libraries=(), owner=None):
    return {"device_id": "device-1", "instance_id": "local-instance", "is_local": is_local,
            "enabled": True, "revoked": False,
            "verification_status": "verified" if verified else "unverified",
            "libraries": list(libraries), "local_owner_access": owner or {"active": False}}


class ReceiverConfigTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve() / "mention_receiver"

    def test_fresh_install_is_setup_required_not_unavailable(self):
        state = setup.receiver_setup_state(self.root)
        self.assertEqual(state["state"], "missing")
        self.assertEqual(state["reason_code"], "assistant_setup_required")
        self.assertFalse(self.root.exists())

    def test_existing_ledger_without_config_is_never_treated_as_fresh(self):
        self.root.mkdir()
        (self.root / setup.INBOX_NAME).write_bytes(b"")
        state = setup.receiver_setup_state(self.root)
        self.assertEqual(state["state"], "legacy_missing")
        with self.assertRaises(QueueError) as raised:
            setup.write_receiver_config(self.root, "12345", "https://www.douyin.com/user/B-public")
        self.assertEqual(str(raised.exception), "assistant_receiver_configuration_required")
        self.assertFalse((self.root / setup.CONFIG_NAME).exists())

    def test_write_stores_only_canonical_public_identity(self):
        state = setup.write_receiver_config(
            self.root, " 123456 ", "https://douyin.com/user/B-public/")
        self.assertEqual(state["state"], "ready")
        self.assertEqual(state["receiver_public_number"], "123456")
        self.assertEqual(state["receiver_profile_url"], "https://www.douyin.com/user/B-public")
        self.assertEqual(sorted(path.name for path in self.root.iterdir()), [setup.CONFIG_NAME])
        text = (self.root / setup.CONFIG_NAME).read_text(encoding="utf-8")
        self.assertNotIn("token", text.lower())

    def test_invalid_input_is_rejected_before_creating_state(self):
        for number, profile, code in (
            ("abc", "https://www.douyin.com/user/B-public", "invalid_receiver_public_number"),
            ("12345", "http://www.douyin.com/user/B-public", "invalid_receiver_profile_url"),
            ("12345", "https://www.douyin.com/user/self", "invalid_receiver_profile_url"),
            ("12345", "https://example.com/user/B-public", "invalid_receiver_profile_url"),
        ):
            with self.subTest(number=number, profile=profile):
                with self.assertRaises(QueueError) as raised:
                    setup.write_receiver_config(self.root, number, profile)
                self.assertEqual(str(raised.exception), code)
        self.assertFalse(self.root.exists())

    def test_existing_receiver_config_is_never_replaced(self):
        setup.write_receiver_config(self.root, "12345", "https://www.douyin.com/user/B-public")
        self.assertEqual(setup.write_receiver_config(
            self.root, "12345", "https://www.douyin.com/user/B-public")["state"], "ready")
        with self.assertRaises(QueueError) as raised:
            setup.write_receiver_config(self.root, "54321", "https://www.douyin.com/user/other-B")
        self.assertEqual(str(raised.exception), "receiver_configuration_locked")

    def test_duplicate_json_keys_are_not_accepted(self):
        self.root.mkdir()
        (self.root / setup.CONFIG_NAME).write_text(
            '{"receiver_public_number":"12345","receiver_public_number":"12345",'
            '"receiver_profile_url":"https://www.douyin.com/user/B-public"}',
            encoding="utf-8")
        state = setup.receiver_setup_state(self.root)
        self.assertEqual(state["state"], "invalid")
        self.assertEqual(state["reason_code"], "assistant_receiver_configuration_invalid")


class SetupStatusTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve() / "mention_receiver"

    def test_steps_follow_registry_progress(self):
        status = setup.setup_status(None, self.root)
        self.assertFalse(status["ready"])
        self.assertEqual(status["state"], "setup_required")
        self.assertEqual(status["next_step"], "receiver")

        setup.write_receiver_config(self.root, "12345", "https://www.douyin.com/user/B-public")
        status = setup.setup_status(_snapshot(), self.root)
        self.assertEqual(status["next_step"], "accounts")
        self.assertEqual([step["state"] for step in status["steps"]], ["ready", "missing", "blocked", "blocked"])

        status = setup.setup_status(_snapshot([_user()]), self.root)
        self.assertEqual(status["next_step"], "device")
        status = setup.setup_status(_snapshot([_user(devices=[_device()])]), self.root)
        self.assertEqual(status["next_step"], "scope")

        grant = {"grant_id": "g1", "can_query": True, "can_import": False}
        status = setup.setup_status(_snapshot([_user(devices=[_device(libraries=[grant])])]), self.root)
        self.assertTrue(status["ready"])
        self.assertIsNone(status["next_step"])
        self.assertEqual(status["login"]["state"], "on_start")

    def test_unverified_or_mismatched_account_does_not_complete_accounts(self):
        setup.write_receiver_config(self.root, "12345", "https://www.douyin.com/user/B-public")
        status = setup.setup_status(_snapshot([_user(verified=False)]), self.root)
        self.assertEqual(status["steps"][1]["state"], "missing")
        status = setup.setup_status(
            _snapshot([_user(receiver="https://www.douyin.com/user/other-B")]), self.root)
        self.assertEqual(status["steps"][1]["state"], "missing")

    def test_owner_scope_counts_as_an_authorized_scope(self):
        setup.write_receiver_config(self.root, "12345", "https://www.douyin.com/user/B-public")
        owner = {"active": True, "query_all_local": True}
        status = setup.setup_status(_snapshot([_user(devices=[_device(owner=owner)])]), self.root)
        self.assertTrue(status["ready"])


class SetupOverviewTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        path = Path(temporary.name).resolve() / "queue.sqlite3"
        self.queue = AssistantQueue(path)
        from types import SimpleNamespace
        self.control = SimpleNamespace(snapshot=lambda: {"state": "stopped", "desired_state": "stopped"})
        self.root = Path(temporary.name).resolve() / "mention_receiver"
        self.binding = "assistant_setup_required"
        self.service = routes.AssistantAPI(
            self.queue, self.control,
            binding_provider=self._binding,
            setup_provider=lambda: setup.setup_status(None, self.root),
            receiver_writer=lambda number, profile: setup.write_receiver_config(self.root, number, profile))

    def _binding(self):
        raise QueueError(self.binding)

    def test_fresh_install_is_shown_as_setup_not_unreadable(self):
        value = self.service.overview()
        self.assertEqual(value["configuration"]["state"], "setup_required")
        self.assertIn("首次使用", value["configuration"]["message"])
        self.assertFalse(value["capabilities"]["start"])
        self.assertTrue(value["capabilities"]["stop"])
        self.assertEqual(value["setup"]["steps"][0]["key"], "receiver")
        self.assertIsNone(value["binding"])

    def test_invalid_existing_configuration_stays_unavailable(self):
        self.binding = "assistant_receiver_configuration_invalid"
        value = self.service.overview()
        self.assertEqual(value["configuration"]["state"], "unavailable")
        self.assertEqual(value["configuration"]["reason_code"], "assistant_receiver_configuration_invalid")
        self.assertFalse(value["capabilities"]["start"])

    def test_start_stays_disabled_while_setup_steps_remain(self):
        self.service.binding_provider = lambda: {
            "binding_id": "partial-binding", "sender_profile_url": "https://www.douyin.com/user/A-public",
            "receiver_profile_url": "https://www.douyin.com/user/B-public", "knowledge_base_id": 7}
        setup.write_receiver_config(self.root, "12345", "https://www.douyin.com/user/B-public")
        value = self.service.overview()
        self.assertEqual(value["configuration"]["state"], "ready")
        self.assertEqual(value["setup"]["next_step"], "accounts")
        self.assertFalse(value["capabilities"]["start"])

    def test_setup_handlers_project_steps_and_fixed_failures(self):
        status = (routes.setup(service=self.service))
        self.assertEqual(status["state"], "setup_required")

        with self.assertRaises(HTTPException) as invalid:
            routes.save_receiver(routes.ReceiverSetupBody(
                receiver_public_number="abc",
                receiver_profile_url="https://www.douyin.com/user/B-public"), service=self.service)
        self.assertEqual(invalid.exception.status_code, 400)
        self.assertEqual(invalid.exception.detail["code"], "invalid_receiver_public_number")
        self.assertNotIn("abc", invalid.exception.detail["message"])

        with self.assertRaises(ValidationError):
            routes.ReceiverSetupBody()

        saved = routes.save_receiver(routes.ReceiverSetupBody(
            receiver_public_number="123456",
            receiver_profile_url="https://douyin.com/user/B-public/"), service=self.service)
        self.assertTrue(saved["success"])
        self.assertEqual(saved["setup"]["steps"][0]["state"], "ready")


if __name__ == "__main__":
    unittest.main()
