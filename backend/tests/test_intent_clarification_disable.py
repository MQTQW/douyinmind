"""Capability disable/reenable only; synthetic sessions and temporary owned DBs."""
from copy import deepcopy
import json
import sqlite3

import pytest

from app.assistant import __main__ as entry
from app.assistant.clarification_sessions import (
    ClarificationSessionStore, ClarificationError, DATABASE_NAME,
)
from tests.test_intent_clarification_sessions import sample, response
from tests.test_intent_clarification_entry import configured


def test_disable_and_reenable_preserve_epoch_and_every_session_field(tmp_path):
    c = sample(tmp_path)
    epoch = c.store.activated_at
    before = c.store.get(c.session["session_id"])
    assert c.store.enabled is True
    assert c.store.set_enabled(False) is False
    assert c.store.enabled is False
    assert c.store.get(c.session["session_id"]) == before
    reopened = ClarificationSessionStore(c.store.path, clock=lambda: 2000)
    assert reopened.enabled is False and reopened.activated_at == epoch
    enabled = ClarificationSessionStore.prepare(c.store.path, enabled=True, clock=lambda: 2000)
    assert enabled.enabled is True and enabled.activated_at == epoch
    assert enabled.get(c.session["session_id"]) == before
    with enabled._db() as db:
        events = [row[0] for row in db.execute(
            "SELECT kind FROM clarification_events WHERE session_id='capability' ORDER BY id")]
    assert events == ["disabled", "enabled"]


@pytest.mark.parametrize("method", ["arm", "claim_response", "plan", "admitted"])
def test_store_transactions_reject_new_actions_after_hot_disable(tmp_path, method):
    c = sample(tmp_path)
    envelope = response(c)
    before = c.store.get(c.session["session_id"])
    c.store.set_enabled(False)
    task, source = deepcopy(c.task), deepcopy(c.source)
    task["id"] = "4" * 32
    source.update(task_id=task["id"], source_id="native:" + "e" * 64)
    calls = {
        "arm": lambda: c.store.arm(task, source, reason_code="semantic_intent_unclear",
            semantic_plan_id="semantic-" + "d" * 64),
        "claim_response": lambda: c.store.claim_response(c.session["session_id"], "native:" + "f" * 64, envelope),
        "plan": lambda: c.store.plan(c.session["session_id"], "native:" + "f" * 64,
            envelope, {"action": "save", "plan_id": "semantic-" + "e" * 64}, {}),
        "admitted": lambda: c.store.admitted(c.session["session_id"], "native:" + "f" * 64, task),
    }
    with pytest.raises(ClarificationError, match="feature_disabled"):
        calls[method]()
    assert c.store.get(c.session["session_id"]) == before
    assert c.store.for_root(task["id"]) is None


def test_disabled_store_still_reads_history_and_records_existing_reply_observation(tmp_path):
    c = sample(tmp_path)
    before = c.store.get(c.session["session_id"])
    c.store.set_enabled(False)
    assert c.store.for_root(c.task["id"]) == before
    assert c.store.published(c.session["session_id"], c.reply) == before
    assert c.store.enabled is False


@pytest.mark.parametrize("value", [None, 0, 1, "false", {}, []])
def test_set_enabled_requires_an_actual_boolean(tmp_path, value):
    c = sample(tmp_path)
    before = c.store.path.read_bytes()
    with pytest.raises(ClarificationError, match="enabled_invalid"):
        c.store.set_enabled(value)
    assert c.store.path.read_bytes() == before and c.store.enabled is True


@pytest.mark.parametrize("operation", ["delete", "invalid"])
def test_missing_or_corrupt_enabled_meta_fails_closed_without_repair(tmp_path, operation):
    c = sample(tmp_path)
    with sqlite3.connect(c.store.path) as db:
        if operation == "delete":
            db.execute("DELETE FROM clarification_meta WHERE key='enabled'")
        else:
            db.execute("UPDATE clarification_meta SET value='broken' WHERE key='enabled'")
    before = c.store.path.read_bytes()
    with pytest.raises(Exception):
        _ = c.store.enabled
    with pytest.raises(Exception):
        c.store.set_enabled(False)
    assert c.store.path.read_bytes() == before


def test_disable_absent_store_is_noop_without_queue_owner_or_directory(tmp_path, monkeypatch, capsys):
    state = tmp_path / "not-created"
    monkeypatch.setattr(entry, "STATE", state)
    monkeypatch.setattr(entry, "AssistantQueue", lambda *a, **k: pytest.fail("disable cannot construct queue"))
    monkeypatch.setattr(entry, "AssistantControl", lambda *a, **k: pytest.fail("disable cannot construct control"))
    monkeypatch.setattr(entry, "start_guard", lambda: pytest.fail("disable cannot require active login"))
    assert entry.main(["--state-dir", str(state), "--disable-intent-clarification"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["enabled"] is False and result["activated_at"] is None and result["started"] is False
    assert not state.exists()


def test_cli_disable_existing_store_preserves_session_then_normal_enable_reuses_epoch(configured, capsys):
    state, calls = configured
    c = sample(state)
    before, epoch = c.store.get(c.session["session_id"]), c.store.activated_at
    assert entry.main(["--state-dir", str(state), "--disable-intent-clarification"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["enabled"] is False and result["activated_at"] == epoch and result["started"] is False
    assert calls == [] and c.store.enabled is False
    assert c.store.get(c.session["session_id"]) == before
    assert entry.main(["--state-dir", str(state), "--enable-intent-clarification"]) == 0
    assert calls == ["queue", "guard"]
    assert ClarificationSessionStore(c.store.path).enabled is True
    assert ClarificationSessionStore(c.store.path).activated_at == epoch
    assert c.store.get(c.session["session_id"]) == before


@pytest.mark.parametrize("extra", [
    ["--enable-intent-clarification"], ["--run-registered"],
    ["--review-native-source", "native:" + "a" * 64],
    ["--review-native-source", ""], ["--inspect-thread-followup", ""],
    ["--candidate-sha256", ""], ["--finalize-query-draft", "1" * 32],
])
def test_disable_cannot_mix_with_activation_owner_review_or_sending(configured, extra):
    state, calls = configured
    with pytest.raises(SystemExit) as result:
        entry.main(["--state-dir", str(state), "--disable-intent-clarification", *extra])
    assert result.value.code == 2 and calls == [] and not (state / DATABASE_NAME).exists()


def test_disable_rejects_broken_marker_and_keeps_existing_database(configured):
    state, calls = configured
    c = sample(state)
    c.store.marker.write_text('{"component":"foreign","version":1}')
    before = c.store.path.read_bytes()
    assert entry.main(["--state-dir", str(state), "--disable-intent-clarification"]) == 1
    assert calls == [] and c.store.path.read_bytes() == before


def test_disable_does_not_adopt_a_foreign_database(configured):
    state, calls = configured
    path = state / DATABASE_NAME
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE unrelated(value TEXT)")
    before = path.read_bytes()
    assert entry.main(["--state-dir", str(state), "--disable-intent-clarification"]) == 1
    assert calls == [] and path.read_bytes() == before
