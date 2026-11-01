"""Failure-triage display text: model output is bounded, cached separately."""
from types import SimpleNamespace

from app.assistant import semantic_triage


class FakeModel:
    def __init__(self, content="这条回复没有发出，可以重新处理一次或重新发送指令。"):
        self.content = content
        self.calls = 0

    def complete(self, messages, tools):
        self.calls += 1
        assert tools == []
        return SimpleNamespace(message={"content": self.content},
                               usage={"total_tokens": 120, "input_tokens": 80, "output_tokens": 40},
                               finish_reason="stop")


class FakeBudget:
    def __init__(self, fail=None):
        self.fail = fail
        self.reserved, self.settled, self.unknown = [], [], []

    def reserve(self, task_id, request_key, estimate):
        if self.fail:
            raise RuntimeError(self.fail)
        self.reserved.append((task_id, request_key, estimate))

    def settle(self, request_key, usage):
        self.settled.append((request_key, usage))

    def mark_unknown(self, request_key):
        self.unknown.append(request_key)


TASK = {"id": "failure-task", "state": "needs_review", "reason_code": "readback_confirmed_unsent",
        "kind": "clarify", "reply_state": "needs_review", "import_state": None,
        "details": {"reply_text": "请明确指定一个允许的知识库"}}


def test_model_text_is_returned_and_usage_settled():
    model, budget = FakeModel(), FakeBudget()
    result = semantic_triage.explain(TASK, model=model, budget=budget)
    assert result == {"text": "这条回复没有发出，可以重新处理一次或重新发送指令。", "fallback": False}
    assert model.calls == 1
    assert budget.reserved and budget.settled
    assert budget.reserved[0][0] == "failure-task"


def test_claims_of_completion_are_replaced_by_fixed_fallback():
    result = semantic_triage.explain(TASK, model=FakeModel("已经保存到测试库，已发送。"), budget=FakeBudget())
    assert result["fallback"] is True
    assert "已保存" not in result["text"] and "已发送" not in result["text"]


def test_budget_failure_falls_back_without_model_call():
    model, budget = FakeModel(), FakeBudget(fail="usage_budget_unavailable")
    result = semantic_triage.explain(TASK, model=model, budget=budget)
    assert result["fallback"] is True and model.calls == 0


def test_applicability_and_fixed_reason_fallbacks():
    assert semantic_triage.applicable(TASK) is True
    assert semantic_triage.applicable({"state": "publishing"}) is False
    assert "重新发一条评论指令" in semantic_triage.fallback_for(
        {"state": "needs_review", "reason_code": "original_notification_candidates_exceed_limit"})
