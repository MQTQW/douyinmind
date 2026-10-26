"""Rendered-page regressions without browser, credentials or project data.

The fake keeps locators lazy, so hovering the notification entry changes the
same locators' visible results. Hidden templates intentionally precede the
visible content, as they can on a single-page application.
"""
from dataclasses import dataclass
import re
import unittest

from playwright.sync_api import TimeoutError as PageTimeout

from app.mention_receiver.browser import (
    NormalPageReceiver,
    ReceiverInterventionRequired,
    ReceiverPageError,
)


@dataclass(eq=False)
class Node:
    text: str
    visible: object = True
    role: str | None = None
    level: int | None = None
    parent: "Node | None" = None
    interactive: bool = True

    def is_visible(self):
        return self.visible() if callable(self.visible) else self.visible


class FakeLocator:
    def __init__(self, page, query):
        self.page = page
        self.query = query

    def and_(self, other):
        return FakeLocator(self.page, lambda: [node for node in self.query() if node in other.query()])

    @property
    def first(self):
        return FakeLocator(self.page, lambda: self.query()[:1])

    def count(self):
        return len(self.query())

    def locator(self, selector):
        if selector != "..":
            raise AssertionError("unsupported relative fixture selector: " + selector)
        return FakeLocator(self.page, lambda: list(dict.fromkeys(
            node.parent for node in self.query() if node.parent is not None)))

    def is_visible(self):
        nodes = self.query()
        if len(nodes) > 1:
            raise AssertionError("strict locator matched multiple nodes")
        return bool(nodes) and nodes[0].is_visible()

    def wait_for(self, *, timeout, state="visible"):
        self.page.waits += 1
        if state != "visible":
            raise AssertionError("this regression fixture expects visible waits")
        if not self.is_visible() and self.page.pending_open_ms is not None:
            if self.page.pending_open_ms <= timeout:
                self.page.menu_open = True
                self.page.pending_open_ms = None
            else:
                self.page.pending_open_ms -= timeout
        if not self.is_visible():
            if self.page.challenge_on_panel_timeout:
                self.page.nodes.append(Node("身份验证", True))
                self.page.challenge_on_panel_timeout = False
            raise PageTimeout("matching content has not become visible")

    def hover(self, *, timeout=30000):
        if not self.is_visible():
            raise PageTimeout("hover target is not visible")
        node = self.query()[0]
        if not node.interactive:
            raise PageTimeout("the text child is not an actionable event target")
        self.page.actions.append(("hover", node.text))
        already_inside = self.page.pointer is node
        self.page.pointer = node
        if node is self.page.notice_button and not already_inside:
            self.page.button_entries += 1
            if self.page.ignored_entries:
                self.page.ignored_entries -= 1
                return
        if node is self.page.notice_button and not self.page.requires_click and not already_inside:
            if self.page.open_delay_ms:
                self.page.pending_open_ms = self.page.open_delay_ms
            else:
                self.page.menu_open = True

    def click(self, *, timeout=30000):
        if not self.is_visible():
            raise PageTimeout("click target is not visible")
        node = self.query()[0]
        if not node.interactive:
            raise PageTimeout("the text child is not an actionable event target")
        self.page.actions.append(("click", node.text))
        if node is self.page.notice_button:
            # On the affected UI, moving in opens the panel; clicking can
            # close it again. A hover-only opener must not rely on a click.
            self.page.menu_open = not self.page.menu_open

    def all_text_contents(self):
        return [node.text for node in self.query()]

    def inner_text(self):
        nodes = self.query()
        if len(nodes) != 1:
            raise AssertionError("expected one rendered text node")
        return nodes[0].text


class FakeHandle:
    def __init__(self, snapshot):
        self.snapshot = snapshot
        self.json_reads = 0
        self.disposed = False

    def json_value(self):
        if self.disposed:
            raise AssertionError("cannot read a disposed snapshot")
        self.json_reads += 1
        return self.snapshot

    def dispose(self):
        self.disposed = True


class FakePage:
    def __init__(self, *, menu_open=False, loaded=True, empty=False, challenge=False,
                 requires_click=False, text_interactive=True, pointer_at_button=False,
                 open_delay_ms=0, ignored_entries=0, challenge_on_panel_timeout=False):
        self.url = "https://www.douyin.com/user/self?from_tab_name=main"
        self.menu_open = menu_open
        self.loaded = loaded
        self.requires_click = requires_click
        self.open_delay_ms = open_delay_ms
        self.pending_open_ms = None
        self.ignored_entries = ignored_entries
        self.button_entries = 0
        self.challenge_on_panel_timeout = challenge_on_panel_timeout
        self.actions = []
        self.waits = 0
        self.evaluations = 0
        self.notification_waits = 0
        self.notification_frames = None
        self.polled_snapshots = []
        self.handles = []
        self.notice = {
            "pre_index": 1,
            "source_profile_url": "https://www.douyin.com/user/sender",
            "sender_display_name": "发送者",
            "exact_text": "@6 帮我把这个视频加入‘测试知识库’",
            "semantic_fingerprint": "/public-cover.jpg",
        }
        row_text = "暂无互动消息" if empty else "发送者 在评论中提到了你"
        self.empty = empty
        self.notice_button = Node("通知按钮", True, "button")
        self.pointer = self.notice_button if pointer_at_button else None
        self.nodes = [
            Node("身份验证", False),
            Node("抖音号：99999999", False),
            Node("旧主页", False, "heading", 1),
            Node("抖音号：41258822921"),
            Node("6", True, "heading", 1),
            self.notice_button,
            Node("通知", parent=self.notice_button, interactive=text_interactive),
            Node("互动消息", False),
            Node("互动消息", lambda: self.menu_open),
            Node("旧发送者 在评论中提到了你", False),
            Node(row_text, lambda: self.menu_open and self.loaded),
        ]
        if challenge:
            self.nodes.append(Node("身份验证", True))

    def get_by_text(self, text, *, exact=False):
        def matches(node):
            if isinstance(text, re.Pattern):
                return bool(text.search(node.text))
            return node.text == text if exact else text in node.text
        return FakeLocator(self, lambda: [node for node in self.nodes if matches(node)])

    def get_by_role(self, role, *, level=None):
        return FakeLocator(self, lambda: [node for node in self.nodes
            if node.role == role and (level is None or node.level == level)])

    def locator(self, selector):
        if selector != ":visible":
            raise AssertionError("unsupported fixture selector: " + selector)
        return FakeLocator(self, lambda: [node for node in self.nodes if node.is_visible()])

    def evaluate(self, _script):
        self.evaluations += 1
        if self.notification_frames is not None:
            # A separate evaluate would observe the first (possibly incomplete)
            # render instead of the ready snapshot returned by the wait.
            return self.notification_frames[0][1]
        return self.notification_snapshot()

    def notification_snapshot(self):
        return {
            "items": [] if self.empty or not self.loaded else [self.notice.copy()],
            "visible_mention_count": 0 if self.empty or not self.loaded else 1,
            "potential_mention_count": 0 if self.empty or not self.loaded else 1,
            "known_end_or_empty": self.empty and self.loaded,
        }

    def wait_for_function(self, _script, *, timeout):
        # Model the wait_for_function protocol using scripted JS results. The
        # test does not reimplement NOTICE_READY_JS or claim to execute the DOM.
        self.notification_waits += 1
        frames = self.notification_frames
        if frames is None:
            frames = [(self.loaded, self.notification_snapshot())]
        for ready, snapshot in frames:
            self.polled_snapshots.append(snapshot)
            if ready:
                handle = FakeHandle(snapshot)
                self.handles.append(handle)
                return handle
        raise PageTimeout("notification snapshot has not become complete")


class ReceiverVisibilityTests(unittest.TestCase):
    def receiver(self, page, **kwargs):
        return NormalPageReceiver(page, "41258822921", "https://www.douyin.com/user/receiver", **kwargs)

    def test_hidden_first_template_does_not_mask_visible_notification(self):
        page = FakePage(menu_open=True)
        self.assertEqual(self.receiver(page).notifications(), [page.notice])
        self.assertEqual(page.notification_waits, 1)
        self.assertEqual(page.evaluations, 0)

    def test_hover_targets_parent_button_when_text_child_cannot_receive_input(self):
        page = FakePage(text_interactive=False)
        self.assertEqual(self.receiver(page).notifications(), [page.notice])
        self.assertEqual(page.actions, [("hover", "6"), ("hover", "通知按钮")])

    def test_pointer_retained_over_button_is_moved_out_before_reentering(self):
        page = FakePage(pointer_at_button=True)
        self.assertEqual(self.receiver(page).notifications(), [page.notice])
        self.assertEqual(page.actions, [("hover", "6"), ("hover", "通知按钮")])
        self.assertIs(page.pointer, page.notice_button)

    def test_explicit_click_strategy_opens_click_only_variant_once(self):
        page = FakePage(requires_click=True)
        self.assertEqual(self.receiver(page, open_strategy="click").notifications(), [page.notice])
        self.assertEqual([action for action in page.actions if action[1] == "通知按钮"],
                         [("click", "通知按钮")])
        self.assertTrue(page.menu_open)

    def test_hover_opening_after_old_short_wait_is_awaited_without_extra_click(self):
        page = FakePage(open_delay_ms=2500)
        self.assertEqual(self.receiver(page).notifications(), [page.notice])
        self.assertEqual(page.actions, [("hover", "6"), ("hover", "通知按钮")])
        self.assertTrue(page.menu_open)
        self.assertIsNone(page.pending_open_ms)

    def test_hover_that_never_opens_stops_after_two_entries_without_click(self):
        page = FakePage(requires_click=True)
        receiver = self.receiver(page)
        with self.assertRaises(PageTimeout):
            receiver.notifications()
        self.assertEqual(page.actions, [("hover", "6"), ("hover", "通知按钮")] * 2)
        self.assertEqual(page.button_entries, 2)
        self.assertEqual(page.waits, 2)
        self.assertEqual(receiver.open_attempts, 2)
        self.assertEqual(receiver.open_retries, 1)
        self.assertFalse(page.menu_open)
        self.assertEqual(page.evaluations, 0)

    def test_first_entry_ignored_during_startup_recovers_with_one_same_strategy_retry(self):
        page = FakePage(ignored_entries=1)
        receiver = self.receiver(page)
        self.assertEqual(receiver.notifications(), [page.notice])
        self.assertEqual(page.actions, [("hover", "6"), ("hover", "通知按钮")] * 2)
        self.assertEqual(page.button_entries, 2)
        self.assertEqual(receiver.open_attempts, 2)
        self.assertEqual(receiver.open_retries, 1)
        receiver.begin_cycle()
        self.assertEqual(receiver.open_attempts, 0)
        self.assertEqual(receiver.open_retries, 0)

    def test_verification_appearing_during_first_wait_prevents_any_retry(self):
        page = FakePage(requires_click=True, challenge_on_panel_timeout=True)
        receiver = self.receiver(page)
        with self.assertRaises(ReceiverInterventionRequired):
            receiver.notifications()
        self.assertEqual(page.actions, [("hover", "6"), ("hover", "通知按钮")])
        self.assertEqual(receiver.open_attempts, 1)
        self.assertEqual(receiver.open_retries, 0)
        self.assertEqual(page.notification_waits, 0)

    def test_open_panel_is_read_without_retriggering_entry(self):
        page = FakePage(menu_open=True)
        self.receiver(page).notifications()
        self.assertEqual(page.actions, [])

    def test_visible_challenge_behind_hidden_template_stops_before_input(self):
        page = FakePage(challenge=True)
        with self.assertRaises(ReceiverInterventionRequired):
            self.receiver(page).notifications()
        self.assertEqual(page.actions, [])
        self.assertEqual(page.evaluations, 0)

    def test_loading_without_rows_or_end_marker_is_not_an_empty_inbox(self):
        page = FakePage(menu_open=True, loaded=False)
        with self.assertRaises(PageTimeout):
            self.receiver(page).notifications()
        self.assertEqual(page.evaluations, 0)

    def test_visible_explicit_empty_state_can_return_zero_notifications(self):
        page = FakePage(menu_open=True, empty=True)
        self.assertEqual(self.receiver(page).notifications(), [])
        self.assertEqual(page.notification_waits, 1)
        self.assertEqual(page.evaluations, 0)

    def test_panel_and_end_marker_do_not_skip_a_potential_unparsed_mention(self):
        page = FakePage(menu_open=True)
        incomplete = {"items": [], "visible_mention_count": 0,
                      "potential_mention_count": 1, "known_end_or_empty": True}
        complete = page.notification_snapshot()
        page.notification_frames = [(False, incomplete), (True, complete)]
        self.assertEqual(self.receiver(page).notifications(), [page.notice])
        self.assertEqual(page.polled_snapshots, [incomplete, complete])
        self.assertEqual(page.evaluations, 0)
        self.assertEqual(page.handles[0].json_reads, 1)
        self.assertTrue(page.handles[0].disposed)

    def test_potential_mention_that_never_finishes_times_out_instead_of_returning_empty(self):
        page = FakePage(menu_open=True)
        incomplete = {"items": [], "visible_mention_count": 0,
                      "potential_mention_count": 1, "known_end_or_empty": True}
        page.notification_frames = [(False, incomplete)]
        with self.assertRaises(PageTimeout):
            self.receiver(page).notifications()
        self.assertEqual(page.polled_snapshots, [incomplete])
        self.assertEqual(page.evaluations, 0)
        self.assertEqual(page.handles, [])

    def test_false_ready_snapshot_with_empty_cover_fingerprint_is_defensively_rejected(self):
        for fingerprint in ("", " \t ", None):
            with self.subTest(fingerprint=fingerprint):
                page = FakePage(menu_open=True)
                if fingerprint is None:
                    page.notice.pop("semantic_fingerprint")
                else:
                    page.notice["semantic_fingerprint"] = fingerprint
                # Deliberately bypass the JS readiness guard. The Python
                # adapter must still reject a malformed successful snapshot.
                with self.assertRaises(ReceiverPageError):
                    self.receiver(page).notifications()
                self.assertEqual(page.evaluations, 0)
                self.assertEqual(page.handles[0].json_reads, 1)
                self.assertTrue(page.handles[0].disposed)

    def test_identity_ignores_hidden_old_account_and_heading(self):
        page = FakePage()
        identity = self.receiver(page).identity()
        self.assertEqual(identity.public_number, "41258822921")
        self.assertEqual(identity.display_name, "6")
        self.assertEqual(page.actions, [])

    def test_visible_wrong_account_still_requires_personal_intervention(self):
        page = FakePage()
        page.nodes.append(Node("抖音号：88888888"))
        with self.assertRaises(ReceiverInterventionRequired):
            self.receiver(page).identity()
        self.assertEqual(page.actions, [])


if __name__ == "__main__":
    unittest.main()
