"""Plan proposals are fixed shapes, validated against the workflow validator."""
from types import SimpleNamespace

import pytest

from app.assistant import semantic_plan_proposal as proposal_mod
from app.assistant.workflow_store import validate_plan

SCOPE = {"user_id": "fixture-user", "device_id": "fixture-device",
         "instance_id": "fixture-instance",
         "sender_profile_url": "https://www.douyin.com/user/sender-fixture",
         "receiver_profile_url": "https://www.douyin.com/user/receiver-fixture",
         "video_id": "7999999000000000011", "root_reference_id": "fixture-root",
         "reference_id": "fixture-comment", "knowledge_base_id": 7,
         "query_scope": "library"}


def turn_for(goal, question=None):
    return SimpleNamespace(
        message={"tool_calls": [{"type": "function", "function": {
            "name": "submit_plan",
            "arguments": '{"goal": "%s", "question": %s}' % (goal, "null" if question is None else '"%s"' % question)}}]},
        usage={"total_tokens": 300, "input_tokens": 200, "output_tokens": 100},
        finish_reason="tool_calls")


class FakeModel:
    def __init__(self, turn=None, error=None):
        self.turn, self.error, self.calls = turn, error, 0

    def complete(self, messages, tools):
        self.calls += 1
        if self.error:
            raise RuntimeError(self.error)
        return self.turn


class FakeBudget:
    def __init__(self, fail=None):
        self.fail, self.reserved, self.settled = fail, [], []

    def reserve(self, task_id, request_key, estimate):
        if self.fail:
            raise RuntimeError(self.fail)
        self.reserved.append((task_id, request_key, estimate))
        return {'may_request': True}

    def settle(self, request_key, usage):
        self.settled.append((request_key, usage))


TASK = {"id": "proposal-task", "comment_text": "@6 先把这个视频保存到测试库，再根据资料总结三个要点"}


def test_save_then_query_proposal_is_valid_for_the_real_workflow_validator():
    model = FakeModel(turn_for("save_then_query", "总结三个要点"))
    result = proposal_mod.propose(TASK, model=model, budget=FakeBudget())
    assert result["goal"] == "save_then_query" and result["fallback"] is False
    assert [step["tool"] for step in result["steps"]] == [
        "import_current_video", "await_import", "query_current_video", "propose_reply"]
    # Engine compatibility: the real validator accepts the exact fixed shape.
    validate_plan({"goal": result["goal"], "steps": result["steps"]}, SCOPE)
    assert "总结三个要点" in result["summary"]


def test_save_proposal_has_no_question_and_is_engine_valid():
    result = proposal_mod.propose(TASK, model=FakeModel(turn_for("save")), budget=FakeBudget())
    assert result["goal"] == "save" and result["steps"][0]["tool"] == "import_current_video"
    validate_plan({"goal": result["goal"], "steps": result["steps"]}, SCOPE)


def test_decline_is_an_empty_proposal_and_bad_goals_fall_back():
    decline = proposal_mod.propose(TASK, model=FakeModel(turn_for("decline")), budget=FakeBudget())
    assert decline["goal"] == "decline" and decline["steps"] == []
    bad = proposal_mod.propose(TASK, model=FakeModel(turn_for("save_then_query")), budget=FakeBudget())
    assert bad["fallback"] is True  # query question missing -> invalid
    assert proposal_mod.propose(TASK, model=FakeModel(error="boom"), budget=FakeBudget())["fallback"] is True
    assert proposal_mod.propose(TASK, model=FakeModel(turn_for("save")), budget=FakeBudget(fail="budget"))["fallback"] is True


def test_question_must_be_bounded_and_present_for_query_shapes():
    with pytest.raises(proposal_mod.ProposalError):
        proposal_mod.build_steps("query", None)
    with pytest.raises(proposal_mod.ProposalError):
        proposal_mod.build_steps("save", "不该有问题")
    assert proposal_mod.build_steps("query", "视频讲了什么")[0]["args"]["question"] == "视频讲了什么"


def test_pure_question_is_never_proposed_as_a_save_plan():
    plain = {"id": "q-task", "comment_text": "@6 帮我看看这个视频讲了什么"}
    result = proposal_mod.propose(plain, model=FakeModel(turn_for("save_then_query", "视频讲了什么")), budget=FakeBudget())
    assert result["goal"] == "query"
    assert [step["tool"] for step in result["steps"]] == ["query_current_video", "propose_reply"]
    mixed = {"id": "m-task", "comment_text": "@6 保存到测试库，再总结三个要点"}
    assert proposal_mod.propose(mixed, model=FakeModel(turn_for("save_then_query", "总结三个要点")),
                                budget=FakeBudget())["goal"] == "save_then_query"
