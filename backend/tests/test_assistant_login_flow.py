"""Web-triggered visible login flow: fakes only, no browser or process."""
from types import SimpleNamespace

from fastapi import HTTPException
import pytest

from app.assistant import login_flow, routes
from app.assistant.queue import AssistantQueue


class FakeControl:
    def __init__(self, states):
        self.states = list(states)
        self.calls = []

    def stop(self):
        self.calls.append("stop")

    def start(self):
        self.calls.append("start")

    def snapshot(self):
        return self.states.pop(0) if self.states else {"state": "stopped", "process_alive": False}


class FakeProcess:
    def __init__(self, marker, write):
        self.marker, self.write = marker, write

    def wait(self, timeout=None):
        if self.write:
            self.marker.write_text('{"ready": true}', encoding="utf-8")
        return 0


def make_flow(tmp_path, control, *, write_marker=True, spawn_calls=None, monkeypatch=None):
    marker = tmp_path / login_flow.MARKER_NAME

    def spawn(command, **kwargs):
        if spawn_calls is not None:
            spawn_calls.append((command, kwargs))
        return FakeProcess(marker, write_marker)

    if monkeypatch is not None:
        monkeypatch.setattr(login_flow.time, "sleep", lambda *_: None)
    return login_flow.LoginFlow(control, backend=tmp_path, receiver_state=tmp_path, spawn=spawn)


def test_flow_stops_opens_window_then_restarts(tmp_path, monkeypatch):
    control = FakeControl([{"state": "pausing", "process_alive": True},
                           {"state": "stopped", "process_alive": False}])
    spawn_calls = []
    flow = make_flow(tmp_path, control, spawn_calls=spawn_calls, monkeypatch=monkeypatch)
    assert flow.run_once() == "started"
    assert control.calls == ["stop", "start"]
    command, kwargs = spawn_calls[0]
    assert command[1:] == ["-m", "app.assistant.login_window", "--wait-close"]
    assert kwargs["cwd"] == str(tmp_path)
    assert flow.status()["stage"] == "started"
    assert not flow.status()["active"]


def test_flow_without_login_marker_does_not_restart(tmp_path, monkeypatch):
    control = FakeControl([{"state": "pausing", "process_alive": True},
                           {"state": "stopped", "process_alive": False}])
    flow = make_flow(tmp_path, control, write_marker=False, monkeypatch=monkeypatch)
    assert flow.run_once() == "login_not_finished"
    assert control.calls == ["stop"]
    assert flow.status()["stage"] == "login_not_finished"


def test_flow_stop_timeout_never_opens_window(tmp_path, monkeypatch):
    spawn_calls = []
    control = FakeControl([{"state": "pausing", "process_alive": True}])
    flow = make_flow(tmp_path, control, spawn_calls=spawn_calls, monkeypatch=monkeypatch)
    monkeypatch.setattr(flow, "_wait_stopped", lambda seconds=45: False)
    assert flow.run_once() == "stop_timeout"
    assert spawn_calls == []
    assert control.calls == ["stop"]


def test_flow_start_failure_is_reported_without_losing_login(tmp_path, monkeypatch):
    control = FakeControl([{"state": "stopped", "process_alive": False}])

    def fail():
        control.calls.append("start")
        raise RuntimeError("synthetic")
    control.start = fail
    flow = make_flow(tmp_path, control, monkeypatch=monkeypatch)
    assert flow.run_once() == "start_failed"
    assert control.calls == ["stop", "start"]
    assert flow.status()["stage"] == "start_failed"


class FakeFlow:
    def __init__(self, started):
        self.started = started
        self.calls = []

    def start(self):
        self.calls.append(True)
        return self.started

    def status(self):
        return {"active": False, "stage": "waiting_login", "message": "已打开"}


def test_endpoint_starts_flow_and_reports_fixed_conflicts():
    service = SimpleNamespace(login_flow=FakeFlow(True))
    value = routes.login_window(routes.EmptyBody(), service=service)
    assert value["success"] is True
    assert value["login_window"]["stage"] == "waiting_login"

    service.login_flow = FakeFlow(False)
    with pytest.raises(HTTPException) as busy:
        routes.login_window(routes.EmptyBody(), service=service)
    assert busy.value.status_code == 409
    assert busy.value.detail["code"] == "login_window_busy"

    service.login_flow = None
    with pytest.raises(HTTPException) as unavailable:
        routes.login_window(routes.EmptyBody(), service=service)
    assert unavailable.value.status_code == 503


def test_overview_projects_login_window_without_secrets(tmp_path):
    queue = AssistantQueue(tmp_path / "queue.sqlite3")
    control = SimpleNamespace(snapshot=lambda: {"state": "paused", "desired_state": "paused"})
    service = routes.AssistantAPI(queue, control, login_flow=FakeFlow(False))
    value = service.overview()
    assert value["login_window"] == {"active": False, "stage": "waiting_login", "message": "已打开"}
    assert "synthetic" not in str(value["login_window"])
