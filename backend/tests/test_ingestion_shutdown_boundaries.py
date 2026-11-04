"""Execute real lifecycle/launcher methods with inert workers and child handles.

The strict isolated harness may copy the public root launcher, byte-for-byte, to
app/_launcher_shutdown_source.py. Normal repository tests use start-local.py.
AST selection avoids importing the live FastAPI application or Windows job API.
"""
import ast
import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
from types import ModuleType, SimpleNamespace
import sys

import pytest


def complete_immediate(awaitable):
    """Drive a fully fake async adapter without creating an event-loop socket."""
    try:
        awaitable.send(None)
    except StopIteration as done:
        return done.value
    finally:
        awaitable.close()
    pytest.fail("fake lifecycle unexpectedly requested real async I/O")


def lifespan_case(monkeypatch, outcome, *, failure=None, dispose_failure=None):
    events, logs = [], []
    class FakeWorker:
        def start(self):
            events.append(("start",))
        def stop(self, *, timeout):
            events.append(("stop", timeout))
            if failure is not None:
                raise failure
            return outcome
    class FakeEngine:
        def dispose(self):
            events.append(("dispose",))
            if dispose_failure is not None:
                raise dispose_failure
    fake_worker = FakeWorker()
    worker_module = ModuleType("app.services.worker")
    worker_module.worker = fake_worker
    monkeypatch.setitem(sys.modules, "app.services.worker", worker_module)

    async def to_thread(function, *args, **kwargs):
        assert function.__self__ is fake_worker
        events.append(("to_thread",))
        return function(*args, **kwargs)
    monkeypatch.setattr(asyncio, "to_thread", to_thread)
    source = Path(__file__).resolve().parents[1] / "app" / "main.py"
    function = next(node for node in ast.parse(source.read_text(encoding="utf-8")).body
                    if isinstance(node, ast.AsyncFunctionDef) and node.name == "lifespan")
    namespace = {
        "asynccontextmanager": asynccontextmanager, "FastAPI": object,
        "logger": SimpleNamespace(info=lambda text: logs.append(("info", text)),
                                   warning=lambda text: logs.append(("warning", text))),
        "os": SimpleNamespace(makedirs=lambda *args, **kwargs: None),
        "settings": SimpleNamespace(chroma_persist_dir="synthetic-chroma", audio_cache_dir="synthetic-audio",
            video_cache_dir="synthetic-video", playwright_user_data_dir="synthetic-profile",
            playwright_browsers_path="synthetic-browser"),
        "setup_logging": lambda: None, "engine": FakeEngine(),
        "Base": SimpleNamespace(metadata=SimpleNamespace(create_all=lambda **kwargs: None)),
        "ensure_video_analysis_columns": lambda *_: None,
        "ensure_chat_knowledge_base_column": lambda *_: None,
        "ensure_chat_mode_column": lambda *_: events.append(("chat_mode_migration",)),
        "ensure_knowledge_base_tombstones": lambda *_: None,
    }
    exec(compile(ast.Module(body=[function], type_ignores=[]), str(source), "exec"), namespace)
    context = namespace["lifespan"](object())
    complete_immediate(context.__aenter__())
    return context, events, logs


def test_lifespan_waits_for_worker_before_disposing_and_preserves_chat_mode_startup(monkeypatch):
    context, events, logs = lifespan_case(monkeypatch, {"stopped": True, "timed_out": False})
    complete_immediate(context.__aexit__(None, None, None))
    assert ("chat_mode_migration",) in events
    assert events.index(("to_thread",)) < events.index(("stop", 30.0)) < events.index(("dispose",))
    assert not any(level == "warning" for level, text in logs)


@pytest.mark.parametrize("outcome", [None, True, {}, {"stopped": False, "timed_out": True},
    {"stopped": True, "timed_out": True}])
def test_unconfirmed_or_timeout_shutdown_keeps_business_engine_and_warns(monkeypatch, outcome):
    context, events, logs = lifespan_case(monkeypatch, outcome)
    complete_immediate(context.__aexit__(None, None, None))
    assert ("stop", 30.0) in events and ("dispose",) not in events
    assert any(level == "warning" and "未确认停止" in text for level, text in logs)
    assert not any("处理完成" in text for level, text in logs)


def test_stopped_worker_with_unconfirmed_state_is_not_reported_as_completed_work(monkeypatch):
    context, events, logs = lifespan_case(monkeypatch,
        {"stopped": True, "timed_out": False, "state_recorded": False})
    complete_immediate(context.__aexit__(None, None, None))
    assert ("dispose",) in events
    assert any(level == "warning" and "状态写入未确认" in text for level, text in logs)
    assert not any("处理完成" in text for level, text in logs)


def test_shutdown_exception_does_not_dispose_or_expose_raw_provider_details(monkeypatch):
    context, events, logs = lifespan_case(monkeypatch, None,
        failure=RuntimeError("private-token https://secret.invalid/private"))
    complete_immediate(context.__aexit__(None, None, None))
    assert ("dispose",) not in events
    assert any(level == "warning" for level, text in logs)
    assert all("private-token" not in text and "secret.invalid" not in text for level, text in logs)


def test_exceptional_lifespan_exit_still_waits_for_current_worker(monkeypatch):
    context, events, logs = lifespan_case(monkeypatch, {"stopped": True, "timed_out": False})
    error = RuntimeError("synthetic request failure")
    assert complete_immediate(context.__aexit__(RuntimeError, error, None)) is False
    assert ("stop", 30.0) in events and ("dispose",) in events


def test_dispose_failure_uses_a_fixed_warning_without_false_success(monkeypatch):
    context, events, logs = lifespan_case(monkeypatch, {"stopped": True},
        dispose_failure=RuntimeError("private-database-path"))
    complete_immediate(context.__aexit__(None, None, None))
    assert any(level == "warning" and "连接释放结果未确认" in text for level, text in logs)
    assert not any("private-database-path" in text or "连接已释放" in text for level, text in logs)


def launcher_stop(platform_name="nt"):
    backend = Path(__file__).resolve().parents[1]
    snapshot = backend / "app" / "_launcher_shutdown_source.py"
    source = snapshot if snapshot.exists() else backend.parent / "start-local.py"
    if not source.is_file():
        pytest.skip("strict launcher tests require a byte-identical public root-script snapshot")
    class_node = next(node for node in ast.parse(source.read_text(encoding="utf-8-sig")).body
                      if isinstance(node, ast.ClassDef) and node.name == "LocalSupervisor")
    function = next(node for node in class_node.body if isinstance(node, ast.FunctionDef) and node.name == "stop")
    class TimeoutExpired(Exception):
        pass
    namespace = {"os": SimpleNamespace(name=platform_name), "signal": SimpleNamespace(CTRL_BREAK_EVENT="fake-break"),
                 "subprocess": SimpleNamespace(TimeoutExpired=TimeoutExpired)}
    exec(compile(ast.Module(body=[function], type_ignores=[]), str(source), "exec"), namespace)
    return namespace["stop"], TimeoutExpired


class FakeChild:
    def __init__(self, *, wait_error=None, signal_error=None, exited=False):
        self.calls = []
        self.wait_error, self.signal_error = wait_error, signal_error
        self.returncode = 0 if exited else None
    def poll(self):
        return self.returncode
    def send_signal(self, value):
        self.calls.append(("signal", value))
        if self.signal_error:
            raise self.signal_error
    def terminate(self):
        self.calls.append(("terminate",))
        if self.signal_error:
            raise self.signal_error
    def wait(self, *, timeout):
        self.calls.append(("wait", timeout))
        if self.wait_error is not None:
            error, self.wait_error = self.wait_error, None
            raise error
        self.returncode = 0
    def kill(self):
        self.calls.append(("kill",))


def supervisor(children):
    closed = []
    return SimpleNamespace(children=children, job=SimpleNamespace(close=lambda: closed.append("job")),
                           logs=[SimpleNamespace(close=lambda: closed.append("log"))]), closed


@pytest.mark.parametrize("platform_name", ["nt", "posix"])
def test_launcher_waits_forty_seconds_for_owned_backend_and_five_for_frontend(platform_name):
    stop, _ = launcher_stop(platform_name)
    backend, frontend = FakeChild(), FakeChild()
    local, closed = supervisor([("backend", backend), ("frontend", frontend)])
    stop(local)
    assert ("wait", 40) in backend.calls and ("wait", 5) in frontend.calls
    assert not any(call[0] == "kill" for call in backend.calls + frontend.calls)
    assert closed == ["job", "log"]


def test_launcher_timeout_only_kills_its_owned_child_after_full_backend_grace(capsys):
    stop, TimeoutExpired = launcher_stop()
    owned, reused = FakeChild(wait_error=TimeoutExpired("synthetic timeout")), FakeChild()
    local, closed = supervisor([("backend", owned)])
    stop(local)
    assert owned.calls == [("signal", "fake-break"), ("wait", 40), ("kill",), ("wait", 3)]
    assert reused.calls == [] and closed == ["job", "log"]
    assert "仍需核对原记录" in capsys.readouterr().out


def test_launcher_signal_failure_still_waits_full_grace_without_exposing_details(capsys):
    stop, _ = launcher_stop()
    child = FakeChild(signal_error=OSError("private-handle-details"))
    local, _ = supervisor([("backend", child)])
    stop(local)
    assert child.calls == [("signal", "fake-break"), ("wait", 40)]
    output = capsys.readouterr().out
    assert "停止请求未确认" in output and "private-handle-details" not in output


def test_launcher_never_reclaims_exited_or_reused_services():
    stop, _ = launcher_stop()
    exited, reused = FakeChild(exited=True), FakeChild()
    local, _ = supervisor([("backend", exited)])
    stop(local)
    assert exited.calls == reused.calls == []
