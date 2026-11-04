"""The verified final body is the only body allowed to be stored and sent."""
from copy import deepcopy
from hashlib import sha256

import pytest

from app.mention_agent.engine import AgentEngine
from app.mention_agent.service import AgentService
from app.mention_agent.store import AgentStore
from app.mention_agent.publisher import ReplyPublisher
from app.assistant.reply_humanize import validate_frozen_reply
from tests.test_mention_agent_engine_review import (
    FictionalKnowledge, ScriptModel, binding, envelope, query_turn, proposal_turn, call,
)
from tests.test_mention_agent_publisher import FakeAdapter


def execute(text, *, original=None, prefix='', comment=None):
    records = []
    model = ScriptModel([query_turn(), proposal_turn(text)])
    result = AgentEngine(model, FictionalKnowledge(evidence_extra={'text': original or text}),
                         reply_prefix=prefix).run(binding(), envelope(comment or '请根据测试库说明步骤。'),
        record=lambda kind, data: records.append((kind, data)))
    return result, records


def codes(records):
    return [data.get('code') for kind, data in records if kind == 'tool_result']


def test_formatted_answer_keeps_original_quotes_numbers_and_identifiers():
    quote = '首先，Python 3.12 的阈值为 1.25。'
    body = '## 根据资料显示\n首先，原文写道：“' + quote + '”\n最后，请按 `pip_install` 执行。'
    result, _ = execute(body)
    assert result.proposal['kind'] == 'answer'
    final = result.proposal['text']
    assert quote in final and 'pip_install' in final
    assert '3.12' in final and '1.25' in final and not final.startswith('#')
    assert result.proposal['source_ids'] == ['资料1']


def test_final_quote_is_rechecked_after_any_formatter_change(monkeypatch):
    body = '原文写道：“首先，将资料逐项核对后再继续处理。”'
    monkeypatch.setattr('app.mention_agent.engine.prepare_public_body',
                        lambda text, *args, **kwargs: text.replace('逐项', '直接'))
    result, records = execute(body)
    assert result.proposal is None or result.proposal['kind'] != 'answer'
    assert 'public_quote_not_supported' in codes(records)


def test_removed_required_quote_is_not_hidden_by_valid_structured_quote_payload(monkeypatch):
    first = '首先，整理收藏中的主题内容。'
    second = '其次，核对每个步骤对应的资料。'
    body = f'原文：“{first}”；“{second}”'
    monkeypatch.setattr('app.mention_agent.engine.prepare_public_body',
                        lambda text, *args, **kwargs: text.replace(f'；“{second}”', ''))
    records = []
    model = ScriptModel([query_turn(), call('propose_reply', {
        'text': body, 'source_ids': ['资料1'],
        'evidence_quotes': [{'source_id': '资料1', 'excerpt': first},
                            {'source_id': '资料1', 'excerpt': second}],
    }, 'two-quotes')])
    result = AgentEngine(model, FictionalKnowledge(evidence_extra={'text': first + second})).run(
        binding(), envelope('请说明步骤，并给出两条原文依据。'),
        record=lambda kind, data: records.append((kind, data)))
    assert result.proposal is None or result.proposal['kind'] != 'answer'
    assert 'evidence_quote_not_visible' in codes(records)


def test_write_claim_spans_are_computed_from_formatted_final_text():
    quote = '历史提交已经保存到本地版本库。'
    body = f'首先，原文写道：“{quote}”我已经保存了这个视频。'
    result, records = execute(body, original=quote)
    assert result.proposal is None or result.proposal['kind'] != 'answer'
    assert 'write_success_not_supported' in codes(records)


@pytest.mark.parametrize('prefix,code', [
    ('我已保存，', 'write_success_not_supported'),
    ('资料2，', 'undeclared_source_reference'),
])
def test_final_salutation_does_not_bypass_evidence_or_write_checks(prefix, code):
    result, records = execute('请按原文步骤核对资料。', prefix=prefix)
    assert result.proposal is None or result.proposal['kind'] != 'answer'
    assert code in codes(records)


def test_final_body_survives_service_store_send_and_exact_readback_unchanged(tmp_path):
    original = '## 根据资料显示\n首先，使用 Python 3.12，阈值设为 1.25。'
    model = ScriptModel([query_turn(), proposal_turn(original)])
    store = AgentStore(tmp_path / 'agent.sqlite3')
    env = {**envelope(), 'source_mode': 'live_ui'}
    result = AgentService(store, binding(), model=model,
        knowledge=FictionalKnowledge(evidence_extra={'text': original}), reply_prefix='少爷，').process(env)
    reply = store.get_reply(result['task_id'])
    assert reply['text'].startswith('少爷，') and not reply['text'].startswith('少爷，#')
    assert '3.12' in reply['text'] and '1.25' in reply['text']
    frozen = deepcopy(reply)
    adapter = FakeAdapter()
    publisher = ReplyPublisher(store, binding(), adapter, tmp_path / 'evidence',
        content_gate=lambda text, kind: validate_frozen_reply(text, kind, maximum=320))
    assert publisher.publish(result['task_id'])['state'] == 'b_view_visible'
    saved = store.get_reply(result['task_id'])
    assert adapter.observed_text == saved['text'] == frozen['text'] == result['reply']['text']
    assert saved['source_ids'] == frozen['source_ids'] and saved['sources'] == frozen['sources']
    assert saved['evidence']['reply_sha256'] == sha256(saved['text'].encode()).hexdigest()
    assert publisher.publish(result['task_id'])['state'] == 'not_sendable'
    assert adapter.clicks == 1 and len(model.requests) == 2


def test_publisher_has_no_option_to_enable_post_freeze_rewriting(tmp_path):
    store = AgentStore(tmp_path / 'agent.sqlite3')
    with pytest.raises(ValueError, match='immutable_reply_required'):
        ReplyPublisher(store, binding(), FakeAdapter(), tmp_path / 'evidence', require_exact_text=False)


@pytest.mark.parametrize('automatic', [False, True])
@pytest.mark.parametrize('body', ['https://example.invalid/untrusted', '回' * 318, ''])
def test_rejected_import_candidate_cannot_escape_as_a_completed_proposal(automatic, body):
    class InvalidReceipt:
        saves = 0

        def save_current_video(self, library):
            assert library == 7
            self.saves += 1
            return {'status': 'processing'}

        def proposal(self):
            return {'kind': 'import_status', 'text': body, 'source_ids': [], 'sources': [],
                    'import_job_id': 'synthetic-job', 'import_state': 'processing'}

    receipt = InvalidReceipt()
    tool = (call('save_current_video', {'knowledge_base_id': 7}, 'save') if automatic
            else call('propose_import_status', {}, 'receipt'))
    records = []
    result = AgentEngine(ScriptModel([tool]), FictionalKnowledge(), ingestion=receipt,
        automatic_import_receipt=automatic, reply_prefix='少爷，').run(
            binding(), envelope('请把这个视频保存到“测试库”知识库。'),
            record=lambda kind, data: records.append((kind, data)))
    assert result.proposal is None or result.proposal['kind'] != 'import_status'
    assert any(code in {'invalid_import_status', 'invalid_public_reply'} for code in codes(records))
    assert receipt.saves == (1 if automatic else 0)
