"""Agent orchestration: injected model/tools, temporary owned ledger only."""
from copy import deepcopy
from datetime import datetime, timezone
import json
from types import SimpleNamespace

import pytest

from app.mention_agent.contracts import Binding, parse_tool_arguments, validate_envelope
from app.mention_agent.engine import AgentEngine
from app.mention_agent.model import DeepSeekToolModel, IncompleteModelResponse, ModelTurn
from app.mention_agent.service import AgentService, ReceiverHandler
from app.mention_agent.store import AgentStore


def binding(**changes):
    return Binding.from_dict({"binding_id": "a-local", "instance_id": "pc-a", "sender_profile_url": "https://www.douyin.com/user/a",
        "receiver_profile_url": "https://www.douyin.com/user/b", "knowledge_base_id": 6,
        "public_reply_allowed": True, "active": True, **changes})


def envelope(**changes):
    b = binding()
    return {"command_id": "command-one", "reference_id": "ui-reference-one", "reference_quality": "verified_ui",
        "native_comment_id": None, "sender_profile_url": b.sender_profile_url, "receiver_profile_url": b.receiver_profile_url,
        "video_id": "7900000000000000002", "comment_text": '@B 请根据“验收库”知识库说明主题。',
        "source_mode": "isolated", "evidence": {"real_mention": True, "sender_verified": True, "video_verified": True,
            "unique_count": 1, "observed_at_utc": datetime.now(timezone.utc).isoformat()}, **changes}


def tool(name, args, call_id, **extra):
    return ModelTurn({"role": "assistant", "content": "", "tool_calls": [{"id": call_id, "type": "function",
        "function": {"name": name, "arguments": json.dumps(args, ensure_ascii=False)}}], **extra}, {"total_tokens": 7}, "tool_calls")


class Model:
    def __init__(self, turns):
        self.turns = iter(turns)
        self.requests = []

    def complete(self, messages, tools):
        self.requests.append(deepcopy(messages))
        return next(self.turns)


class Knowledge:
    def __init__(self, *, fail=False, empty=False):
        self.queries, self.fail, self.empty = [], fail, empty

    def describe(self):
        return {"id": 6, "name": "验收库", "total_count": 1, "ready_count": 0 if self.empty else 1}

    def query(self, question):
        self.queries.append(question)
        if self.fail:
            raise ValueError("synthetic_provider_private_diagnostic")
        return {"status": "empty" if self.empty else "ok", "knowledge_base_id": 6, "knowledge_base_name": "验收库",
            "retrieval_mode": "semantic", "evidence": [] if self.empty else [{"source_id": "资料1",
                "platform_item_id": "7999999000000000022", "chunk_id": "c1", "title": "实验原文",
                "url": "https://www.douyin.com/video/7999999000000000022", "text": "资料介绍如何种植一棵树。"}]}


def answer_turns():
    return [tool("query_knowledge_base", {"knowledge_base_id": 6, "question": "主题是什么"}, "query"),
            tool("propose_reply", {"text": "这份资料介绍种树的方法。", "source_ids": ["资料1"]}, "reply")]


def test_real_tool_result_is_a_tool_message_and_reply_is_only_an_intent(tmp_path):
    store = AgentStore(tmp_path / "agent.db")
    model, knowledge = Model(answer_turns()), Knowledge()
    result = AgentService(store, binding(), model=model, knowledge=knowledge).process(envelope())
    assert result["state"] == "answer_ready"
    assert result["model_request_attempts"] == result["completed_model_responses"] == 2
    assert knowledge.queries == ["主题是什么"]
    assert model.requests[1][-1]["role"] == "tool"
    assert model.requests[1][-1]["tool_call_id"] == "query"
    assert "如何种植一棵树" in model.requests[1][-1]["content"]
    reply = store.get_reply(result["task_id"])
    assert reply["state"] == "ready"
    assert reply["target"]["comment_text"] == envelope()["comment_text"]
    assert "[来源" not in reply["text"] and reply["source_ids"] == ["资料1"]
    assert store.reserve_send(result["task_id"], {}) is None


def test_completed_task_replay_never_calls_model_again_after_store_reopen(tmp_path):
    path = tmp_path / "agent.db"
    model = Model(answer_turns())
    first = AgentService(AgentStore(path), binding(), model=model, knowledge=Knowledge()).process(envelope())
    unused = Model([])
    second = AgentService(AgentStore(path), binding(), model=unused, knowledge=Knowledge()).process(envelope())
    assert second["task_id"] == first["task_id"]
    assert second["state"] == "answer_ready" and not second["ran_model"]
    assert unused.requests == []


def test_provider_failure_quarantines_once_without_private_diagnostics(tmp_path):
    class Broken:
        calls = 0
        def complete(self, messages, tools):
            self.calls += 1
            raise RuntimeError("SYNTHETIC_PRIVATE_PROVIDER_KEY")
    model = Broken()
    store = AgentStore(tmp_path / "agent.db")
    service = AgentService(store, binding(), model=model, knowledge=Knowledge())
    first = service.process(envelope())
    assert first["state"] == "needs_review" and first["model_request_attempts"] == 1
    assert first["reason_code"] == "model_request_failed"
    service.process(envelope())
    assert model.calls == 1
    assert "SYNTHETIC_PRIVATE_PROVIDER_KEY" not in json.dumps(store.list_steps(first["task_id"]))


def test_retrieval_failure_is_not_empty_evidence_and_does_not_generate_second_answer(tmp_path):
    model = Model(answer_turns())
    store = AgentStore(tmp_path / "agent.db")
    result = AgentService(store, binding(), model=model, knowledge=Knowledge(fail=True)).process(envelope())
    assert result["state"] == "needs_review" and result["reason_code"] == "knowledge_query_failed"
    assert len(model.requests) == 1 and store.get_reply(result["task_id"]) is None


def test_empty_library_returns_explicit_clarification_not_a_fabricated_answer(tmp_path):
    model = Model([answer_turns()[0], tool("request_clarification", {"reason_code": "no_evidence"}, "clarify")])
    result = AgentService(AgentStore(tmp_path / "agent.db"), binding(), model=model, knowledge=Knowledge(empty=True)).process(envelope())
    assert result["reply"]["kind"] == "clarification"
    assert result["reply"]["source_ids"] == []


def test_public_permission_stops_before_model_and_exhausts_one_processing_attempt(tmp_path):
    current = binding(public_reply_allowed=False)
    model = Model([])
    service = AgentService(AgentStore(tmp_path / "agent.db"), current, model=model, knowledge=Knowledge())
    result = service.process(envelope())
    assert result["state"] == "needs_review" and not result["ran_model"]
    assert result["processing_attempted"] and result["model_request_attempts"] == 0
    assert model.requests == []


@pytest.mark.parametrize("args", ['{"question":"a","question":"b"}', '{"question":NaN}', '{"question":"a","owner":"other"}'])
def test_duplicate_nonfinite_and_unknown_arguments_are_rejected(args):
    with pytest.raises(ValueError):
        parse_tool_arguments(args, {"question"}, {"question"})


def test_sender_or_fake_mention_is_rejected_before_any_model_call():
    for value in (envelope(sender_profile_url="https://www.douyin.com/user/other"),
                  envelope(evidence={**envelope()["evidence"], "real_mention": False})):
        with pytest.raises(ValueError):
            validate_envelope(value, binding())


def test_model_repairs_undeclared_inline_citation_before_reply_is_accepted():
    model = Model([answer_turns()[0],
        tool("propose_reply", {"text": "主题是种树（资料1、资料7）。", "source_ids": ["资料1"]}, "bad-reply"),
        tool("propose_reply", {"text": "主题是种树（资料1）。", "source_ids": ["资料1"]}, "fixed-reply")])
    result = AgentEngine(model, Knowledge()).run(binding(), envelope())
    assert result.proposal and "资料7" not in result.proposal["text"]
    assert result.model_rounds == 3
    error = json.loads(model.requests[2][-1]["content"])
    assert error["code"] == "undeclared_source_reference"


def test_tool_audit_records_source_identity_hashes_without_copying_source_text():
    records = []
    result = AgentEngine(Model(answer_turns()), Knowledge()).run(binding(), envelope(), record=lambda *x: records.append(x))
    evidence_log = next(meta for kind, meta in records if kind == "tool_result" and meta["tool_name"] == "query_knowledge_base")
    assert evidence_log["knowledge_base_id"] == 6
    assert len(evidence_log["evidence_refs"][0]["text_sha256"]) == 64
    assert "如何种植一棵树" not in json.dumps(records, ensure_ascii=False)
    assert result.proposal


def test_transport_keeps_tool_reasoning_without_automatic_retry_or_thinking_default():
    class Client:
        def __init__(self):
            self.chat = SimpleNamespace(completions=self)
        def with_options(self, **options):
            self.options = options
            return self
        def create(self, **kwargs):
            self.request = kwargs
            call = SimpleNamespace(id="one", type="function", function=SimpleNamespace(name="list_knowledge_bases", arguments="{}"))
            message = SimpleNamespace(content="", tool_calls=[call], reasoning_content="synthetic-private-reasoning")
            return SimpleNamespace(choices=[SimpleNamespace(message=message, finish_reason="tool_calls")], usage=SimpleNamespace(total_tokens=13))
    client = Client()
    result = DeepSeekToolModel(client=client, model="unit-model").complete([], [])
    assert result.message["reasoning_content"] == "synthetic-private-reasoning"
    assert client.options["max_retries"] == 0
    assert client.request["extra_body"]["thinking"]["type"] == "disabled"
    assert result.finish_reason == "tool_calls" and result.usage == {"total_tokens": 13}
