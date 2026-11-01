"""Self-serve native (API) intake provisioning: config, capsule, store."""
from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from app.assistant import native_provision
from app.assistant.native_configuration import load_native_configuration

SEC_UID = "MS4wLjABAAAAtestsecuid0000000000000000000000000000000"
PROFILE_URL = "https://www.douyin.com/user/" + SEC_UID
PUBLIC_NUMBER = "41258822921"


class FakeProtector:
    def protect(self, value, entropy):
        return b"sealed:" + bytes(value)

    def unprotect(self, value, entropy):
        if not value.startswith(b"sealed:"):
            raise ValueError("invalid")
        return value[len(b"sealed:"):]


class ProvisionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.receiver_state = self.root / "mention_receiver"
        self.queue_state = self.root / "assistant"
        self.receiver_state.mkdir(parents=True)
        (self.receiver_state / "receiver.local.json").write_text(json.dumps({
            "receiver_public_number": PUBLIC_NUMBER, "receiver_profile_url": PROFILE_URL,
        }), encoding="utf-8")
        self.now = 1_800_000_000.0
        self.uid = "1234567890123456"
        self.cookies = [{"name": "sessionid", "value": "abc123", "domain": ".douyin.com",
                         "path": "/", "expires": self.now + 3600, "secure": True,
                         "httpOnly": True, "sameSite": "Lax"}]

    def capture(self, uid=None):
        return lambda: {"user_agent": "Mozilla/5.0 test", "cookies": self.cookies}

    def network(self, uid=None, status=200, code=0, flags=None):
        value = {"status_code": code, "user_uid": uid or self.uid}
        if flags:
            value.update(flags)
        body = json.dumps(value).encode()

        def call(url, headers, timeout, max_bytes):
            return status, body

        return call

    def provision(self, **overrides):
        arguments = dict(receiver_state=self.receiver_state, queue_state=self.queue_state,
                         capture=self.capture(), network_get=self.network(),
                         clock=lambda: self.now, protector=FakeProtector())
        arguments.update(overrides)
        return native_provision.provision(**arguments)

    def test_enable_writes_valid_config_capsule_and_store(self):
        result = self.provision()
        self.assertTrue(result["enabled"])
        config = load_native_configuration(self.queue_state, PROFILE_URL)
        self.assertIsNotNone(config)
        self.assertEqual(config["receiver_uid"], self.uid)
        self.assertEqual(config["receiver_sec_uid"], SEC_UID)
        self.assertEqual(config["session_source"], "owned_capsule")
        self.assertEqual(config["receiver_profile_url"], PROFILE_URL)
        self.assertTrue((self.queue_state / "native-receiver" / "sources.sqlite3").is_file())
        vault = native_provision.NativeSessionVault if hasattr(native_provision, "NativeSessionVault") else None

    def test_capsule_is_sealed_and_loadable(self):
        from app.assistant.native_session import NativeSessionVault

        self.provision()
        vault = NativeSessionVault(self.uid, private_root=self.queue_state / "native-receiver" / "private",
                                   clock=lambda: self.now, protector=FakeProtector())
        session = vault.load()
        self.assertEqual(session.expected_uid, self.uid)
        self.assertIn("sessionid=abc123", session._cookie_header)
        self.assertEqual(session.expires_at, self.now + 3600)

    def test_verification_flags_do_not_enable(self):
        with self.assertRaises(native_provision.NativeProvisionError) as raised:
            self.provision(network_get=self.network(flags={"verify_required": True}))
        self.assertEqual(raised.exception.code, "native_provision_verification_required")
        self.assertFalse((self.queue_state / "native-receiver.json").exists())

    def test_identity_mismatch_does_not_overwrite(self):
        self.provision()
        with self.assertRaises(native_provision.NativeProvisionError) as raised:
            self.provision(network_get=self.network(uid="9999999999999999"))
        self.assertEqual(raised.exception.code, "native_provision_identity_changed")
        config = load_native_configuration(self.queue_state, PROFILE_URL)
        self.assertEqual(config["receiver_uid"], self.uid)

    def test_disable_keeps_ledger_and_falls_back(self):
        self.provision()
        result = native_provision.disable(queue_state=self.queue_state)
        self.assertTrue(result["changed"])
        self.assertIsNone(load_native_configuration(self.queue_state, PROFILE_URL))
        self.assertTrue((self.queue_state / "native-receiver" / "sources.sqlite3").is_file())
        raw = json.loads((self.queue_state / "native-receiver.json").read_text(encoding="utf-8"))
        self.assertFalse(raw["enabled"])

    def test_invalid_existing_config_is_not_replaced(self):
        (self.queue_state).mkdir(parents=True, exist_ok=True)
        (self.queue_state / "native-receiver.json").write_text("{not json", encoding="utf-8")
        with self.assertRaises(native_provision.NativeProvisionError) as raised:
            self.provision()
        self.assertEqual(raised.exception.code, "native_provision_configuration_invalid")
        self.assertEqual((self.queue_state / "native-receiver.json").read_text(encoding="utf-8"), "{not json")

    def test_missing_receiver_local_config_is_actionable(self):
        (self.receiver_state / "receiver.local.json").unlink()
        with self.assertRaises(native_provision.NativeProvisionError) as raised:
            self.provision()
        self.assertEqual(raised.exception.code, "native_provision_receiver_configuration_required")

    def test_identity_uid_requires_numeric_string(self):
        with self.assertRaises(native_provision.NativeProvisionError) as raised:
            native_provision.provision(receiver_state=self.receiver_state, queue_state=self.queue_state,
                                       capture=self.capture(),
                                       network_get=self.network(uid="not-a-uid"),
                                       clock=lambda: self.now, protector=FakeProtector())
        self.assertEqual(raised.exception.code, "native_provision_identity_unavailable")


class StatusTests(unittest.TestCase):
    def test_status_reports_disabled_after_disable(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name).resolve()
        receiver_state, queue_state = root / "mention_receiver", root / "assistant"
        receiver_state.mkdir(parents=True)
        (receiver_state / "receiver.local.json").write_text(json.dumps({
            "receiver_public_number": PUBLIC_NUMBER, "receiver_profile_url": PROFILE_URL,
        }), encoding="utf-8")
        now = 1_800_000_000.0
        cookies = [{"name": "sessionid", "value": "abc123", "domain": ".douyin.com",
                    "path": "/", "expires": now + 3600}]
        native_provision.provision(receiver_state=receiver_state, queue_state=queue_state,
                                   capture=lambda: {"user_agent": "Mozilla/5.0 test", "cookies": cookies},
                                   network_get=lambda *a: (200, json.dumps(
                                       {"status_code": 0, "user_uid": "1234567890123456"}).encode()),
                                   clock=lambda: now, protector=FakeProtector())
        self.assertTrue(native_provision.status(queue_state=queue_state,
                                                receiver_state=receiver_state)["enabled"])
        native_provision.disable(queue_state=queue_state)
        state = native_provision.status(queue_state=queue_state, receiver_state=receiver_state)
        self.assertTrue(state["configured"])
        self.assertFalse(state["enabled"])


if __name__ == "__main__":
    unittest.main()
