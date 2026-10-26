"""Explicit import-mode wiring, with temporary ledgers and synthetic adapters.

No model, ingestion backend, browser, application database or external API runs.
The fake engine deliberately cannot save anything; it only observes injection.
"""
from contextlib import nullcontext
from datetime import datetime, timezone
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app.mention_agent import service as service_module
from app.mention_agent.contracts import Binding
from app.mention_agent.store import AgentStore
from app.mention_receiver import __main__ as runner


@pytest.fixture(autouse=True)
def isolated_legacy_authority(monkeypatch):
    monkeypatch.setattr(runner,'legacy_testing_blocked',lambda:False)
    monkeypatch.setattr(service_module,'legacy_testing_blocked',lambda:False)


def binding():
    return Binding.from_dict({
        "binding_id": "import-mode-fixture", "instance_id": "fixture-pc",
        "sender_profile_url": "https://www.douyin.com/user/import-mode-A",
        "receiver_profile_url": "https://www.douyin.com/user/import-mode-B",
        "knowledge_base_id": 7, "public_reply_allowed": True, "active": True,
    })


def envelope():
    current = binding()
    return {"command_id": "import-mode-command", "reference_id": "import-mode-reference",
            "reference_quality": "verified_ui", "native_comment_id": None,
            "sender_profile_url": current.sender_profile_url, "receiver_profile_url": current.receiver_profile_url,
            "video_id": "7999999000000000001", "comment_text": "@虚构B 请把这个视频保存到“测试知识库”，测试编号 DM-IMPORT-FIXTURE。",
            "source_mode": "live_ui", "evidence": {"real_mention": True, "unique_count": 1,
                "sender_verified": True, "video_verified": True,
                "observed_at_utc": datetime.now(timezone.utc).isoformat()}}


class Knowledge:
    def describe(self):
        return {"id": 7, "name": "测试知识库", "total_count": 1, "ready_count": 1}

    def query(self, *_):
        raise AssertionError("wiring tests must not run retrieval")


def no_proposal():
    return SimpleNamespace(proposal=None, review_code="isolated_wiring_complete", model_rounds=0, tool_calls=0)


def test_default_service_keeps_old_engine_signature_and_300_second_lease(tmp_path, monkeypatch):
    store = AgentStore(tmp_path / "agent.sqlite3")
    importer = Mock(side_effect=AssertionError("disabled mode cannot construct imports"))
    constructed = []

    class LegacyEngine:
        def __init__(self, model, knowledge):
            constructed.append((model, knowledge))

        def run(self, *args, **kwargs):
            return no_proposal()

    monkeypatch.setattr(service_module, "AgentEngine", LegacyEngine)
    claim = Mock(wraps=store.claim)
    monkeypatch.setattr(store, "claim", claim)
    model, knowledge = object(), Knowledge()
    service = service_module.AgentService(store, binding(), model=model, knowledge=knowledge, import_factory=importer)
    result = service.process(envelope())
    assert result["state"] == "needs_review"
    assert constructed == [(model, knowledge)]
    assert claim.call_args.kwargs["lease_seconds"] == 300
    importer.assert_not_called()


def test_enabled_service_injects_only_claimed_trusted_context_and_live_stop_callback(tmp_path, monkeypatch):
    store = AgentStore(tmp_path / "agent.sqlite3")
    factory_calls, engines = [], []
    state = {"stop": False}
    stop = lambda: state["stop"]
    tools = object()

    def factory(*args, **kwargs):
        factory_calls.append((args, kwargs))
        return tools

    class Engine:
        def __init__(self, model, knowledge, *, ingestion):
            assert ingestion is tools
            engines.append(self)

        def run(self, current, value, **kwargs):
            assert current == binding() and value["video_id"] == envelope()["video_id"]
            assert kwargs["current_binding"]() == binding()
            return no_proposal()

    monkeypatch.setattr(service_module, "AgentEngine", Engine)
    claim = Mock(wraps=store.claim)
    monkeypatch.setattr(store, "claim", claim)
    service = service_module.AgentService(store, binding(), model=object(), knowledge=Knowledge(), allow_import=True,
                                         import_factory=factory, import_should_stop=stop)
    result = service.process(envelope())
    assert result["state"] == "needs_review"
    assert claim.call_args.kwargs["lease_seconds"] == 1200
    assert len(factory_calls) == len(engines) == 1
    args, kwargs = factory_calls[0]
    assert args[0] is store and args[1] == binding() and args[2] == result["task_id"]
    assert args[3] == store.get_task(result["task_id"])["claim_token"]
    assert args[4] == envelope() | {"evidence": args[4]["evidence"]}
    assert args[5] == "测试知识库"
    assert kwargs["should_stop"] is stop and kwargs["should_stop"]() is False
    state["stop"] = True
    assert kwargs["should_stop"]() is True
    service.process(envelope())
    assert len(factory_calls) == len(engines) == 1, "重复任务不得重新构造入库工具或运行模型"


def test_import_factory_failure_is_safe_and_does_not_run_engine(tmp_path, monkeypatch):
    secret = "SYNTHETIC_IMPORT_FACTORY_DIAGNOSTIC"
    store = AgentStore(tmp_path / "agent.sqlite3")
    engine = Mock(side_effect=AssertionError("failed construction cannot reach engine"))
    monkeypatch.setattr(service_module, "AgentEngine", engine)

    def fail(*args, **kwargs):
        raise RuntimeError(secret)

    result = service_module.AgentService(store, binding(), model=object(), knowledge=Knowledge(),
                                        allow_import=True, import_factory=fail).process(envelope())
    assert result["state"] == "needs_review"
    assert secret not in json.dumps(result)
    assert secret not in json.dumps(store.list_steps(result["task_id"]))
    engine.assert_not_called()


@pytest.mark.parametrize("description", [
    {"id": 99, "name": "其他库"}, {"id": True, "name": "测试知识库"}, {"id": 7, "name": ""},
])
def test_scope_mismatch_never_constructs_import_tools(tmp_path, monkeypatch, description):
    knowledge = SimpleNamespace(describe=lambda: description)
    factory = Mock(side_effect=AssertionError("scope mismatch must fail before construction"))
    engine = Mock(side_effect=AssertionError("scope mismatch must fail before engine"))
    monkeypatch.setattr(service_module, "AgentEngine", engine)
    result = service_module.AgentService(AgentStore(tmp_path / "agent.sqlite3"), binding(), model=object(), knowledge=knowledge,
                                        allow_import=True, import_factory=factory).process(envelope())
    assert result["state"] == "needs_review"
    factory.assert_not_called()
    engine.assert_not_called()


@pytest.mark.parametrize("enabled", [False, True])
def test_receiver_builder_passes_import_keyword_only_for_explicit_enable(tmp_path, monkeypatch, enabled):
    database = tmp_path / "fictional-business-path.db"
    monkeypatch.setattr(service_module, "configured_database", lambda: database)
    monkeypatch.setattr(service_module, "read_binding", lambda path: binding())
    store = object()
    monkeypatch.setattr(service_module, "open_store", lambda *args: store)
    calls = []

    def construct(*args, **kwargs):
        calls.append((args, kwargs))
        return SimpleNamespace()

    monkeypatch.setattr(service_module, "AgentService", construct)
    options = {"allow_import": True} if enabled else {}
    handler = service_module.build_receiver_handler(binding_path=tmp_path / "binding.json", state_path=tmp_path / "agent.db",
                                                    required_marker="DM-IMPORT-FIXTURE", max_tasks=1, **options)
    assert handler.required_marker == "DM-IMPORT-FIXTURE"
    assert calls[0][0] == (store, binding())
    keywords = dict(calls[0][1])
    authorization = keywords.pop("scope_authorizer")
    assert keywords == {"database": database, **options}
    assert authorization(binding()) is True
    monkeypatch.setattr(service_module,"legacy_testing_blocked",lambda:True)
    assert authorization(binding()) is False
    assert not database.exists()


def test_import_builder_rejects_multi_task_before_resolving_database(tmp_path, monkeypatch):
    resolve = Mock(side_effect=AssertionError("invalid budget must not inspect storage"))
    monkeypatch.setattr(service_module, "configured_database", resolve)
    with pytest.raises(ValueError):
        service_module.build_receiver_handler(binding_path=tmp_path / "binding.json", state_path=tmp_path / "agent.db",
                                              required_marker="DM-IMPORT-FIXTURE", max_tasks=2, allow_import=True)
    resolve.assert_not_called()


@pytest.mark.parametrize("arguments", [
    ["--agent-allow-import"],
    ["--agent-allow-import", "--agent-marker", "DM-I"],
    ["--agent-allow-import", "--agent-binding", "fictional.json"],
    ["--agent-allow-import", "--agent-binding", "fictional.json", "--agent-marker", "DM-I", "--agent-max-tasks", "2"],
    ["--agent-allow-import", "--agent-binding", "fictional.json", "--agent-marker", "DM-I", "--replay", "fictional-replay.json"],
    ["--agent-allow-import", "--stop"],
    ["--agent-allow-import", "--agent-binding", "fictional.json", "--agent-marker", "DM-I", "--stop"],
])
def test_import_cli_rejects_missing_authority_or_nonlive_modes_before_storage(monkeypatch, arguments):
    preflight = Mock(side_effect=AssertionError("invalid options cannot reach storage"))
    monkeypatch.setattr(runner, "preflight_state", preflight)
    with pytest.raises(SystemExit) as caught:
        runner.main(arguments)
    assert caught.value.code == 2
    preflight.assert_not_called()


@pytest.mark.parametrize("allow_import,auto_reply", [(False, False), (True, False), (True, True)])
def test_valid_cli_import_opt_in_is_forwarded_without_changing_reply_opt_in(tmp_path, monkeypatch, allow_import, auto_reply):
    root = tmp_path / "state"
    monkeypatch.setattr(runner, "STATE_DIR", root)
    monkeypatch.setattr(runner, "configured_database", lambda *args: tmp_path / "fictional-business.db")
    monkeypatch.setattr(runner, "preflight_state", lambda *args: tmp_path / "fictional-browser")
    monkeypatch.setattr(runner, "receiver_lock", lambda *args: nullcontext())
    monkeypatch.setattr(runner, "configuration", lambda *args: {"receiver_public_number": "123456"})
    monkeypatch.setattr(runner, "bind_receiver", lambda *args: None)
    monkeypatch.setattr(runner, "SQLiteInbox", lambda *args: object())
    monkeypatch.setattr(runner, "RunHistory", lambda *args: nullcontext(object()))
    handler = object()
    factory = Mock(return_value=handler)
    monkeypatch.setattr(service_module, "build_receiver_handler", factory)
    live = Mock()
    monkeypatch.setattr(runner, "run_live", live)
    args = ["--agent-binding", str(tmp_path / "binding.json"), "--agent-marker", " DM-IMPORT-FIXTURE ",
            "--agent-state", str(tmp_path / "agent.db"), "--agent-max-tasks", "1"]
    if allow_import:
        args.append("--agent-allow-import")
    if auto_reply:
        args.append("--agent-auto-reply")
    assert runner.main(args) == 0
    assert factory.call_args.kwargs == {"binding_path": tmp_path / "binding.json", "state_path": tmp_path / "agent.db",
                                        "max_tasks": 1, "required_marker": "DM-IMPORT-FIXTURE",
                                        **({"allow_import": True} if allow_import else {})}
    passed = live.call_args.args[0]
    assert passed.agent_allow_import is allow_import and passed.agent_auto_reply is auto_reply
    assert live.call_args.kwargs["agent_handler"] is handler


@pytest.mark.parametrize("interruption,expected_reason", [
    ("stop", "stopped"), ("window", "window_closed"), ("deadline", "duration_elapsed"),
])
def test_long_import_wait_receives_live_cancellation_and_cannot_publish_after_stop(tmp_path, monkeypatch, interruption, expected_reason):
    clock = SimpleNamespace(value=0.0)
    page = SimpleNamespace(closed=False)
    page.is_closed = lambda: page.closed
    page.goto = lambda *args, **kwargs: None
    page.wait_for_timeout = lambda milliseconds: setattr(clock, "value", clock.value + milliseconds / 1000)
    context = SimpleNamespace(pages=[page])
    context.close = lambda: setattr(page, "closed", True)
    playwright = SimpleNamespace(chromium=SimpleNamespace(launch_persistent_context=lambda *args, **kwargs: context))
    monkeypatch.setattr("playwright.sync_api.sync_playwright", lambda: nullcontext(playwright))
    history = SimpleNamespace(stopped=False, cycles=0, final=None, run_id="synthetic-import-run")
    history.stop_requested = lambda: history.stopped
    history.summary = lambda: {"cycle_count": history.cycles}
    history.record_cycle = lambda *args, **kwargs: setattr(history, "cycles", history.cycles + 1)
    history.finish = lambda reason: setattr(history, "final", reason)
    service = SimpleNamespace(store=object(), binding=object(), allow_import=True, import_should_stop=None)
    observations = []

    class Handler:
        exhausted = False

        def __init__(self):
            self.service = service

        def accepts_notice(self, notice):
            return True

        def handle_detail(self, notice, detail):
            assert callable(self.service.import_should_stop)
            observations.append(self.service.import_should_stop())
            if interruption == "stop":
                history.stopped = True
            elif interruption == "window":
                page.closed = True
            else:
                clock.value = 61.0
            observations.append(self.service.import_should_stop())
            self.exhausted = True
            return {"created": True, "task_id": "fictional-ready-task", "state": "answer_ready"}

    notice = {"source_profile_url": binding().sender_profile_url, "exact_text": envelope()["comment_text"],
              "semantic_fingerprint": "/synthetic-cover"}
    adapter = SimpleNamespace(open_method="hover", open_attempts=1,
                              begin_cycle=lambda: None, identity=lambda: SimpleNamespace(display_name="虚构B"),
                              notifications=lambda: [notice], detail=lambda *args: {}, ui_state=lambda: {})
    monkeypatch.setattr(runner, "NormalPageReceiver", lambda *args, **kwargs: adapter)
    monkeypatch.setattr(runner, "browser_options", lambda *args: {})
    monkeypatch.setattr(runner.time, "monotonic", lambda: clock.value)
    monkeypatch.setattr(runner, "STATE_DIR", tmp_path)
    dashboards = []
    monkeypatch.setattr(runner, "write_dashboard", lambda *args, **kwargs: dashboards.append(kwargs))
    publisher = SimpleNamespace(publish=Mock(side_effect=AssertionError("cancelled imports cannot enter publisher")))
    from app.mention_agent import publisher as publisher_module
    monkeypatch.setattr(publisher_module, "ReplyPublisher", lambda *args, **kwargs: publisher)
    args = SimpleNamespace(agent_allow_import=True, agent_auto_reply=True, agent_max_tasks=1,
                           agent_state=tmp_path / "agent.sqlite3", duration_minutes=1, max_cycles=1, poll_seconds=20)
    runner.run_live(args, object(), [], {"receiver_public_number": "123456",
                    "receiver_profile_url": binding().receiver_profile_url}, tmp_path / "fictional-browser", history,
                    agent_handler=Handler())
    assert observations == [False, True]
    assert history.final == expected_reason
    publisher.publish.assert_not_called()
    messages = "\n".join(value["message"] for value in dashboards)
    assert "没有自动导入" not in messages and "不导入视频" not in messages
    assert "结果" in messages and "台账" in messages
