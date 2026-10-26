"""Direct per-tool timing with synthetic providers/clock; no DB or network."""
from copy import deepcopy

import pytest

from app.mention_agent import engine as engine_module
from app.mention_agent.engine import AgentEngine, ToolExecutionFailure
from app.mention_agent.model import ModelTurn
from tests.test_mention_agent_engine_review import (
    FictionalKnowledge, ScriptModel, binding, call, envelope, proposal_turn, query_turn,
)


class Clock:
    now = 0.0

    def advance(self, seconds):
        self.now += seconds

    def monotonic(self):
        return self.now


class TimedModel(ScriptModel):
    def __init__(self, clock, turns):
        super().__init__(turns); self.clock = clock

    def complete(self, messages, tools):
        self.clock.advance(.020)
        return super().complete(messages, tools)


class TimedKnowledge(FictionalKnowledge):
    def __init__(self, clock):
        super().__init__(); self.clock = clock

    def query(self, question):
        self.clock.advance(.137)
        return super().query(question)


def test_tool_duration_excludes_model_and_step_persistence_time(monkeypatch):
    clock = Clock(); records = []
    monkeypatch.setattr(engine_module.time, "monotonic", clock.monotonic)
    def record(kind, metadata):
        records.append((kind, deepcopy(metadata)))
        if kind == "tool_call":
            clock.advance(5)  # A slow audit transaction before the operation.
        elif kind == "tool_result":
            clock.advance(7)  # Another audit transaction after the measurement.
    result = AgentEngine(TimedModel(clock, [query_turn(), proposal_turn()]), TimedKnowledge(clock)).run(
        binding(), envelope(), record=record)
    assert result.proposal["kind"] == "answer"
    tools = [item for kind, item in records if kind == "tool_result"]
    assert [(item["tool_name"], item["elapsed_ms"]) for item in tools] == [
        ("query_knowledge_base", 137), ("propose_reply", 0)]
    assert [item["elapsed_ms"] for kind, item in records if kind == "model_response"] == [20, 20]


def test_handled_invalid_evidence_keeps_measured_time_without_becoming_success(monkeypatch):
    clock = Clock(); records = []
    monkeypatch.setattr(engine_module.time, "monotonic", clock.monotonic)
    class InvalidScope(TimedKnowledge):
        def query(self, question):
            return {**super().query(question), "knowledge_base_id": 999}
    model = TimedModel(clock, [query_turn()])
    decision = AgentEngine(model, InvalidScope(clock)).run(binding(), envelope(), record=lambda *args: records.append(args))
    measured = next(item for kind, item in records if kind == "tool_result" and item["tool_name"] == "query_knowledge_base")
    assert measured["status"] == "error" and measured["elapsed_ms"] == 137
    assert decision.proposal["kind"] == "clarification"
    assert len(model.requests) == 2


def test_unhandled_query_failure_preserves_existing_failure_and_no_fake_zero_result(monkeypatch):
    clock = Clock(); records = []
    monkeypatch.setattr(engine_module.time, "monotonic", clock.monotonic)
    class Failing(TimedKnowledge):
        def query(self, question):
            self.clock.advance(.137)
            raise RuntimeError("synthetic private failure")
    with pytest.raises(ToolExecutionFailure, match="knowledge_query_failed"):
        AgentEngine(TimedModel(clock, [query_turn()]), Failing(clock)).run(
            binding(), envelope(), record=lambda *args: records.append(args))
    assert len([item for kind, item in records if kind == "tool_call"]) == 1
    assert not any(kind == "tool_result" for kind, _ in records)
    assert "synthetic private failure" not in repr(records)


def test_serial_tools_in_one_turn_have_independent_durations(monkeypatch):
    clock = Clock(); records = []
    monkeypatch.setattr(engine_module.time, "monotonic", clock.monotonic)
    combined = deepcopy(call("list_knowledge_bases", {}, "list-1").message)
    combined["tool_calls"].extend(deepcopy(query_turn().message["tool_calls"]))
    decision = AgentEngine(TimedModel(clock, [ModelTurn(combined, {}), proposal_turn()]), TimedKnowledge(clock)).run(
        binding(), envelope(), record=lambda *args: records.append(args))
    assert decision.proposal["kind"] == "answer"
    assert [(item["tool_name"], item["elapsed_ms"]) for kind, item in records if kind == "tool_result"] == [
        ("list_knowledge_bases", 0), ("query_knowledge_base", 137), ("propose_reply", 0)]
