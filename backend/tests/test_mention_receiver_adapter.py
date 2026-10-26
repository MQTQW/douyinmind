"""Behavioural boundaries; no real browser, auth, models or project data."""
import hashlib
from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import unittest

from app.mention_receiver.browser import canonical_profile, isolated_profile
from app.mention_receiver.catalog import read_libraries, configured_database
from app.mention_receiver.dashboard import write_dashboard
from app.mention_receiver.__main__ import receiver_lock, AlreadyRunningError, preflight_state, bind_receiver


class ReceiverBoundaryTests(unittest.TestCase):
    def test_account_relative_and_external_profile_rejected(self):
        for value in ("https://www.douyin.com/user/self", "https://evil.example/user/a",
                      "https://www.douyin.com:8443/user/a", "https://user:password@www.douyin.com/user/a"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                canonical_profile(value)
        self.assertEqual(canonical_profile("https://www.douyin.com/user/public-id?from_tab=main"),
                         "https://www.douyin.com/user/public-id")

    def test_original_profile_cannot_overlap_in_either_direction(self):
        with tempfile.TemporaryDirectory() as directory:
            storage = Path(directory).resolve()
            target = storage / "mention_receiver/browser_b"
            for protected in (target, target.parent, target / "nested"):
                with self.subTest(protected=protected), self.assertRaises(ValueError):
                    isolated_profile(storage, protected)
            self.assertEqual(isolated_profile(storage, storage / "playwright_user_data"), target)

    def test_symlink_profile_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            storage = Path(directory).resolve()
            (storage / "mention_receiver").mkdir()
            (storage / "original").mkdir()
            try:
                (storage / "mention_receiver/browser_b").symlink_to(storage / "original", target_is_directory=True)
            except OSError:
                self.skipTest("OS does not permit directory symlinks")
            with self.assertRaises(ValueError):
                isolated_profile(storage, storage / "original")

    def test_catalogue_read_does_not_change_database(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "business.db"
            with closing(sqlite3.connect(path)) as connection:
                connection.execute("CREATE TABLE knowledge_bases(id INTEGER, name TEXT)")
                connection.execute("INSERT INTO knowledge_bases VALUES(7, '本地测试库')")
                connection.commit()
            before = hashlib.sha256(path.read_bytes()).hexdigest()
            self.assertEqual(read_libraries(path), [{"id": 7, "name": "本地测试库"}])
            self.assertEqual(before, hashlib.sha256(path.read_bytes()).hexdigest())

    def test_missing_catalogue_is_not_created(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "missing.db"
            with self.assertRaises(FileNotFoundError):
                read_libraries(path)
            self.assertFalse(path.exists())

    def test_untrusted_comment_is_escaped_in_dashboard(self):
        class Inbox:
            def list_events(self):
                return [{"status": "needs_review", "target_library_name": '<img src=x onerror="bad()">',
                         "command_text": "<script>bad()</script>", "video_id": "123456"}]
            def list_candidates(self):
                return []
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_dashboard(root, Inbox(), state="回放检查", message="没有执行导入", mode="replay")
            text = (root / "接收测试台.html").read_text(encoding="utf-8")
            self.assertNotIn("<script>", text)
            self.assertNotIn("<img src", text)
            self.assertIn("&lt;script&gt;", text)
            self.assertIn("回放验证", text)

    def test_only_one_live_runner_and_lock_is_released(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with receiver_lock(root):
                with self.assertRaises(AlreadyRunningError):
                    with receiver_lock(root):
                        self.fail("second receiver acquired lock")
            with receiver_lock(root):
                pass

    def test_preflight_rejects_overlap_before_creating_any_files(self):
        with tempfile.TemporaryDirectory() as directory:
            storage = Path(directory).resolve()
            root = storage / 'mention_receiver'
            database = storage / 'business.db'
            with self.assertRaises(ValueError):
                preflight_state(root, root, database)
            self.assertFalse(root.exists())

    def test_hardlinked_inbox_cannot_modify_business_database(self):
        import os
        with tempfile.TemporaryDirectory() as directory:
            storage = Path(directory).resolve()
            root = storage / 'mention_receiver'
            root.mkdir()
            database = storage / 'business.db'
            database.write_bytes(b'unchanged business data')
            os.link(database, root / 'inbox.sqlite3')
            with self.assertRaises(ValueError):
                preflight_state(root, storage / 'original_profile', database)
            self.assertEqual(database.read_bytes(), b'unchanged business data')

    def test_bound_inbox_cannot_silently_switch_receiver(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = {'receiver_public_number': '123456', 'receiver_profile_url': 'https://www.douyin.com/user/b1'}
            bind_receiver(root, config)
            bind_receiver(root, config)
            with self.assertRaises(ValueError):
                bind_receiver(root, {**config, 'receiver_public_number': '999999'})

    def test_database_uses_explicit_configured_file(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            (root / 'custom.db').write_bytes(b'')
            self.assertEqual(configured_database('sqlite:///custom.db', root), root / 'custom.db')
            with self.assertRaises(ValueError):
                configured_database('postgresql://example.invalid/project', root)


if __name__ == "__main__":
    unittest.main()
