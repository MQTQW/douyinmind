"""Bounded original-target relookup used before publishing a reply."""
from __future__ import annotations

import unittest

from app.mention_receiver.browser import NormalPageReceiver, ReceiverDetailError
from app.mention_agent.publisher import ReplyPreflightError


def receiver_with(open_fn):
    receiver = object.__new__(NormalPageReceiver)
    receiver.open_original_from_notifications = open_fn
    return receiver


class TargetRelookupTests(unittest.TestCase):
    def test_native_target_uses_native_correlation_and_larger_window(self):
        calls = []

        def fake(target, *, max_candidates=3, native_candidate_correlation=False):
            calls.append((max_candidates, native_candidate_correlation))
            return {}

        receiver_with(fake)._open_target_for_publish({"native_comment_id": "1234567890123456789"})
        self.assertEqual(calls, [(8, True)])

    def test_dom_target_retries_the_two_candidate_failures_once(self):
        for reason in ("original_notification_candidates_exceed_limit",
                       "original_notification_not_unique"):
            with self.subTest(reason=reason):
                calls = []

                def fake(target, *, max_candidates=3, native_candidate_correlation=False):
                    calls.append(max_candidates)
                    if len(calls) == 1:
                        raise ReceiverDetailError("x", {"reason_code": reason})
                    return {}

                receiver_with(fake)._open_target_for_publish({"native_comment_id": None})
                self.assertEqual(calls, [3, 8])

    def test_dom_target_other_failures_are_not_retried(self):
        calls = []

        def fake(target, *, max_candidates=3, native_candidate_correlation=False):
            calls.append(max_candidates)
            raise ReceiverDetailError("x", {"reason_code": "original_target_changed"})

        with self.assertRaises(ReplyPreflightError) as raised:
            receiver_with(fake)._open_target_for_publish({"native_comment_id": None})
        self.assertEqual(str(raised.exception), "original_target_changed")
        self.assertEqual(calls, [3])

    def test_dom_retry_failure_keeps_the_bounded_reason(self):
        calls = []

        def fake(target, *, max_candidates=3, native_candidate_correlation=False):
            calls.append(max_candidates)
            raise ReceiverDetailError("x", {"reason_code": "original_notification_candidates_exceed_limit"})

        with self.assertRaises(ReplyPreflightError) as raised:
            receiver_with(fake)._open_target_for_publish({"native_comment_id": None})
        self.assertEqual(str(raised.exception), "original_notification_candidates_exceed_limit")
        self.assertEqual(calls, [3, 8])


if __name__ == "__main__":
    unittest.main()
