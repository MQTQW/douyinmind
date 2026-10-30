"""Synthetic bounded correction only: no provider, account, DB or publisher."""
from copy import deepcopy
import json

import pytest

from app.assistant.reply_style import style_reply_text
from app.mention_agent.engine import AgentEngine, PublicReplyValidationError
from tests.test_mention_agent_engine_review import binding, envelope, call, FictionalKnowledge


def query():
    return call('query_knowledge_base', {'knowledge_base_id': 7, 'question': '原文讲了什么'}, 'query')


def answer(text, identity='reply'):
    return call('propose_reply', {'text': text, 'source_ids': ['资料1']}, identity)


class RepairModel:
    def __init__(self, rejected, check_feedback, *, corrected='原文说明了测试步骤。', clarify=False):
        self.rejected, self.check_feedback, self.corrected = rejected, check_feedback, corrected
        self.clarify, self.requests, self.tools = clarify, [], []

    def complete(self, messages, tools):
        self.requests.append(deepcopy(messages))
        self.tools.append(deepcopy(tools))
        if len(self.requests) == 1:
            return query()
        if len(self.requests) == 2:
            return answer(self.rejected, 'invalid')
        assert len(self.requests) == 3, 'correction must stay inside the existing round budget'
        self.check_feedback(json.loads(messages[-1]['content']))
        if self.clarify:
            return call('request_clarification', {'reason_code': 'quote_requirement_unmet'}, 'clarify')
        return answer(self.corrected, 'corrected')


@pytest.mark.parametrize('rejected,violations,controls', [
    ('长' * 318, ['too_long'], 0),
    ('原文\r说明步骤。', ['contains_control_chars'], 1),
    ('@读者 原文说明步骤。', ['contains_at'], 0),
    ('参见HTTPS://synthetic.invalid/private-draft', ['contains_url'], 0),
    ('私有草稿' * 80 + '@读者 https://synthetic.invalid/private-draft\r\x00',
     ['too_long', 'contains_at', 'contains_url', 'contains_control_chars'], 2),
])
def test_fake_model_repairs_each_public_text_failure_without_retrieval_or_audit_body(rejected, violations, controls):
    def check(feedback):
        assert feedback['status'] == 'error' and feedback['code'] == 'invalid_public_reply'
        assert feedback['field'] == 'text' and feedback['violations'] == violations
        assert feedback['observed_chars'] == len(rejected)
        assert feedback['effective_max_chars'] == 317 and feedback['control_count'] == controls
        assert str(len(rejected)) in feedback['hint'] and '317' in feedback['hint']
        assert '单独调用propose_reply' in feedback['hint'] and '无需重复检索' in feedback['hint']
        for violation, hint in [('too_long', '缩短'), ('contains_at', '@'),
                                ('contains_url', '外链'), ('contains_control_chars', '控制字符')]:
            if violation in violations:
                assert hint in feedback['hint']

    model, knowledge, records = RepairModel(rejected, check), FictionalKnowledge(), []
    decision = AgentEngine(model, knowledge, reply_prefix='少爷，').run(
        binding(), envelope(), record=lambda *items: records.append(items))
    assert decision.proposal['text'] == model.corrected and decision.proposal['kind'] == 'answer'
    assert decision.model_rounds == decision.tool_calls == 3 and len(knowledge.queries) == 1
    audit = next(meta for kind, meta in records if kind == 'tool_result' and meta['status'] == 'error')
    assert audit['code'] == 'invalid_public_reply'
    assert audit['public_text_validation'] == {
        'field': 'text', 'violations': violations, 'observed_chars': len(rejected),
        'effective_max_chars': 317, 'control_count': controls}
    assert rejected not in json.dumps(records, ensure_ascii=False)


@pytest.mark.parametrize('value,violations,observed,controls', [
    (None, ['invalid_type'], None, 0),
    ({'private': 'untrusted'}, ['invalid_type'], None, 0),
    (' \n\t', ['empty_text'], 3, 0),
    ('\r', ['empty_text', 'contains_control_chars'], 1, 1),
])
def test_typed_error_keeps_fixed_legacy_code_and_never_retains_input(value, violations, observed, controls):
    engine = AgentEngine(None, None)
    with pytest.raises(PublicReplyValidationError, match='^invalid_public_reply$') as captured:
        engine._public_text(value)
    error = captured.value
    assert isinstance(error, ValueError) and error.args == ('invalid_public_reply',)
    assert error.diagnostic() == {'field': 'text', 'violations': violations, 'observed_chars': observed,
                                  'effective_max_chars': 320, 'control_count': controls}
    assert not hasattr(error, 'text')


def test_quote_hint_appends_specific_length_and_forbidden_item_feedback():
    rejected = '长' * 278 + '@读者\r'
    def check(feedback):
        assert feedback['code'] == 'invalid_public_reply'
        assert feedback['effective_max_chars'] == 277 and feedback['observed_chars'] == len(rejected)
        assert feedback['violations'] == ['too_long', 'contains_at', 'contains_control_chars']
        assert '277' in feedback['hint'] and str(len(rejected)) in feedback['hint']
        assert '@字符' in feedback['hint'] and '控制字符' in feedback['hint']
        assert 'evidence_quotes' in feedback['hint'] and 'quote_requirement_unmet' in feedback['hint']

    model, knowledge = RepairModel(rejected, check, clarify=True), FictionalKnowledge()
    decision = AgentEngine(model, knowledge, reply_prefix='少爷，').run(
        binding(), envelope('请根据测试库回答这个问题，并给出两条原文依据。'))
    assert decision.proposal['kind'] == 'clarification'
    assert len(knowledge.queries) == 1 and decision.model_rounds == 3
    schema = next(tool['function'] for tool in model.tools[0] if tool['function']['name'] == 'propose_reply')
    assert schema['parameters']['properties']['text']['maxLength'] == 277
    assert '277' in schema['description'] and '280' in schema['description']
    assert '有效正文上限为277' in model.requests[0][0]['content']


@pytest.mark.parametrize('prefix,limit', [('', 320), ('少爷，', 317)])
def test_effective_budget_and_valid_answer_are_unchanged(prefix, limit):
    text = '文' * limit
    class Model:
        calls = 0
        def complete(self, messages, tools):
            schema = next(tool['function'] for tool in tools if tool['function']['name'] == 'propose_reply')
            assert schema['parameters']['properties']['text']['maxLength'] == limit
            assert f'最多{limit}字符' in schema['description']
            assert f'最终公开文本上限320字符' in schema['description']
            self.calls += 1
            return query() if self.calls == 1 else answer(text)
    knowledge = FictionalKnowledge()
    engine = AgentEngine(Model(), knowledge, reply_prefix=prefix)
    decision = engine.run(binding(), envelope())
    assert decision.proposal['text'] == text and decision.model_rounds == 2
    assert len(style_reply_text(text, prefix, maximum=320)) == 320
    with pytest.raises(PublicReplyValidationError) as captured:
        engine._public_text(text + '文')
    assert captured.value.violations == ('too_long',)
    assert engine._public_text('  正文\n第二行\t说明  ') == '正文\n第二行\t说明'


def test_unknown_validation_error_never_exposes_exception_or_draft():
    private = 'PRIVATE_EXCEPTION_https://synthetic.invalid/secret'
    class Engine(AgentEngine):
        checks = 0
        def _public_text(self, text, **options):
            self.checks += 1
            if self.checks == 1:
                raise ValueError(private)
            return super()._public_text(text, **options)
    def check(feedback):
        assert feedback == {'status': 'error', 'code': 'invalid_tool_arguments'}
    model, records = RepairModel('未接受的合成草稿', check), []
    decision = Engine(model, FictionalKnowledge()).run(binding(), envelope(), record=lambda *items: records.append(items))
    assert decision.proposal['text'] == model.corrected
    assert private not in json.dumps(records, ensure_ascii=False)
    assert private not in json.dumps(model.requests, ensure_ascii=False)
    assert all('public_text_validation' not in meta for kind, meta in records if kind == 'tool_result')


def test_diagnostics_do_not_extend_model_round_budget_or_truncate_rejected_text():
    class Model:
        calls = 0
        def complete(self, messages, tools):
            self.calls += 1
            return query() if self.calls == 1 else answer('文' * 321, str(self.calls))
    model, knowledge, records = Model(), FictionalKnowledge(), []
    decision = AgentEngine(model, knowledge).run(binding(), envelope(), record=lambda *items: records.append(items))
    assert decision.review_code == 'model_round_budget_exceeded' and decision.proposal is None
    assert model.calls == decision.model_rounds == decision.tool_calls == 4
    assert len(knowledge.queries) == 1
    errors = [meta for kind, meta in records if kind == 'tool_result' and meta['status'] == 'error']
    assert len(errors) == 3 and all(item['public_text_validation']['observed_chars'] == 321 for item in errors)
