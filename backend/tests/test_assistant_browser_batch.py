"""Bounded visible-notification intake: fake UI + temporary ledgers only."""
from copy import deepcopy
from types import SimpleNamespace
import threading

import pytest

from app.assistant.__main__ import receive_bounded_batch, worker_health_state
from app.assistant.browser_lane import BrowserLane, notice_fingerprint
from app.assistant.conversation import ConversationStore
from app.assistant.queue import AssistantQueue
from app.assistant.registry import Registry
from app.assistant.runtime import AssistantRuntime
from app.mention_agent.contracts import Binding
from app.mention_receiver.browser import ReceiverDetailError, ReceiverInterventionRequired


class Page:
    def __init__(self, events):
        self.events = events
        self.keyboard = SimpleNamespace(press=lambda key: events.append(("key", key)))
    def goto(self, url, **kwargs): self.events.append(("goto", url))


class Adapter:
    def __init__(self, binding, events):
        self.expected_profile_url = binding.receiver_profile_url
        self.events, self.page = events, Page(events)
        self.notices, self.opens = [], []
        self.challenge = False
        self.fail_open = None
    def begin_cycle(self): self.events.append(("begin", None))
    def check_intervention(self):
        if self.challenge: raise ReceiverInterventionRequired("synthetic human verification")
    def identity(self): self.events.append(("identity", None)); return object()
    def notifications(self): self.events.append(("notifications", None)); return deepcopy(self.notices)
    def conversation_reference(self, envelope, roots):
        return {"quality": "current_comment", "root_reference_id": envelope["reference_id"]}
    def detail(self, notice, *, keep_open=False):
        self.check_intervention()
        self.opens.append(notice["semantic_fingerprint"])
        self.events.append(("detail", notice["semantic_fingerprint"]))
        if len(self.opens) == self.fail_open: raise ReceiverDetailError("synthetic row drift", {})
        if notice not in self.notices: raise ReceiverDetailError("synthetic row vanished", {})
        proof = {"sender_profile_url": notice["source_profile_url"], "receiver_profile_url": self.expected_profile_url,
                 "command_text": notice["exact_text"], "native_comment_id": None, "unique_count": 1,
                 "video_id": "7999999000000000033", "real_mention": True, "sender_verified": True, "video_verified": True}
        return {"ok": True, **proof, "evidence": proof, "video_title": "隔离批量测试视频",
                "source_url": "https://www.douyin.com/video/7999999000000000033",
                "diagnostics": {"id_conflict": False, "command_match_count": 1, "visible_video_ids": [proof["video_id"]]}}


def setup(tmp_path):
    binding = Binding.from_dict({"binding_id": "batch-fixture", "instance_id": "batch-local",
        "sender_profile_url": "https://www.douyin.com/user/batch-A", "receiver_profile_url": "https://www.douyin.com/user/batch-B",
        "knowledge_base_id": 7, "public_reply_allowed": True, "active": True})
    registry = Registry(tmp_path / "registry.sqlite3", catalog_reader=lambda: [{"id": 7, "name": "测试库"}])
    registry.import_legacy(binding, verified_by="synthetic-owner", allow_import=True)
    queue, history = AssistantQueue(tmp_path / "queue.sqlite3"), ConversationStore(tmp_path / "conversation.sqlite3")
    events = []
    adapter = Adapter(binding, events)
    lane = BrowserLane(adapter, registry, queue, history)
    assert lane.collect() == []  # Baseline established from an empty verified snapshot.
    events.clear()
    control = SimpleNamespace(desired="running")
    control.snapshot = lambda: {"desired_state": control.desired, "run_id": "batch-run"}
    lane.should_stop = lambda: control.desired == "stopped"
    runtime = AssistantRuntime(queue, control, intake=lane.collect)
    return binding, registry, queue, history, events, adapter, lane, control, runtime


def notice(binding, n, *, sender=None):
    return {"source_profile_url": sender or binding.sender_profile_url,
            "exact_text": f"@助手 解释Git {n}", "semantic_fingerprint": f"batch-cover-{n}"}


def capture(runtime, lane, control):
    return receive_bounded_batch(runtime, lane, threading.RLock(), lambda: control.desired == "stopped")


def test_three_admissions_use_one_navigation_and_commit_thread_before_next(tmp_path):
    b, r, q, h, events, a, lane, c, runtime = setup(tmp_path)
    a.notices = [notice(b, n) for n in (4, 3, 2, 1)]
    original = a.detail
    def detail(item, **kwargs):
        tasks = q.list_tasks()["items"]
        assert all(h.task_scope(task["id"]) is not None for task in tasks)
        return original(item, **kwargs)
    a.detail = detail
    tasks = capture(runtime, lane, c)
    assert len(tasks) == 3 and q.list_tasks()["total"] == 3
    assert a.opens == ["batch-cover-1", "batch-cover-2", "batch-cover-3"]
    assert len([e for e in events if e[0] == "goto"]) == 1
    assert len([e for e in events if e[0] == "begin"]) == 1
    assert len([e for e in events if e[0] == "identity"]) >= 3
    assert not lane.has_buffered_notices and lane.pending == {}
    assert not q.has_seen_notice(lane.capture_id, notice_fingerprint(notice(b, 4)))
    capture(runtime, lane, c)
    assert q.list_tasks()["total"] == 4
    assert len([e for e in events if e[0] == "goto"]) == 2


def test_second_detail_failure_preserves_first_and_allows_third_without_marking_failure_seen(tmp_path):
    b, r, q, h, events, a, lane, c, runtime = setup(tmp_path)
    a.notices = [notice(b, n) for n in (3, 2, 1)]
    a.fail_open = 2
    capture(runtime, lane, c)
    task = q.list_tasks()["items"][0]
    assert q.list_tasks()["total"] == 2 and h.task_scope(task["id"]) is not None
    assert q.has_seen_notice(lane.capture_id, notice_fingerprint(notice(b, 1)))
    assert not q.has_seen_notice(lane.capture_id, notice_fingerprint(notice(b, 2)))
    assert q.has_seen_notice(lane.capture_id, notice_fingerprint(notice(b, 3)))
    assert q.notice_is_deferred(lane.capture_id, notice_fingerprint(notice(b, 2)))
    assert lane.last_detail_failure_count == 1
    assert not lane.has_buffered_notices and lane.pending == {}
    assert len([e for e in events if e[0] == "goto"]) == 1


def test_snapshot_does_not_adopt_a_new_candidate_from_later_panel_reopen(tmp_path):
    b, r, q, h, events, a, lane, c, runtime = setup(tmp_path)
    a.notices = [notice(b, n) for n in (3, 2, 1)]
    original = lane.admitted
    def admitted(tasks):
        original(tasks)
        if tasks and len(a.opens) == 1: a.notices.insert(0, notice(b, 99))
    lane.admitted = admitted
    capture(runtime, lane, c)
    assert a.opens == ["batch-cover-1", "batch-cover-2", "batch-cover-3"]
    assert not q.has_seen_notice(lane.capture_id, notice_fingerprint(notice(b, 99)))


def test_unverified_sender_is_never_opened_and_does_not_consume_admission_slot(tmp_path):
    b, r, q, h, events, a, lane, c, runtime = setup(tmp_path)
    a.notices = [notice(b, 3), notice(b, 2), notice(b, 1), notice(b, 0, sender="https://www.douyin.com/user/unknown")]
    assert len(capture(runtime, lane, c)) == 3
    assert "batch-cover-0" not in a.opens


@pytest.mark.parametrize("mode", ["stop", "challenge"])
def test_stop_or_verification_after_first_admission_never_opens_next(tmp_path, mode):
    b, r, q, h, events, a, lane, c, runtime = setup(tmp_path)
    a.notices = [notice(b, n) for n in (3, 2, 1)]
    original = lane.admitted
    def admitted(tasks):
        original(tasks)
        if tasks:
            if mode == "stop": c.desired = "stopped"
            else: a.challenge = True
    lane.admitted = admitted
    if mode == "challenge":
        with pytest.raises(ReceiverInterventionRequired): capture(runtime, lane, c)
    else: capture(runtime, lane, c)
    assert a.opens == ["batch-cover-1"] and q.list_tasks()["total"] == 1
    assert h.task_scope(q.list_tasks()["items"][0]["id"]) is not None
    assert [e for e in events if e[0] == "key"] == [("key", "Escape")]
    assert not lane.has_buffered_notices and lane.pending == {}


def test_stop_before_capture_performs_no_ui_navigation(tmp_path):
    b, r, q, h, events, a, lane, c, runtime = setup(tmp_path)
    a.notices = [notice(b, 1)]; c.desired = "stopped"
    assert capture(runtime, lane, c) == [] and events == [] and a.opens == []


def test_stop_during_snapshot_never_opens_a_detail_or_marks_it_seen(tmp_path):
    b, r, q, h, events, a, lane, c, runtime = setup(tmp_path)
    a.notices = [notice(b, 1)]
    original = a.notifications
    def stop_after_read():
        values = original(); c.desired = "stopped"; return values
    a.notifications = stop_after_read
    assert capture(runtime, lane, c) == [] and a.opens == []
    assert q.list_tasks()["total"] == 0
    assert not q.has_seen_notice(lane.capture_id, notice_fingerprint(notice(b, 1)))


def test_challenge_after_detail_read_retains_scene_and_no_unadmitted_seen_marker(tmp_path):
    b, r, q, h, events, a, lane, c, runtime = setup(tmp_path)
    a.notices = [notice(b, 2), notice(b, 1)]
    original = a.detail
    def challenged(item, **kwargs):
        result = original(item, **kwargs); a.challenge = True; return result
    a.detail = challenged
    with pytest.raises(ReceiverInterventionRequired): capture(runtime, lane, c)
    assert a.opens == ["batch-cover-1"] and q.list_tasks()["total"] == 0
    assert not any(event[0] == "key" for event in events)
    assert not q.has_seen_notice(lane.capture_id, notice_fingerprint(notice(b, 1)))
    assert not lane.has_buffered_notices and lane.pending == {}


def test_sender_revoke_between_items_prevents_opening_buffered_notices(tmp_path):
    b, r, q, h, events, a, lane, c, runtime = setup(tmp_path)
    a.notices = [notice(b, n) for n in (3, 2, 1)]
    original = lane.admitted
    user = r.find_user(b.sender_profile_url, b.receiver_profile_url)
    def admitted(tasks):
        original(tasks)
        if tasks: r.revoke_user(user["user_id"])
    lane.admitted = admitted
    capture(runtime, lane, c)
    assert a.opens == ["batch-cover-1"] and q.list_tasks()["total"] == 1
    assert h.task_scope(q.list_tasks()["items"][0]["id"]) is not None


def test_pending_proof_must_be_admitted_before_next_detail(tmp_path):
    b, r, q, h, events, a, lane, c, runtime = setup(tmp_path)
    a.notices = [notice(b, 2), notice(b, 1)]
    rows = lane.collect()
    with pytest.raises(ValueError, match="prior_notice_admission_required"): lane.collect()
    assert a.opens == ["batch-cover-1"]
    task = q.enqueue(**rows[0]); lane.admitted([task])
    assert h.task_scope(task["id"]) is not None
    assert len(lane.collect()) == 1


def test_possible_selection_with_unproven_parent_is_deferred_not_qa_or_seen(tmp_path):
    """A frame swap must not turn an uncertain selection into a paid QA task."""
    b, r, q, h, events, a, lane, c, runtime = setup(tmp_path)
    candidate = notice(b, 1)
    a.notices = [candidate]
    inspected = []

    class UncertainChoice:
        def collect_pending(self, adapter, run_id):
            return []
        def known_selection_notice(self, item, detail, adapter):
            inspected.append((item, detail))
            return False
        def selection_notice_requires_recheck(self, item, detail):
            assert item == candidate and detail['ok'] is True
            return True
        def admitted(self, tasks):
            assert tasks == []
        def clear_buffer(self):
            pass

    lane.choice_controller = UncertainChoice()
    assert capture(runtime, lane, c) == []
    fingerprint = notice_fingerprint(candidate)
    assert len(inspected) == 1
    assert q.list_tasks()['total'] == 0
    assert not q.has_seen_notice(lane.capture_id, fingerprint)
    assert q.notice_is_deferred(lane.capture_id, fingerprint)
    assert lane.last_detail_failure_count == 1
    assert not lane.pending and not lane.has_buffered_notices
    # Only the ordinary read-and-close operations ran; no execution callback
    # or task exists for a model to claim, and an immediate cycle cannot retry.
    assert a.opens == ['batch-cover-1']
    assert capture(runtime, lane, c) == []
    assert a.opens == ['batch-cover-1']
    assert not q.has_seen_notice(lane.capture_id, fingerprint)


@pytest.mark.parametrize("desired,counts,expected", [
    ("paused", {"queued": 7, "waiting_ingestion": 2}, "idle"),
    ("stopped", {"queued": 7}, "idle"),
    ("running", {"queued": 1}, "processing"),
    ("running", {"waiting_ingestion": 1}, "processing"),
    ("running", {"ready_to_publish": 1}, "idle"),
])
def test_worker_health_respects_pause_and_the_tools_lane(desired, counts, expected):
    assert worker_health_state({"desired_state": desired}, counts) == expected
