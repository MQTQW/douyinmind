"""Independent behavioral probes: no real model, database or network."""
from copy import deepcopy
from datetime import datetime, timezone
import json
from types import SimpleNamespace

import pytest

from app.mention_agent.contracts import Binding
from app.mention_agent.engine import AgentEngine
from app.mention_agent.model import DeepSeekToolModel, ModelTurn


def binding():
    return Binding.from_dict({
        "binding_id": "independent-review", "instance_id": "fictional-desktop",
        "sender_profile_url": "https://www.douyin.com/user/review-sender",
        "receiver_profile_url": "https://www.douyin.com/user/review-receiver",
        "knowledge_base_id": 7, "public_reply_allowed": True, "active": True,
    })


def envelope(comment="请根据测试库回答这个问题"):
    current = binding()
    return {
        "command_id": "independent-command", "reference_id": "independent-reference",
        "reference_quality": "verified_ui", "native_comment_id": None,
        "sender_profile_url": current.sender_profile_url,
        "receiver_profile_url": current.receiver_profile_url,
        "video_id": "7999999000000000011", "comment_text": comment,
        "source_mode": "isolated",
        "evidence": {"real_mention": True, "sender_verified": True, "video_verified": True,
                     "unique_count": 1, "observed_at_utc": datetime.now(timezone.utc).isoformat()},
    }


def call(name, args, identity, *, reasoning=None):
    message = {"role": "assistant", "content": "", "tool_calls": [{
        "id": identity, "type": "function", "function": {
            "name": name, "arguments": json.dumps(args, ensure_ascii=False),
        },
    }]}
    if reasoning is not None:
        message["reasoning_content"] = reasoning
    return ModelTurn(message=message, usage={})


def query_turn(**options):
    return call("query_knowledge_base", {"knowledge_base_id": 7, "question": "原文怎样说明"}, "query-1", **options)


def proposal_turn(text="原文说明了测试步骤。", **options):
    return call("propose_reply", {"text": text, "source_ids": ["资料1"]}, "proposal-1", **options)


class ScriptModel:
    def __init__(self, turns):
        self.turns = list(turns)
        self.requests = []

    def complete(self, messages, tools):
        self.requests.append(deepcopy(messages))
        if self.turns:
            return self.turns.pop(0)
        return call("request_clarification", {"reason_code": "ambiguous_request"}, "fallback-clarification")


class FictionalKnowledge:
    def __init__(self, result_extra=None, evidence_extra=None, *, name="测试库"):
        self.queries = []
        self.result_extra = result_extra or {}
        self.evidence_extra = evidence_extra or {}
        self.name = name

    def describe(self):
        return {"id": 7, "name": self.name, "total_count": 1, "ready_count": 1}

    def query(self, question):
        self.queries.append(question)
        return {
            "status": "ok", "knowledge_base_id": 7, "knowledge_base_name": self.name,
            "retrieval_mode": "synthetic", "evidence": [{
                "source_id": "资料1", "platform_item_id": "7999999000000000022",
                "chunk_id": "synthetic-chunk", "title": "虚构原文", "text": "原文说明了测试步骤。",
                "url": "https://www.douyin.com/video/7999999000000000022", **self.evidence_extra,
            }], **self.result_extra,
        }


def is_answer(decision):
    return bool(decision.proposal and decision.proposal.get("kind") == "answer")


@pytest.mark.parametrize("comment", [
    "请用财务知识库回答报销规则。",
    "请根据知识库：财务回答报销规则。",
])
def test_explicit_other_library_must_not_silently_fall_back(comment):
    knowledge = FictionalKnowledge()
    model = ScriptModel([query_turn(), proposal_turn()])
    result = AgentEngine(model, knowledge).run(binding(), envelope(comment))
    assert not is_answer(result), "明确指定了另一库，不能拿当前绑定库作答"
    assert knowledge.queries == [], "不应先查错库再依赖模型自行纠正"


def test_adapter_metadata_is_not_a_second_route_for_private_fields_into_model():
    secret = "SYNTHETIC_PRIVATE_CANARY_NEVER_FROM_REAL_DATA"
    knowledge = FictionalKnowledge(
        result_extra={"history": [{"content": secret}], "diagnostics": {"internal_token": secret}},
        evidence_extra={"debug": secret, "raw_transcript_metadata": {"private_note": secret}},
    )
    model = ScriptModel([query_turn(), proposal_turn()])
    result = AgentEngine(model, knowledge).run(binding(), envelope())
    assert is_answer(result)
    assert secret not in json.dumps(model.requests, ensure_ascii=False), "仅允许显式 evidence 字段进入下一轮"


def test_failed_evidence_batch_cannot_leave_a_citable_partial_source():
    class BrokenBatch(FictionalKnowledge):
        def query(self, question):
            result = super().query(question)
            result["evidence"].append({"source_id": 42, "text": "合成的畸形证据"})
            return result

    model = ScriptModel([query_turn(), proposal_turn()])
    result = AgentEngine(model, BrokenBatch()).run(binding(), envelope())
    assert not is_answer(result), "工具结果被判非法后，模型未收到的前半批证据不能留作可引用来源"


@pytest.mark.parametrize("text", ["我已经保存到测试知识库。", "该视频已收藏成功。"])
def test_answer_tool_cannot_claim_unimplemented_write_success(text):
    model = ScriptModel([query_turn(), proposal_turn(text)])
    result = AgentEngine(model, FictionalKnowledge()).run(binding(), envelope())
    assert not is_answer(result), "引用了真证据也不能声称执行未实现的保存动作"


def test_plain_model_success_statement_has_no_effect_without_a_tool():
    model = ScriptModel([ModelTurn({"role": "assistant", "content": "已经上传并入库成功。"}, {})])
    knowledge = FictionalKnowledge()
    result = AgentEngine(model, knowledge).run(binding(), envelope())
    assert result.proposal is None
    assert knowledge.queries == []


def test_provider_reasoning_survives_each_tool_round_but_not_public_proposal_or_audit():
    private_reasoning = "SYNTHETIC_REASONING_ONLY_PROTOCOL"
    model = ScriptModel([
        call("list_knowledge_bases", {}, "list-1", reasoning=private_reasoning),
        query_turn(reasoning=private_reasoning + "-2"),
        proposal_turn(reasoning=private_reasoning + "-3"),
    ])
    records = []
    result = AgentEngine(model, FictionalKnowledge()).run(binding(), envelope(), record=lambda *args: records.append(args))
    assert is_answer(result)
    prior_assistants = [m for m in model.requests[2] if m["role"] == "assistant"]
    assert [m["reasoning_content"] for m in prior_assistants] == [private_reasoning, private_reasoning + "-2"]
    assert private_reasoning not in json.dumps(result.proposal, ensure_ascii=False)
    assert private_reasoning not in json.dumps(records, ensure_ascii=False)


def test_noncomplete_provider_tool_turn_never_executes_even_when_json_is_parseable():
    knowledge = FictionalKnowledge()

    class Client:
        def __init__(self):
            self.chat = SimpleNamespace(completions=self)
            self.count = 0

        def with_options(self, **options):
            return self

        def create(self, **options):
            self.count += 1
            raw = query_turn() if self.count == 1 else proposal_turn()
            tool = raw.message["tool_calls"][0]
            source = SimpleNamespace(
                content="", reasoning_content="synthetic reasoning",
                tool_calls=[SimpleNamespace(id=tool["id"], type="function", function=SimpleNamespace(**tool["function"]))],
            )
            return SimpleNamespace(choices=[SimpleNamespace(message=source, finish_reason="length" if self.count == 1 else "tool_calls")], usage=None)

    model = DeepSeekToolModel(client=Client(), model="fictional-model", thinking=True)
    try:
        decision = AgentEngine(model, knowledge).run(binding(), envelope())
    except Exception:
        # Either a sanitized exception or a review decision can reject this;
        # no knowledge operation may run before the incomplete turn is rejected.
        decision = None
    assert knowledge.queries == []
    assert decision is None or not is_answer(decision)


def test_raw_provider_exception_is_not_the_public_error_contract():
    secret = "SYNTHETIC_PROVIDER_SECRET_NOT_REAL"

    class FailingModel:
        def complete(self, messages, tools):
            raise RuntimeError("provider diagnostic contains " + secret)

    try:
        decision = AgentEngine(FailingModel(), FictionalKnowledge()).run(binding(), envelope())
    except Exception as error:
        assert secret not in str(error), "异常可向外抛出固定错误码，不能带原始供应商内容"
    else:
        assert decision.proposal is None
