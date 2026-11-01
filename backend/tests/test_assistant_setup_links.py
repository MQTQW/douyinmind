"""File-type safety for the public receiver configuration.

Hard links and symlinks are denied by the portable guard, so this file is run
with the normal pytest entry (like the existing in-process TestClient reviews),
not with the portable guarded runner.
"""
import os
from pathlib import Path
import tempfile
import unittest

from app.assistant import setup
from app.assistant.queue import QueueError

CONFIG = '{"receiver_public_number":"12345",' \
         '"receiver_profile_url":"https://www.douyin.com/user/B-public"}'


class ReceiverFileSafetyTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve() / "mention_receiver"
        self.root.mkdir()
        self.config = self.root / setup.CONFIG_NAME

    def test_hard_linked_config_is_not_accepted(self):
        self.config.write_text(CONFIG, encoding="utf-8")
        linked = self.root / "linked.json"
        try:
            os.link(self.config, linked)
        except (OSError, NotImplementedError):
            self.skipTest("hard links are not available on this filesystem")
        state = setup.receiver_setup_state(self.root)
        self.assertEqual(state["state"], "invalid")
        with self.assertRaises(QueueError) as raised:
            setup.write_receiver_config(self.root, "12345", "https://www.douyin.com/user/B-public")
        self.assertEqual(str(raised.exception), "assistant_receiver_configuration_invalid")

    def test_symlinked_config_is_not_accepted(self):
        real = self.root / "real.json"
        real.write_text(CONFIG, encoding="utf-8")
        try:
            os.symlink(real, self.config)
        except (OSError, NotImplementedError):
            self.skipTest("symlinks are not available for this process")
        state = setup.receiver_setup_state(self.root)
        self.assertEqual(state["state"], "invalid")


if __name__ == "__main__":
    unittest.main()
