"""Detail lifecycle with fake pages/handles; no browser or live account."""
from copy import deepcopy

import pytest
from playwright.sync_api import TimeoutError as PageTimeout

from app.mention_receiver.browser import (
    DETAIL_JS, DETAIL_READY_JS, NOTICE_JS, NormalPageReceiver,
    ReceiverDetailError, ReceiverInterventionRequired, ReceiverPageError,
)


A = "https://www.douyin.com/user/keep-open-fixture-A"
B = "https://www.douyin.com/user/keep-open-fixture-B"


def notice():
    return {"pre_index": 1, "source_profile_url": A,
            "exact_text": "@虚构B 请把这个视频加入“测试知识库”知识库。测试编号 DM-KEEP-OPEN。",
            "semantic_fingerprint": "/synthetic-cover.jpg"}


def detail_snapshot():
    return {"ok": True, "video_id": "7999999000000000001", "command_text": notice()["exact_text"],
            "diagnostics": {"command_match_count": 1}}


class FakeHandle:
    def __init__(self, page, value):
        self.page, self.value = page, value
        self.json_reads = self.dispose_calls = 0
        self.read_error = self.dispose_error = None
        self.on_read = None

    def json_value(self):
        self.page.events.append("read_ready_handle")
        self.json_reads += 1
        if self.on_read:
            self.on_read()
        if self.read_error:
            raise self.read_error
        return self.value

    def dispose(self):
        self.page.events.append("dispose_ready_handle")
        self.dispose_calls += 1
        if self.dispose_error:
            raise self.dispose_error


class FakePage:
    def __init__(self):
        self.events = []
        self.notices = [notice()]
        self.ready = detail_snapshot()
        self.fallback = detail_snapshot()
        self.handle = FakeHandle(self, self.ready)
        self.wait_error = self.click_error = None
        self.on_wait = None
        self.challenge = False
        self.clicks, self.keys, self.waits, self.detail_evaluations = [], [], [], []
        self.keyboard = self

    def evaluate(self, script, *args):
        if script == NOTICE_JS:
            self.events.append("read_live_notice_rows")
            return {"items": deepcopy(self.notices)}
        if script == DETAIL_JS:
            self.events.append("read_fallback_detail")
            self.detail_evaluations.append(args)
            return self.fallback
        raise AssertionError("unrecognized synthetic evaluation")

    def locator(self, selector):
        assert selector == "pre"
        page = self

        class Locator:
            def nth(self, index):
                self.index = index
                return self

            def click(self, *, timeout):
                page.events.append("click_notice")
                page.clicks.append(self.index)
                if page.click_error:
                    raise page.click_error

        return Locator()

    def wait_for_function(self, script, *, arg, timeout):
        assert script == DETAIL_READY_JS
        self.events.append("wait_detail_ready")
        self.waits.append((arg, timeout))
        if self.on_wait:
            self.on_wait()
        if self.wait_error:
            raise self.wait_error
        return self.handle

    def press(self, key):
        self.events.append("press_" + key)
        assert not self.challenge, "authentication UI must never receive Escape"
        self.keys.append(key)


@pytest.fixture
def context(monkeypatch):
    page = FakePage()
    receiver = NormalPageReceiver(page, "41258822921", B)

    def check_challenge():
        page.events.append("check_challenge")
        if page.challenge:
            raise ReceiverInterventionRequired("synthetic human verification")

    monkeypatch.setattr(receiver, "_check_challenge", check_challenge)
    return receiver, page


@pytest.mark.parametrize("options", [{}, {"keep_open": False}, {"keep_open": True}])
def test_ready_snapshot_is_read_once_disposed_and_only_default_closes(context, options):
    receiver, page = context
    expected = deepcopy(page.ready)
    assert receiver.detail(notice(), **options) == expected
    assert page.handle.json_reads == page.handle.dispose_calls == 1
    assert page.detail_evaluations == [], "do not replace the ready snapshot with a later transition frame"
    assert page.keys == ([] if options.get("keep_open", False) else ["Escape"])
    assert "check_challenge" in page.events, "keep_open must retain the verification check"
    if page.keys:
        assert page.events.index("check_challenge") < page.events.index("press_Escape")


@pytest.mark.parametrize("keep_open", [False, True])
def test_same_notice_reacquires_current_index_instead_of_clicking_old_position(context, keep_open):
    receiver, page = context
    page.notices[0]["pre_index"] = 9
    receiver.detail(notice(), keep_open=keep_open)
    assert page.clicks == [9]
    assert page.waits[0][0] == {"sender": A, "wanted": notice()["exact_text"], "receiver": B}


@pytest.mark.parametrize("change", ["sender", "body", "fingerprint", "missing", "duplicate"])
@pytest.mark.parametrize("keep_open", [False, True])
def test_drifted_or_duplicate_notice_never_clicks_or_closes_unrelated_page(context, change, keep_open):
    receiver, page = context
    if change == "sender":
        page.notices[0]["source_profile_url"] = "https://www.douyin.com/user/someone-else"
    elif change == "body":
        page.notices[0]["exact_text"] += " 已经改变"
    elif change == "fingerprint":
        page.notices[0]["semantic_fingerprint"] = "/different-video-cover.jpg"
    elif change == "missing":
        page.notices = []
    else:
        page.notices.append({**notice(), "pre_index": 8})
    with pytest.raises(ReceiverPageError):
        receiver.detail(notice(), keep_open=keep_open)
    assert page.clicks == page.keys == page.waits == []
    assert page.handle.json_reads == page.handle.dispose_calls == 0


@pytest.mark.parametrize("keep_open", [False, True])
def test_wait_timeout_with_valid_fallback_obeys_same_close_policy(context, keep_open):
    receiver, page = context
    page.wait_error = PageTimeout("synthetic detail hydration timeout")
    result = receiver.detail(notice(), keep_open=keep_open)
    assert result == page.fallback
    assert len(page.detail_evaluations) == 1
    assert page.events.index("check_challenge") < page.events.index("read_fallback_detail")
    assert page.keys == ([] if keep_open else ["Escape"])
    assert page.handle.dispose_calls == 0, "no handle was returned by the failed wait"


@pytest.mark.parametrize("keep_open", [False, True])
def test_wait_timeout_with_invalid_fallback_raises_without_false_evidence(context, keep_open):
    receiver, page = context
    page.wait_error = PageTimeout("synthetic timeout")
    page.fallback = {"ok": False, "reason": "synthetic original not unique", "diagnostics": {"command_match_count": 2}}
    with pytest.raises(ReceiverDetailError) as captured:
        receiver.detail(notice(), keep_open=keep_open)
    assert captured.value.diagnostics == {"command_match_count": 2}
    assert page.keys == ([] if keep_open else ["Escape"])


@pytest.mark.parametrize("keep_open", [False, True])
def test_json_read_failure_still_disposes_handle_and_respects_keep_open(context, keep_open):
    receiver, page = context
    page.handle.read_error = ValueError("synthetic snapshot decoding failure")
    with pytest.raises(ValueError, match="snapshot decoding"):
        receiver.detail(notice(), keep_open=keep_open)
    assert page.handle.json_reads == page.handle.dispose_calls == 1
    assert page.detail_evaluations == []
    assert page.keys == ([] if keep_open else ["Escape"])


@pytest.mark.parametrize("keep_open", [False, True])
def test_dispose_failure_never_causes_a_second_read_or_notice_click(context, keep_open):
    receiver, page = context
    page.handle.dispose_error = RuntimeError("synthetic handle disposal failure")
    with pytest.raises(RuntimeError, match="handle disposal"):
        receiver.detail(notice(), keep_open=keep_open)
    assert page.handle.json_reads == page.handle.dispose_calls == 1
    assert len(page.clicks) == 1
    assert page.keys == ([] if keep_open else ["Escape"])


@pytest.mark.parametrize("keep_open", [False, True])
def test_challenge_appearing_during_wait_prevents_fallback_and_escape(context, keep_open):
    receiver, page = context
    page.wait_error = PageTimeout("synthetic timeout with verification")
    page.on_wait = lambda: setattr(page, "challenge", True)
    with pytest.raises(ReceiverInterventionRequired):
        receiver.detail(notice(), keep_open=keep_open)
    assert page.detail_evaluations == []
    assert page.keys == []


@pytest.mark.parametrize("keep_open", [False, True])
def test_challenge_appearing_after_ready_read_is_not_closed_or_returned_as_success(context, keep_open):
    receiver, page = context
    page.handle.on_read = lambda: setattr(page, "challenge", True)
    with pytest.raises(ReceiverInterventionRequired):
        receiver.detail(notice(), keep_open=keep_open)
    assert page.handle.json_reads == page.handle.dispose_calls == 1
    assert page.keys == []


@pytest.mark.parametrize("keep_open", [False, True])
def test_existing_challenge_prevents_notice_input_itself(context, keep_open):
    receiver, page = context
    page.challenge = True
    with pytest.raises(ReceiverInterventionRequired):
        receiver.detail(notice(), keep_open=keep_open)
    assert page.clicks == [], "fresh detail access must check verification before clicking a notification"
    assert page.keys == []


def test_notice_click_failure_is_not_retried_and_does_not_send_global_escape(context):
    receiver, page = context
    page.click_error = PageTimeout("synthetic notice click timeout")
    with pytest.raises(PageTimeout):
        receiver.detail(notice(), keep_open=True)
    assert len(page.clicks) == 1
    assert page.keys == page.waits == []
