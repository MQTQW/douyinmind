"""Synthetic pre-queue visibility; no account, browser, network or model."""
from contextlib import contextmanager
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import sqlite3
from types import SimpleNamespace

import pytest

from app.assistant.native_intake_visibility import (
    project_native_intake, record_native_intake, summarize_native_intake,
)

STAMP = "2026-01-02T12:00:00Z"


class OwnedFixture:
    def __init__(self, path):
        self.path = path
        self.reads = 0
        with sqlite3.connect(path) as db:
            db.execute("CREATE TABLE sources(source_id TEXT,state TEXT,hold_code TEXT,first_seen REAL,"
                       "candidate TEXT,historical INTEGER,task_id TEXT)")

    def add(self, letter, *, state="held", historical=0, task_id=None, reason="native_reply_parent_unresolved", text="请保存到合成资料库"):
        with sqlite3.connect(self.path) as db:
            db.execute("INSERT INTO sources VALUES(?,?,?,?,?,?,?)",
                ("native:" + letter * 64, state, reason, 1767355200.0,
                 json.dumps({"content": text, "sender_uid": "private-fixture-uid",
                             "cookie": "private-fixture-secret"}), historical, task_id))

    @contextmanager
    def _connection(self):
        self.reads += 1
        with sqlite3.connect(self.path.resolve().as_uri() + "?mode=ro", uri=True) as db:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA query_only=ON")
            yield db


def summary(**changes):
    result = {"available": True, "observed_at": STAMP, "unresolved_count": 1,
        "pending_count": 0, "recent": [{"id": "native:" + "a" * 64, "state": "held",
            "reason_code": "native_reply_parent_unresolved", "comment_excerpt": "合成回复",
            "created_at": STAMP}]}
    result.update(changes)
    return result


def test_projection_counts_only_unconfirmed_nonhistorical_sources_and_never_writes(tmp_path):
    store = OwnedFixture(tmp_path / "owned.sqlite3")
    store.add("a")
    store.add("b", state="held_conflict", reason="native_identity_conflict")
    store.add("c", historical=1)
    store.add("d", task_id="already-linked")
    for letter, state in zip("efg", ("fresh", "claimed", "planned")):
        store.add(letter, state=state)
    store.add("h", state="admitted", task_id="done")
    before = hashlib.sha256(store.path.read_bytes()).hexdigest()
    value = summarize_native_intake(store)
    assert value["unresolved_count"] == 2 and value["pending_count"] == 3
    assert len(value["recent"]) == 2
    assert {item["state"] for item in value["recent"]} == {"held", "held_conflict"}
    assert store.reads == 1
    assert hashlib.sha256(store.path.read_bytes()).hexdigest() == before
    encoded = json.dumps(value)
    assert "private-fixture" not in encoded and "sender_uid" not in encoded and "cookie" not in encoded
    assert "未调用模型" not in encoded


def test_recent_is_bounded_without_losing_total(tmp_path):
    store = OwnedFixture(tmp_path / "owned.sqlite3")
    for i in range(12):
        with sqlite3.connect(store.path) as db:
            db.execute("INSERT INTO sources VALUES(?,?,?,?,?,?,?)",
                ("native:" + format(i, "064x"), "held", "new_unknown_code", 1767355200.0 + i,
                 json.dumps({"content": "合成内容"}), 0, None))
    result = summarize_native_intake(store)
    assert result["unresolved_count"] == 12 and len(result["recent"]) == 10
    assert all(item["message"] == "原评论或受理条件需要核对。" for item in result["recent"])


def test_excerpt_removes_controls_and_limits_long_text(tmp_path):
    store = OwnedFixture(tmp_path / "owned.sqlite3")
    store.add("a", text="  合成\x00回复\n " + "很长" * 100)
    item = summarize_native_intake(store)["recent"][0]
    assert len(item["comment_excerpt"]) <= 160 and "\x00" not in item["comment_excerpt"]
    assert "\n" not in item["comment_excerpt"]


def test_unknown_extra_fields_cannot_escape_projection():
    raw = summary(secret="secret", plan={"authorization": "not-public"})
    raw["recent"][0].update(token="token", sender_profile_url="not-public", message="unsafe supplied message")
    result = project_native_intake(raw)
    assert set(result) == {"available", "observed_at", "unresolved_count", "pending_count", "recent"}
    assert result["recent"][0]["message"] == "无法确认这条回复对应的原线程。"
    assert not {"token", "sender_profile_url"} & result["recent"][0].keys()


@pytest.mark.parametrize("changes", [
    {"unresolved_count": True}, {"pending_count": -1}, {"observed_at": "not-a-valid-timestamp"},
    {"observed_at": "2026-01-02T12:00:00"}, {"recent": [] ,"unresolved_count": -1},
    {"recent": [summary()["recent"][0]] * 11}, {"recent": [{"id": "malformed", "state": "held"}]},
])
def test_invalid_snapshots_fail_closed(changes):
    with pytest.raises(ValueError):
        project_native_intake(summary(**changes))


def test_read_failure_is_unknown_not_zero_and_does_not_retry():
    class Broken:
        calls = 0
        @contextmanager
        def _connection(self):
            self.calls += 1
            raise PermissionError("private detail must not appear")
            yield
    broken = Broken()
    control = SimpleNamespace(set_native_intake=lambda run, value: project_native_intake(value))
    value = record_native_intake(control, "run", SimpleNamespace(source_store=broken))
    assert value["available"] is False and "unresolved_count" not in value
    assert "private detail" not in json.dumps(value) and broken.calls == 1


def test_non_native_lane_does_not_create_or_read_any_store():
    control = SimpleNamespace(set_native_intake=lambda *_: pytest.fail("must not record"))
    assert record_native_intake(control, "run", SimpleNamespace()) is None


def test_snapshot_updates_require_the_current_runtime_owner(tmp_path):
    from app.assistant.control import AssistantControl
    from app.assistant.queue import QueueError
    class Queue:
        path = tmp_path / "queue.sqlite3"
        value = {"run_id": "a" * 32, "desired_state": "running"}
        def update_control(self, change):
            self.value = change(deepcopy(self.value))
            return deepcopy(self.value)
    queue = Queue()
    control = AssistantControl(queue, tmp_path, process_probe=lambda _: False)
    with pytest.raises(QueueError, match="runtime_owner_changed"):
        control.set_native_intake("b" * 32, summary())
    assert "native_intake" not in queue.value
    value = control.set_native_intake("a" * 32, summary())
    assert value["native_intake"]["unresolved_count"] == 1
    assert value["desired_state"] == "running"


def test_api_returns_safe_snapshot_in_both_locations_without_a_store_getter(monkeypatch):
    from app.assistant import routes
    raw = summary(secret="do-not-expose")
    raw["recent"][0]["token"] = "do-not-expose"
    queue = SimpleNamespace(counts=lambda: {}, notice_deferral_summary=lambda: {"pending_rechecks": 0},
                            intake_issue_summary=lambda: {"unresolved_count": 0, "recent": []})
    control = SimpleNamespace(snapshot=lambda: {"native_intake": raw})
    monkeypatch.setattr(routes, "diagnostics", lambda *args, **kwargs: {})
    result = routes.AssistantAPI(queue, control).overview()
    assert result["intake_issues"]["unresolved_count"] == 0
    assert result["native_intake"]["unresolved_count"] == 1
    assert result["control"]["native_intake"] == result["native_intake"]
    assert "do-not-expose" not in json.dumps(result)
