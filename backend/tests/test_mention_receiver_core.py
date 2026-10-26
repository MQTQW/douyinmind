"""Isolated inbox tests: standard library only, never touches the product DB."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from app.mention_receiver.core import SQLiteInbox, event_identity, notification_identity, parse_command


SENDER = "https://www.douyin.com/user/test_sender_public"
VIDEO = "7420000000000000001"
TEXT = "@6 帮我把这个视频加入‘测试知识库’，测试编号 DM-1021-D500。"
LIBRARIES = [{"id": "test-kb", "name": "测试知识库"}]
FINGERPRINT = {"video_url": "https://www.douyin.com/video/" + VIDEO}


class ParserTests(unittest.TestCase):
    def test_real_command_and_paired_quotes(self):
        for left, right in (("“", "”"), ("‘", "’"), ('"', '"'), ("'", "'"), ("「", "」"), ("『", "』")):
            for action in ("加入", "导入", "导入到", "上传到"):
                with self.subTest(left=left, action=action):
                    result = parse_command(f"@6 帮我把这个视频{action}{left}测试知识库{right}，测试编号 DM-1021-D500。")
                    self.assertEqual(result.status, "ready")
                    self.assertEqual(result.target_library_name, "测试知识库")
                    self.assertEqual(result.marker, "DM-1021-D500")

    def test_incomplete_mismatched_and_multiple_targets_need_review(self):
        for text in ("", "@6 把这个视频上传到测试知识库", "@6 把这个视频加入‘’", "@6 把这个视频加入‘测试知识库”",
                     "@6 把这个视频加入‘A’并导入到‘B’", "@6 把这个视频加入‘A’、‘B’", "@6 把这个视频加入‘A’和B",
                     "@6 把这个视频加入‘A’，以及B", "@6 不要把这个视频加入‘A’"):
            with self.subTest(text=text):
                self.assertEqual(parse_command(text).status, "needs_review")

    def test_normal_sentence_without_explicit_video_reference_is_not_a_command(self):
        for text in ("我看他把茶加入‘水’", "@6 帮我加入‘测试知识库’"):
            with self.subTest(text=text):
                self.assertEqual(parse_command(text).status, "needs_review")
        for reference in ("这个视频", "这条视频", "本视频", "该视频", "此视频"):
            with self.subTest(reference=reference):
                self.assertEqual(parse_command(f"@6 帮我把{reference}加入‘测试知识库’").status, "ready")

    def test_negation_anywhere_before_action_is_never_ready(self):
        for text in ("不要帮我把这个视频加入‘X’", "@6 别帮我把这个视频导入到‘X’",
                     "@6 取消把这个视频上传到‘X’", "禁止将本视频加入‘X’",
                     "@6 不必再把这个视频加入‘X’", "@6 无需将该视频加入‘X’"):
            with self.subTest(text=text):
                self.assertEqual(parse_command(text).status, "needs_review")


class InboxTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "only-inbox.sqlite3"
        self.inbox = SQLiteInbox(self.path)

    def observe(self, fingerprint=FINGERPRINT):
        return self.inbox.observe_notification(SENDER, TEXT, fingerprint)

    def complete(self, candidate, **overrides):
        arguments = dict(sender_profile_url=SENDER, video_id=VIDEO, command_text=TEXT,
                         libraries=LIBRARIES, video_title="公开测试视频", author_display_name="公开作者")
        arguments.update(overrides)
        return self.inbox.complete_notification(candidate.notification_key, **arguments)

    def test_id_uses_profile_exact_text_and_semantic_card_not_time(self):
        key = notification_identity(SENDER, TEXT, {"video_url": VIDEO, "kind": "mention"})
        self.assertEqual(key, notification_identity(SENDER + "?from=notice", TEXT, {"kind": "mention", "video_url": VIDEO}))
        self.assertNotEqual(key, notification_identity(SENDER, TEXT + " ", {"video_url": VIDEO, "kind": "mention"}))
        self.assertNotEqual(key, notification_identity(SENDER, TEXT, {"video_url": VIDEO + "2", "kind": "mention"}))
        with self.assertRaises(ValueError):
            notification_identity(SENDER, TEXT, {"time": "刚刚", "video_url": VIDEO})
        with self.assertRaises(ValueError):
            notification_identity("https://www.douyin.com/user/self", TEXT, FINGERPRINT)

    def test_observation_survives_restart_and_repeat(self):
        first = self.observe()
        second = SQLiteInbox(self.path).observe_notification(SENDER, TEXT, FINGERPRINT)
        self.assertEqual(first.notification_key, second.notification_key)
        self.assertEqual(second.status, "pending")
        self.assertEqual(len(self.inbox.list_candidates()), 1)
        self.assertEqual(self.inbox.list_events(), [])

    def test_partial_detail_and_navigation_failure_remain_retryable(self):
        candidate = self.observe()
        incomplete = self.complete(candidate, video_id=None)
        self.assertEqual(incomplete.status, "retryable")
        self.assertEqual(incomplete.details["command_text"], TEXT)
        self.assertEqual(self.inbox.list_events(), [])
        failed = self.inbox.mark_retryable(candidate.notification_key, "视频页暂未加载")
        self.assertEqual(failed.status, "retryable")
        self.assertEqual(len(self.inbox.list_candidates(["retryable"])), 1)
        completed = self.complete(candidate)
        self.assertEqual(completed.status, "received")
        self.assertEqual(len(self.inbox.list_events()), 1)

    def test_completion_unique_across_repeats_and_restart(self):
        candidate = self.observe()
        first = self.complete(candidate)
        second = self.complete(candidate)
        third = SQLiteInbox(self.path).complete_notification(candidate.notification_key,
            sender_profile_url=SENDER, video_id=VIDEO, command_text=TEXT, libraries=LIBRARIES)
        self.assertEqual(first.event_key, event_identity(SENDER, VIDEO, TEXT))
        self.assertEqual(first.event_key, second.event_key)
        self.assertEqual(first.event_key, third.event_key)
        self.assertEqual(len(self.inbox.list_events()), 1)
        self.assertEqual(len(self.inbox.list_candidates()), 1)
        flags = self.inbox.list_events()[0].to_dict()
        self.assertTrue(flags["preview_only"])
        self.assertFalse(flags["import_enabled"])
        self.assertEqual(flags["id_quality"], "local_fingerprint")
        self.assertEqual(flags["metadata"]["video_title"], "公开测试视频")
        self.assertEqual(flags["video_title"], "公开测试视频")
        self.assertEqual(self.inbox.mark_retryable(first.notification_key, "later error").status, "received")

    def test_different_cards_for_same_event_store_one_event(self):
        first = self.observe()
        second = self.observe({"video_url": VIDEO, "other_card_variant": "public"})
        self.complete(first)
        self.complete(second)
        self.assertEqual(len(self.inbox.list_candidates()), 2)
        self.assertEqual(len(self.inbox.list_events()), 1)

    def test_missing_ambiguous_and_inexact_library_are_saved_for_review(self):
        for libraries in (None, [], [{"id": 1, "name": "测试知识库 "}],
                          [{"id": 1, "name": "测试知识库"}, {"id": 2, "name": "测试知识库"}]):
            with self.subTest(libraries=libraries):
                inbox = SQLiteInbox(Path(self.directory.name) / (str(len(list(Path(self.directory.name).iterdir()))) + ".db"))
                candidate = inbox.observe_notification(SENDER, TEXT, FINGERPRINT)
                result = inbox.complete_notification(candidate.notification_key, sender_profile_url=SENDER,
                    video_id=VIDEO, command_text=TEXT, libraries=libraries)
                self.assertEqual(result.status, "needs_review")
                event = inbox.list_events()[0]
                self.assertEqual(event.video_id, VIDEO)
                self.assertEqual(event.command_text, TEXT)
                self.assertIsNone(event.library_id)

    def test_incomplete_target_creates_review_not_ready(self):
        candidate = self.observe()
        result = self.complete(candidate, command_text="@6 帮我加入")
        self.assertEqual(result.status, "needs_review")
        self.assertEqual(self.inbox.list_events()[0].status, "needs_review")

    def test_missing_fingerprint_never_claims_ready_identity(self):
        for fingerprint in (None, " \t\n"):
            with self.subTest(fingerprint=fingerprint):
                candidate = self.observe(fingerprint)
                self.assertEqual(candidate.status, "pending")
                result = self.complete(candidate)
                self.assertEqual(result.status, "needs_review")
                self.assertIn("碰撞", result.identity_note)
        self.assertEqual(self.inbox.list_events()[0].status, "needs_review")

    def test_quarantine_preserves_incomplete_rows_and_is_durable_idempotent(self):
        for fingerprint, should_retry in (("", False), (" \t\n", True)):
            with self.subTest(fingerprint=fingerprint, should_retry=should_retry):
                with patch("app.mention_receiver.core._now", return_value="2026-10-21T01:00:00+00:00"):
                    original = self.observe(fingerprint)
                if should_retry:
                    with patch("app.mention_receiver.core._now", return_value="2026-10-21T01:01:00+00:00"):
                        self.complete(original, video_id=None)
                        original = self.inbox.mark_retryable(original.notification_key, "打开提醒对应评论")
                with patch("app.mention_receiver.core._now", return_value="2026-10-21T02:00:00+00:00"):
                    changed = self.inbox.quarantine_incomplete_candidates()
                self.assertEqual(len(changed), 1)
                result = changed[0]
                self.assertEqual(result.status, "needs_review")
                for field in ("notification_key", "source_profile_url", "exact_text", "semantic_fingerprint",
                              "attempts", "event_key", "last_error", "created_at"):
                    self.assertEqual(getattr(result, field), getattr(original, field), field)
                for key, value in original.details.items():
                    self.assertEqual(result.details[key], value)
                audit = result.details["incomplete_candidate_quarantine"]
                self.assertEqual(audit["previous_status"], original.status)
                self.assertEqual(audit["previous_updated_at"], original.updated_at)
                self.assertEqual(audit["previous_identity_note"], original.identity_note)
                self.assertEqual(audit["quarantined_at"], "2026-10-21T02:00:00+00:00")
                self.assertEqual(audit["reason_code"], "incomplete_notification_fingerprint_v1")
                self.assertIn("停止自动重试", result.identity_note)
                if original.identity_note:
                    self.assertTrue(result.identity_note.startswith(original.identity_note))
                reopened = SQLiteInbox(self.path)
                self.assertEqual(reopened.quarantine_incomplete_candidates(), [])
                self.assertIn(result, reopened.list_candidates())
                self.assertEqual(reopened.list_events(), [])
        self.assertEqual(len(self.inbox.list_candidates()), 2)

    def test_quarantine_does_not_change_linked_nonblank_or_existing_review_rows(self):
        linked = self.complete(self.observe(None))
        normal_pending = self.observe()
        normal_retry = self.inbox.observe_notification(SENDER, TEXT + " retry", FINGERPRINT)
        normal_retry = self.inbox.mark_retryable(normal_retry.notification_key, "稍后重试")
        review = self.inbox.observe_notification(SENDER, TEXT + " review", "")
        with closing(sqlite3.connect(self.path)) as db:
            db.execute("UPDATE mention_candidates SET status='needs_review' WHERE notification_key=?",
                       (review.notification_key,))
            db.commit()
        before = self.inbox.list_candidates()
        events_before = self.inbox.list_events()
        self.assertEqual(self.inbox.quarantine_incomplete_candidates(), [])
        self.assertEqual(self.inbox.list_candidates(), before)
        self.assertEqual(self.inbox.list_events(), events_before)
        self.assertTrue(linked.event_key)
        self.assertEqual(normal_pending.status, "pending")
        self.assertEqual(normal_retry.status, "retryable")

    def test_explicit_quarantine_cannot_be_reactivated_by_stale_completion_or_retry(self):
        candidate = self.observe(None)
        saved = self.inbox.quarantine_incomplete_candidates()[0]
        reopened = SQLiteInbox(self.path)
        self.assertEqual(reopened.observe_notification(SENDER, TEXT, None), saved)
        self.assertEqual(reopened.mark_retryable(candidate.notification_key, "迟到的错误"), saved)
        self.assertEqual(reopened.complete_notification(candidate.notification_key, sender_profile_url=SENDER,
            video_id=VIDEO, command_text=TEXT, libraries=LIBRARIES), saved)
        self.assertEqual(reopened.complete_notification(candidate.notification_key, video_id=None), saved)
        self.assertEqual(reopened.list_events(), [])
        self.assertEqual(reopened.list_candidates(["pending", "retryable"]), [])
        # Another legitimate review decision is not made terminal by its status alone.
        normal = self.observe()
        with closing(sqlite3.connect(self.path)) as db:
            db.execute("UPDATE mention_candidates SET status='needs_review' WHERE notification_key=?",
                       (normal.notification_key,))
            db.commit()
        self.assertEqual(self.complete(normal).status, "received")
        self.assertEqual(len(reopened.list_events()), 1)

    def test_quarantine_audit_preserves_colliding_original_metadata(self):
        candidate = self.observe(None)
        previous = {"public_note": "保留原记录", "reason_code": "incomplete_notification_fingerprint_v1"}
        original = self.complete(candidate, video_id=None,
            extra_metadata={"incomplete_candidate_quarantine": previous})
        saved = self.inbox.quarantine_incomplete_candidates()[0]
        audit = saved.details["incomplete_candidate_quarantine"]
        self.assertEqual(audit["previous_value"], previous)
        self.assertEqual(saved.attempts, original.attempts)
        self.assertEqual(saved.last_error, original.last_error)

    def test_conflicting_event_for_same_candidate_does_not_replace_original(self):
        candidate = self.observe()
        original = self.complete(candidate)
        conflict = self.complete(candidate, video_id=VIDEO[:-1] + "2")
        self.assertEqual(conflict.status, "needs_review")
        self.assertEqual(conflict.event_key, original.event_key)
        self.assertEqual(conflict.details["conflicting_detail"]["video_id"], VIDEO[:-1] + "2")
        self.assertEqual(len(self.inbox.list_events()), 1)

    def test_unknown_notification_cannot_advance_status(self):
        with self.assertRaises(KeyError):
            self.inbox.mark_retryable("unknown", "error")
        with self.assertRaises(KeyError):
            self.inbox.complete_notification("unknown", sender_profile_url=SENDER, video_id=VIDEO,
                                             command_text=TEXT, libraries=LIBRARIES)
        self.assertEqual(self.inbox.list_events(), [])

    def test_failed_transaction_does_not_advance_candidate(self):
        candidate = self.observe()
        with closing(sqlite3.connect(self.path)) as db:
            db.execute("""CREATE TRIGGER deny_event BEFORE INSERT ON mention_events
                       BEGIN SELECT RAISE(ABORT, 'synthetic disk failure'); END""")
        with self.assertRaises(sqlite3.DatabaseError):
            self.complete(candidate)
        persisted = SQLiteInbox(self.path).list_candidates()[0]
        self.assertEqual(persisted.status, "pending")
        self.assertIsNone(persisted.event_key)
        self.assertEqual(self.inbox.list_events(), [])

    def test_two_concurrent_connections_cannot_duplicate_event(self):
        candidate = self.observe()
        def operation(_):
            return SQLiteInbox(self.path).complete_notification(candidate.notification_key,
                sender_profile_url=SENDER, video_id=VIDEO, command_text=TEXT, libraries=LIBRARIES)
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(operation, range(2)))
        self.assertEqual(results[0].event_key, results[1].event_key)
        self.assertEqual(len(self.inbox.list_events()), 1)

    def test_public_only_metadata_rejects_credentials(self):
        candidate = self.observe()
        with self.assertRaises(ValueError):
            self.complete(candidate, extra_metadata={"cookie": "never-store"})
        self.assertEqual(self.inbox.list_events(), [])


if __name__ == "__main__":
    unittest.main()
