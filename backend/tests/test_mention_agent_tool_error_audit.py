"""Safe fixed validation codes in future audit steps; fake models/tools only."""
import json

import pytest

from app.mention_agent.engine import AgentEngine
from app.mention_agent.answer_requirements import parse_answer_requirements
from tests.test_mention_agent_engine_review import binding, envelope, call, ScriptModel, FictionalKnowledge, query_turn


def run_error(arguments, *, comment='请根据测试库回答这个问题', mutate=None):
    records = []
    model = ScriptModel([query_turn(), call('propose_reply', arguments, 'bad-proposal'),
        call('request_clarification', {'reason_code': 'ambiguous_request'}, 'clarification')])
    engine = AgentEngine(model, FictionalKnowledge())
    if mutate: mutate(engine)
    result = engine.run(binding(), envelope(comment), record=lambda *items: records.append(items))
    errors = [metadata for kind, metadata in records if kind == 'tool_result' and metadata['status'] == 'error']
    return result, errors, records


@pytest.mark.parametrize('arguments,expected', [
    ({'text': '合成回答。', 'source_ids': ['not-a-source']}, 'invalid_source_ids'),
    ({'text': '合成回答。', 'source_ids': ['资料1'], 'unexpected': 'synthetic-secret-marker'}, 'invalid_tool_arguments'),
    ({'text': '长' * 321, 'source_ids': ['资料1']}, 'invalid_public_reply'),
    ({'text': '资料2说明了测试步骤。', 'source_ids': ['资料1']}, 'undeclared_source_reference'),
])
def test_validation_error_audit_persists_only_fixed_code(arguments, expected):
    decision, errors, records = run_error(arguments)
    assert len(errors) == 1 and errors[0]['code'] == expected
    assert 'synthetic-secret-marker' not in json.dumps(records, ensure_ascii=False)
    assert set(errors[0]) == {'round', 'tool_name', 'status', 'source_count', 'elapsed_ms', 'code'}


def test_original_quote_schema_error_audit_is_fixed_and_does_not_change_validation():
    decision, errors, records = run_error({'text': '合成回答。', 'source_ids': ['资料1']}, comment='请给出两条原文依据。')
    assert errors[0]['code'] == 'evidence_quotes_required'


def test_untrusted_exception_text_is_not_copied_to_code_or_any_audit():
    def mutate(engine):
        def fail(value): raise ValueError('synthetic-private-provider-detail')
        engine._public_text = fail
    decision, errors, records = run_error({'text': '合成回答。', 'source_ids': ['资料1']}, mutate=mutate)
    assert errors[0]['code'] == 'invalid_tool_arguments'
    assert 'synthetic-private-provider-detail' not in json.dumps(records, ensure_ascii=False)


def test_bare_two_evidence_is_not_retroactively_a_two_original_quote_requirement():
    assert parse_answer_requirements('解释Git暂存区的作用，并给出两条依据。') is None
    original = parse_answer_requirements('解释Git暂存区的作用，并给出两条原文依据。')
    assert original.minimum == original.maximum == 2
