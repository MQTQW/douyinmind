"""Visible one-time login helper: no browser is started in these checks."""
from app.assistant import login_window


def _fake_control(snapshots):
    class Control:
        def __init__(self):
            self.calls = []
        def stop(self):
            self.calls.append("stop")
        def snapshot(self):
            self.calls.append("snapshot")
            return snapshots.pop(0) if snapshots else {"process_alive": True, "state": "paused"}
    return Control()


def test_refuses_when_operator_declines_auto_stop(monkeypatch, capsys):
    called = []
    monkeypatch.setattr(login_window, "assistant_busy", lambda: "assistant_running")
    monkeypatch.setattr(login_window, "request_stop", lambda *a, **k: called.append("stop") or True)
    monkeypatch.setattr(login_window, "run_login_window", lambda: called.append("run") or 0)
    monkeypatch.setattr("builtins.input", lambda *a: "n")
    assert login_window.main([]) == 2
    assert called == []
    assert "请先在项目网页点「停止」" in capsys.readouterr().out


def test_auto_stops_and_launches_window_on_default_answer(monkeypatch):
    calls = []
    monkeypatch.setattr(login_window, "assistant_busy", lambda: "assistant_running")
    monkeypatch.setattr(login_window, "request_stop", lambda *a, **k: calls.append("stop") or True)
    monkeypatch.setattr(login_window, "run_login_window", lambda *a, **k: calls.append("run") or 0)
    monkeypatch.setattr("builtins.input", lambda *a: "")
    monkeypatch.setattr(login_window.time, "sleep", lambda *_: None)
    assert login_window.main([]) == 0
    assert calls == ["stop", "run"]


def test_auto_stop_failure_does_not_open_the_window(monkeypatch, capsys):
    calls = []
    monkeypatch.setattr(login_window, "assistant_busy", lambda: "assistant_running")
    monkeypatch.setattr(login_window, "request_stop", lambda *a, **k: False)
    monkeypatch.setattr(login_window, "run_login_window", lambda *a, **k: calls.append("run") or 0)
    monkeypatch.setattr("builtins.input", lambda *a: "y")
    monkeypatch.setattr(login_window.time, "sleep", lambda *_: None)
    assert login_window.main([]) == 2
    assert calls == []
    assert "未能在限时内停止" in capsys.readouterr().out


def test_stopped_assistant_runs_window_directly(monkeypatch):
    calls = []
    monkeypatch.setattr(login_window, "assistant_busy", lambda: None)
    monkeypatch.setattr(login_window, "run_login_window", lambda *a, **k: calls.append("run") or 0)
    assert login_window.main([]) == 0
    assert calls == ["run"]


def test_stopped_assistant_is_not_reported_busy():
    class Control:
        def snapshot(self):
            return {"state": "stopped", "desired_state": "stopped", "process_alive": False}
    assert login_window.assistant_busy(lambda: Control()) is None


def test_running_or_starting_assistant_is_reported_busy():
    class Control:
        def __init__(self, value):
            self.value = value
        def snapshot(self):
            return dict(self.value)
    assert login_window.assistant_busy(lambda: Control(
        {"state": "paused", "process_alive": True})) == "assistant_running"
    assert login_window.assistant_busy(lambda: Control(
        {"state": "starting", "process_alive": False})) == "assistant_running"
    assert login_window.assistant_busy(lambda: Control(None)) is None


def test_request_stop_waits_until_process_is_released(monkeypatch):
    control = _fake_control([{"process_alive": True, "state": "pausing"},
                             {"process_alive": False, "state": "stopped"}])
    monkeypatch.setattr(login_window.time, "sleep", lambda *_: None)
    assert login_window.request_stop(5, control_factory=lambda: control) is True
    assert control.calls[0] == "stop"


def test_request_stop_times_out_without_release(monkeypatch):
    control = _fake_control([{"process_alive": True, "state": "pausing"}] * 20)
    ticks = iter([0.0, 0.1, 0.2, 0.3, 2.0, 2.0])
    monkeypatch.setattr(login_window.time, "sleep", lambda *_: None)
    monkeypatch.setattr(login_window.time, "monotonic", lambda: next(ticks, 3.0))
    assert login_window.request_stop(1, control_factory=lambda: control) is False


def test_request_stop_reports_start_failure_without_hanging(monkeypatch):
    control = _fake_control([{"process_alive": False, "state": "start_failed"}])
    monkeypatch.setattr(login_window.time, "sleep", lambda *_: None)
    assert login_window.request_stop(5, control_factory=lambda: control) is True


def test_protected_profile_path_is_absolute():
    assert login_window.protected_profile_path().is_absolute()
    assert login_window.LOGIN_URL.startswith("https://www.douyin.com/")


def test_main_returns_zero_with_ignored_state_and_fake_window(monkeypatch):
    monkeypatch.setattr(login_window, "assistant_busy", lambda: "assistant_running")
    monkeypatch.setattr(login_window, "run_login_window", lambda *a, **k: 0)
    assert login_window.main(["--ignore-assistant-state"]) == 0

def test_bring_login_window_to_front_is_best_effort():
    assert isinstance(login_window.bring_login_window_to_front(), bool)

def test_mark_and_clear_login_ready(tmp_path, monkeypatch):
    monkeypatch.setattr(login_window, "RECEIVER_STATE", tmp_path)
    login_window.mark_login_ready()
    assert login_window.login_ready_marked() is True
    assert (tmp_path / login_window.MARKER_NAME).is_file()
    login_window.clear_login_ready()
    assert login_window.login_ready_marked() is False


def test_probe_logged_in_accepts_only_the_self_page():
    class SelfPage:
        status, url = 200, "https://www.douyin.com/user/self?from_tab_name=main"
        def get(self, *args, **kwargs):
            return self
    class LoginPage:
        status, url = 200, "https://www.douyin.com/"
        def get(self, *args, **kwargs):
            return self
    class Broken:
        def get(self, *args, **kwargs):
            raise RuntimeError("synthetic")
    assert login_window.probe_logged_in(SelfPage()) is True
    assert login_window.probe_logged_in(LoginPage()) is False
    assert login_window.probe_logged_in(Broken()) is False
