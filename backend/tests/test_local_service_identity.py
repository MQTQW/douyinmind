"""Real launcher functions with fake HTTP/child handles; no service is started."""
import argparse
import ast
from datetime import datetime
import hashlib
import json
from pathlib import Path
import re
import sys
from types import SimpleNamespace

import pytest


FIELDS = ("project_id", "backend_code_id", "dependency_id", "config_revision")
EXPECTED = {field: format(number + 1, "064x") for number, field in enumerate(FIELDS)}
EXPECTED_TARGET = hashlib.sha256(b"http://127.0.0.1:8000").hexdigest()


def launcher_namespace(tmp_path):
    backend = Path(__file__).resolve().parents[1]
    fixture = backend / "app" / "_launcher_shutdown_source.py"
    source = fixture if fixture.is_file() else backend.parent / "start-local.py"
    if not source.is_file():
        pytest.skip("strict launcher tests need the byte-identical public launcher fixture")
    names = {"_valid_identity", "expected_backend_identity", "service_ready", "LocalSupervisor", "main"}
    nodes = [node for node in ast.parse(source.read_text(encoding="utf-8-sig")).body
             if isinstance(node, (ast.FunctionDef, ast.ClassDef)) and node.name in names]
    calls = []
    def forbidden(*args, **kwargs):
        pytest.fail("identity checks cannot start or signal a real process")
    namespace = {"__doc__": "public launcher fixture", "argparse": argparse, "Path": Path, "json": json,
        "re": re, "datetime": datetime, "PROJECT_ROOT": tmp_path / "project",
        "_FRONTEND_API_TARGET_ID": EXPECTED_TARGET,
        "LOG_ROOT": tmp_path / "logs", "URL": "http://127.0.0.1:5173/", "_IDENTITY_FIELDS": FIELDS,
        "os": SimpleNamespace(name="posix", environ={}),
        "sys": SimpleNamespace(executable=str(tmp_path / "other-python"), dont_write_bytecode=True,
                               argv=["start-local.py"], stderr=SimpleNamespace(write=lambda *_: None, flush=lambda: None)),
        "time": SimpleNamespace(monotonic=lambda: 0, sleep=lambda *_: None),
        "signal": SimpleNamespace(CTRL_BREAK_EVENT="synthetic-break"),
        "urllib": SimpleNamespace(error=SimpleNamespace(URLError=OSError)),
        "subprocess": SimpleNamespace(Popen=forbidden, run=forbidden, CREATE_NEW_PROCESS_GROUP=0,
                                      TimeoutExpired=RuntimeError, STDOUT=-2),
        "WindowsChildJob": forbidden, "port_in_use": forbidden,
        "shutil": SimpleNamespace(which=forbidden), "_read_local": forbidden,
    }
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(source), "exec"), namespace)
    return namespace, calls


def healthy():
    return {"status": "ok", "service": "DouyinRAG", "version": "0.1.0", **EXPECTED}


def test_backend_old_health_without_identity_is_refused(tmp_path):
    namespace, _ = launcher_namespace(tmp_path)
    namespace["_read_local"] = lambda _: json.dumps({"status": "ok", "service": "DouyinRAG"})
    assert namespace["service_ready"]("backend", expected_identity=EXPECTED) is False


@pytest.mark.parametrize("field", FIELDS)
def test_any_stale_backend_fingerprint_prevents_reuse(tmp_path, field):
    namespace, _ = launcher_namespace(tmp_path)
    body = {**healthy(), field: "f" * 64}
    namespace["_read_local"] = lambda _: json.dumps(body)
    assert namespace["service_ready"]("backend", expected_identity=EXPECTED) is False


def test_backend_matching_current_copy_environment_and_configuration_can_be_reused(tmp_path):
    namespace, _ = launcher_namespace(tmp_path)
    urls = []
    def read(url):
        urls.append(url)
        return json.dumps(healthy())
    namespace["_read_local"] = read
    assert namespace["service_ready"]("backend", expected_identity=EXPECTED) is True
    assert urls == ["http://127.0.0.1:8000/"]


@pytest.mark.parametrize("body,html,expected", [
    ({"service": "DouyinMindFrontend", "project_id": EXPECTED["project_id"], "api_target_id": EXPECTED_TARGET}, '<title>DouyinMind</title><script src="/src/main.tsx"></script>', True),
    ({"service": "DouyinMindFrontend", "project_id": "f" * 64, "api_target_id": EXPECTED_TARGET}, '<title>DouyinMind</title>/src/main.tsx', False),
    ({"service": "OtherFrontend", "project_id": EXPECTED["project_id"], "api_target_id": EXPECTED_TARGET}, '<title>DouyinMind</title>/src/main.tsx', False),
    ({"service": "DouyinMindFrontend", "project_id": EXPECTED["project_id"], "api_target_id": EXPECTED_TARGET}, '<title>Other</title>/src/main.tsx', False),
    ({"service": "DouyinMindFrontend", "project_id": EXPECTED["project_id"], "api_target_id": EXPECTED_TARGET}, '<title>DouyinMind</title>', False),
    ({"service": "DouyinMindFrontend", "project_id": EXPECTED["project_id"]}, '<title>DouyinMind</title>/src/main.tsx', False),
    ({"service": "DouyinMindFrontend", "project_id": EXPECTED["project_id"], "api_target_id": hashlib.sha256(b"http://127.0.0.1:9999").hexdigest()}, '<title>DouyinMind</title>/src/main.tsx', False),
])
def test_frontend_requires_same_project_and_real_source_entry(tmp_path, body, html, expected):
    namespace, _ = launcher_namespace(tmp_path)
    namespace["_read_local"] = lambda url: json.dumps(body) if url.endswith("__douyinmind_identity") else html
    assert namespace["service_ready"]("frontend", expected_identity=EXPECTED) is expected


def identity_helper_files(namespace):
    backend = namespace["PROJECT_ROOT"] / "backend"
    python = backend / ".venv" / "Scripts" / "python.exe"
    helper = backend / "app" / "core" / "runtime_identity.py"
    python.parent.mkdir(parents=True)
    helper.parent.mkdir(parents=True)
    python.write_bytes(b"inert interpreter fixture")
    helper.write_text("def capture_runtime_identity(project_root=None):\n    return " + repr(EXPECTED) + "\n", encoding="utf-8")
    return backend, python, helper


def test_expected_identity_uses_current_venv_directly_without_spawning(tmp_path):
    namespace, _ = launcher_namespace(tmp_path)
    _, python, _ = identity_helper_files(namespace)
    namespace["sys"].executable = str(python)
    namespace["sys"].dont_write_bytecode = False
    assert namespace["expected_backend_identity"]() == EXPECTED
    assert namespace["sys"].dont_write_bytecode is False


def test_other_interpreter_uses_fixed_backend_venv_command_and_json_only(tmp_path):
    namespace, _ = launcher_namespace(tmp_path)
    backend, python, helper = identity_helper_files(namespace)
    calls = []
    def run(command, **kwargs):
        calls.append((command, kwargs))
        return SimpleNamespace(returncode=0, stdout=json.dumps(EXPECTED))
    namespace["subprocess"].run = run
    assert namespace["expected_backend_identity"]() == EXPECTED
    command, options = calls[0]
    assert command == [str(python), "-B", str(helper), "--project-root", str(namespace["PROJECT_ROOT"])]
    assert options["cwd"] == backend and options["shell"] is False
    assert options["env"]["PYTHONDONTWRITEBYTECODE"] == "1"


def test_identity_helper_failure_is_fixed_and_does_not_echo_output(tmp_path):
    namespace, _ = launcher_namespace(tmp_path)
    identity_helper_files(namespace)
    namespace["subprocess"].run = lambda *_, **__: SimpleNamespace(returncode=1, stdout="private-key-output")
    with pytest.raises(RuntimeError) as caught:
        namespace["expected_backend_identity"]()
    assert "private-key-output" not in str(caught.value)


def test_foreign_occupied_port_is_refused_without_starting_or_killing(tmp_path):
    namespace, _ = launcher_namespace(tmp_path)
    namespace["service_ready"] = lambda *_, **__: False
    namespace["port_in_use"] = lambda _: True
    local = namespace["LocalSupervisor"](expected_identity=EXPECTED)
    with pytest.raises(RuntimeError, match="端口 8000 已被占用"):
        local.start("backend", 8000, ["inert"], tmp_path)
    assert local.children == [] and local.job is None and local.logs == []


def test_supervisor_computes_expected_once_and_reuses_it_for_startup_polls(tmp_path):
    namespace, _ = launcher_namespace(tmp_path)
    expected_calls, readiness_calls = [], []
    ready = iter((False, False, True, True))
    namespace["expected_backend_identity"] = lambda: expected_calls.append(1) or dict(EXPECTED)
    def readiness(name, *, expected_identity):
        readiness_calls.append((name, expected_identity))
        return next(ready)
    namespace["service_ready"] = readiness
    namespace["port_in_use"] = lambda _: False
    child = SimpleNamespace(returncode=None, poll=lambda: None)
    namespace["subprocess"].Popen = lambda *_, **__: child
    namespace["LOG_ROOT"].mkdir()
    local = namespace["LocalSupervisor"]()
    try:
        local.start("backend", 8000, ["synthetic"], tmp_path)
        local.start("frontend", 5173, ["synthetic"], tmp_path)
        assert expected_calls == [1]
        assert all(value == EXPECTED for _, value in readiness_calls)
        assert local.children == [("backend", child)]
    finally:
        for stream in local.logs:
            stream.close()


def test_check_mode_only_reads_identity_and_never_constructs_supervisor_or_lock(tmp_path, capsys, monkeypatch):
    namespace, _ = launcher_namespace(tmp_path)
    namespace["sys"].argv = ["start-local.py", "--check"]
    monkeypatch.setattr(sys, "argv", ["start-local.py", "--check"])
    counts = []
    namespace["expected_backend_identity"] = lambda: counts.append(1) or dict(EXPECTED)
    namespace["service_ready"] = lambda *_, **__: True
    namespace["LocalSupervisor"] = lambda: pytest.fail("check cannot create a launcher owner")
    assert namespace["main"]() == 0
    body = json.loads(capsys.readouterr().out)
    assert body == {"success": True, "project_id": EXPECTED["project_id"], "backend_ready": True, "frontend_ready": True}
    assert counts == [1] and not namespace["LOG_ROOT"].exists()


def test_check_mode_identity_failure_is_fixed_and_does_not_construct_a_launcher(tmp_path, capsys, monkeypatch):
    namespace, _ = launcher_namespace(tmp_path)
    monkeypatch.setattr(sys, "argv", ["start-local.py", "--check"])
    namespace["expected_backend_identity"] = lambda: (_ for _ in ()).throw(RuntimeError("private-config-detail"))
    namespace["LocalSupervisor"] = lambda: pytest.fail("failed check cannot create a launcher owner")
    assert namespace["main"]() == 1
    assert json.loads(capsys.readouterr().out) == {"success": False, "reason": "runtime_identity_unavailable"}
    assert not namespace["LOG_ROOT"].exists()


def health_function(captured):
    source = Path(__file__).resolve().parents[1] / "app" / "main.py"
    node = next(item for item in ast.parse(source.read_text(encoding="utf-8")).body
                if isinstance(item, ast.AsyncFunctionDef) and item.name == "root")
    node.decorator_list = []
    namespace = {"_RUNTIME_IDENTITY": dict(captured)}
    exec(compile(ast.Module(body=[node], type_ignores=[]), str(source), "exec"), namespace)
    def call():
        pending = namespace["root"]()
        try:
            pending.send(None)
        except StopIteration as done:
            return done.value
        finally:
            pending.close()
        pytest.fail("health must not perform async I/O or recapture identity")
    return call


def test_health_returns_a_copy_of_startup_fingerprints_with_legacy_service_fields():
    health = health_function(EXPECTED)
    first = health()
    assert first == {"status": "ok", "service": "DouyinRAG", "version": "0.1.0", **EXPECTED}
    first["backend_code_id"] = "caller-mutated"
    assert health()["backend_code_id"] == EXPECTED["backend_code_id"]
    assert set(health()) == {"status", "service", "version", *FIELDS}


def test_unidentified_health_fails_closed_without_exposing_exception_details():
    body = health_function({})()
    assert body == {"status": "identity_unavailable", "service": "DouyinRAG", "version": "0.1.0"}


def test_same_clone_temp_database_override_cannot_reuse_a_captured_backend(tmp_path):
    from app.core.runtime_identity import capture_runtime_identity
    namespace, _ = launcher_namespace(tmp_path)
    project = tmp_path / "same-clone"
    (project / "backend" / "app").mkdir(parents=True)
    (project / "backend" / "app" / "public.py").write_text("value = 1\n", encoding="utf-8")
    captured = capture_runtime_identity(project, dependency_versions={"synthetic": "1"},
        process_environment={"DATABASE_URL": "sqlite:///original-private-data.db"})
    current = capture_runtime_identity(project, dependency_versions={"synthetic": "1"},
        process_environment={"DATABASE_URL": "sqlite:///temporary-synthetic-test-data.db"})
    namespace["_read_local"] = lambda _: json.dumps(health_function(captured)())
    assert captured["project_id"] == current["project_id"]
    assert captured["backend_code_id"] == current["backend_code_id"]
    assert namespace["service_ready"]("backend", expected_identity=current) is False
