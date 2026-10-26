"""Focus is optional within the trusted query scope, never a model target ID."""
import json

import pytest

from app.mention_agent.engine import AgentEngine, TOOLS
from tests.test_mention_agent_source_coverage_review import Model, Knowledge, binding, envelope, call, answer


class FocusKnowledge(Knowledge):
    def __init__(self):
        super().__init__(['合成原文说明需要先核对范围。'])
        self.focus_calls = []

    def query_with_focus(self, question, *, focus='auto'):
        self.focus_calls.append((question, focus))
        return super().query(question)


def run(knowledge, arguments):
    records = []
    model = Model([call('query_knowledge_base', arguments, 'query'), answer('合成原文说明先核对范围。', ['资料1'])])
    result = AgentEngine(model, knowledge).run(binding(), envelope(), record=lambda *row: records.append(row))
    return result, model, records


@pytest.mark.parametrize('focus', ['auto', 'authorized_scope', 'current_video'])
def test_capable_tools_accept_only_bounded_focus_without_additional_model_calls(focus):
    knowledge = FocusKnowledge()
    decision, model, _ = run(knowledge, {'knowledge_base_id': 7, 'question': '合成问题', 'focus': focus})
    assert decision.proposal['kind'] == 'answer'
    assert knowledge.focus_calls == [('合成问题', focus)]
    assert len(model.requests) == 2
    schema = next(tool['function'] for tool in model.requests[0]['tools'] if tool['function']['name'] == 'query_knowledge_base')
    assert schema['parameters']['properties']['focus']['enum'] == ['auto', 'authorized_scope', 'current_video']
    assert 'focus' not in schema['parameters']['required']


def test_omitted_focus_calls_the_old_query_even_with_a_capable_backend():
    knowledge = FocusKnowledge()
    decision, _, _ = run(knowledge, {'knowledge_base_id': 7, 'question': '合成问题'})
    assert decision.proposal['kind'] == 'answer' and knowledge.calls == ['合成问题']
    assert knowledge.focus_calls == []


@pytest.mark.parametrize('focus', [None, True, [], 'another_user', 'http://127.0.0.1:8000'])
def test_invalid_focus_is_rejected_before_any_retrieval(focus):
    knowledge = FocusKnowledge()
    decision, _, records = run(knowledge, {'knowledge_base_id': 7, 'question': '合成问题', 'focus': focus})
    assert decision.proposal is None or decision.proposal['kind'] != 'answer'
    assert not knowledge.calls and not knowledge.focus_calls
    assert any(meta.get('code') == 'invalid_evidence_focus' for kind, meta in records)


@pytest.mark.parametrize('extra', [{'video_id': '7900000000000000099'}, {'user_id': 'other'}, {'query_scope': 'all_local'}])
def test_focus_does_not_admit_arbitrary_targets_or_scope_fields(extra):
    knowledge = FocusKnowledge()
    decision, _, _ = run(knowledge, {'knowledge_base_id': 7, 'question': '合成问题', 'focus': 'authorized_scope', **extra})
    assert decision.proposal is None or decision.proposal['kind'] != 'answer'
    assert not knowledge.calls and not knowledge.focus_calls


def test_a_noncallable_capability_keeps_the_legacy_tool_schema_and_rejects_focus():
    knowledge = Knowledge(['合成原文。'])
    knowledge.query_with_focus = None
    decision, model, _ = run(knowledge, {'knowledge_base_id': 7, 'question': '合成问题', 'focus': 'authorized_scope'})
    schema = next(tool['function'] for tool in model.requests[0]['tools'] if tool['function']['name'] == 'query_knowledge_base')
    assert 'focus' not in schema['parameters']['properties']
    assert decision.proposal is None or decision.proposal['kind'] != 'answer'
    assert not knowledge.calls
    assert 'focus' not in next(tool['function'] for tool in TOOLS if tool['function']['name'] == 'query_knowledge_base')['parameters']['properties']
