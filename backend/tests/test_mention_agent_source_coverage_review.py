"""Source declarations and literal excerpts, without providers or live storage."""
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import pytest

from app.mention_agent.contracts import Binding
from app.mention_agent.engine import AgentEngine
from app.mention_agent.model import ModelTurn


def binding():
    return Binding('source-review', 'source-review-instance',
        'https://www.douyin.com/user/synthetic-source-A',
        'https://www.douyin.com/user/synthetic-source-B', 7, True)


def envelope():
    b = binding()
    return {'command_id': 'source-review-command', 'reference_id': 'source-review-reference',
        'reference_quality': 'verified_ui', 'native_comment_id': None,
        'sender_profile_url': b.sender_profile_url, 'receiver_profile_url': b.receiver_profile_url,
        'video_id': '7900000000000000010', 'comment_text': '请概括这份合成资料的要点。',
        'source_mode': 'isolated', 'evidence': {'real_mention': True, 'sender_verified': True,
            'video_verified': True, 'unique_count': 1, 'observed_at_utc': datetime.now(timezone.utc).isoformat()}}


def call(name, arguments, identity):
    return ModelTurn({'role': 'assistant', 'content': '', 'tool_calls': [{'id': identity,
        'type': 'function', 'function': {'name': name, 'arguments': json.dumps(arguments, ensure_ascii=False)}}]},
        {'total_tokens': 7}, 'tool_calls')


def answer(text, ids, identity='answer'):
    return call('propose_reply', {'text': text, 'source_ids': ids}, identity)


class Model:
    def __init__(self, turns):
        self.turns = list(turns)
        self.requests = []

    def complete(self, messages, tools):
        self.requests.append({'messages': deepcopy(messages), 'tools': deepcopy(tools)})
        return self.turns.pop(0) if self.turns else call('request_clarification',
            {'reason_code': 'ambiguous_request'}, 'clarify-' + str(len(self.requests)))


class Knowledge:
    def __init__(self, texts):
        self.texts, self.calls = texts, []

    def describe(self):
        return {'id': 7, 'name': '合成资料库', 'ready_count': len(self.texts), 'total_count': len(self.texts)}

    def query(self, question):
        self.calls.append(question)
        return {'status': 'ok', 'knowledge_base_id': 7, 'knowledge_base_name': '合成资料库', 'evidence': [
            {'source_id': '资料' + str(i), 'platform_item_id': '79000000000000000' + str(i),
             'chunk_id': 'synthetic:' + str(i), 'title': '合成来源' + str(i),
             'url': 'https://www.douyin.com/video/79000000000000000' + str(i), 'text': text}
            for i, text in enumerate(self.texts, 1)]}


def run(texts, turns, **options):
    knowledge = Knowledge(texts)
    model = Model([call('query_knowledge_base', {'knowledge_base_id': 7, 'question': '合成问题'}, 'query'), *turns])
    records = []
    decision = AgentEngine(model, knowledge, **options).run(binding(), envelope(), record=lambda *row: records.append(row))
    return decision, model, knowledge, records


def test_a_literal_reference_cannot_borrow_an_undeclared_provided_source():
    phrase = '先核对当前设备身份，再决定资料的查询范围。'
    decision, model, knowledge, records = run(['这一段只讲排队。', phrase],
        [answer('原文写道：“' + phrase + '”', ['资料1'])])
    assert decision.proposal is None or decision.proposal['kind'] != 'answer'
    assert len(knowledge.calls) == 1
    assert len(model.requests) <= 4
    assert any(meta.get('code') == 'public_quote_not_supported' for kind, meta in records if kind == 'tool_result')


def test_four_actual_declared_sources_are_not_rejected_by_an_artificial_three_source_cap():
    texts = ['第' + str(i) + '部分说明不同的合成步骤。' for i in range(1, 9)]
    ids = ['资料' + str(i) for i in range(1, 5)]
    decision, model, _, _ = run(texts, [answer('前四部分依次介绍准备、核对、处理与复查。', ids)])
    assert decision.proposal['kind'] == 'answer'
    assert decision.proposal['source_ids'] == ids
    assert len(decision.proposal['sources']) == 4
    assert len(model.requests) == 2


def test_literal_text_outside_the_actual_tool_character_budget_is_not_support():
    phrase = '原文最后特别说明必须先核对归属再处理资料。'
    decision, model, _, _ = run(['背景内容' * 40 + phrase],
        [answer('原文写道：“' + phrase + '”', ['资料1'])], evidence_chars=100)
    provided = json.loads(model.requests[1]['messages'][-1]['content'])['evidence'][0]['text']
    assert len(provided) == 100 and phrase not in provided
    assert decision.proposal is None or decision.proposal['kind'] != 'answer'


def test_provided_contexts_are_not_automatically_promoted_to_declared_sources():
    phrase = '先核对当前设备身份，再决定资料的查询范围。'
    decision, model, _, _ = run([phrase, '其它合成内容。', '第三份不同资料。', '第四份不同资料。'],
        [answer('原文写道：“' + phrase + '”', ['资料1'])])
    assert decision.proposal['kind'] == 'answer'
    assert decision.proposal['source_ids'] == ['资料1']
    assert [source['source_id'] for source in decision.proposal['sources']] == ['资料1']
    assert '[来源' not in decision.proposal['text']
    assert len(model.requests) == 2


def test_natural_quoted_names_and_short_terms_are_not_claimed_to_be_transcript_excerpts():
    decision, _, _, _ = run(['这份资料用于说明合成示例。'],
        [answer('用“约翰·冯·诺伊曼”和“集中式版本管理”作为名称示例，接着说明主题。', ['资料1'])])
    assert decision.proposal['kind'] == 'answer'


def test_english_name_abbreviations_and_named_titles_are_not_excerpt_claims():
    for text in ['用“Prof. John Fitzgerald Kennedy Jr.”作为一个虚构名称示例。',
                 '这个演示标题名为“为什么需要核对身份？”，下面解释步骤。']:
        decision, _, _, records = run(['这份资料用于说明合成示例。'], [answer(text, ['资料1'])])
        assert decision.proposal['kind'] == 'answer'
        assert not any('quote_support' in metadata for kind, metadata in records)


def test_unsupported_full_sentence_quote_is_not_a_terminology_exemption():
    decision, _, _, records = run(['这份资料只讲文件归类。'],
        [answer('“每个陌生账号都应自动获得全部资料权限。”这个说法不在本轮原文。', ['资料1'])])
    assert decision.proposal is None or decision.proposal['kind'] != 'answer'
    assert any(meta.get('code') == 'public_quote_not_supported' for kind, meta in records)


def test_literal_support_audit_links_only_declared_ids_and_hashes_whitespace_normalized_text():
    phrase = '先核对当前设备身份，\n再决定资料的查询范围。'
    public = '先核对当前设备身份， 再决定资料的查询范围。'
    decision, _, _, records = run([phrase, phrase], [answer('原文写道：“' + public + '”', ['资料1'])])
    assert decision.proposal['source_ids'] == ['资料1']
    query = next(meta for kind, meta in records if kind == 'tool_result' and meta['tool_name'] == 'query_knowledge_base')
    proposed = next(meta for kind, meta in records if kind == 'tool_result' and meta['tool_name'] == 'propose_reply')
    assert query['source_count'] == 2 and proposed['source_count'] == 1
    assert proposed['source_ids'] == ['资料1']
    assert proposed['quote_support'] == {'kind': 'literal_match_only', 'quotes': [
        {'excerpt_sha256': hashlib.sha256(public.encode()).hexdigest(), 'source_ids': ['资料1']}]}
    assert public not in json.dumps(records, ensure_ascii=False)


def test_model_can_correct_its_declared_quote_source_inside_the_existing_round_budget():
    phrase = '先核对当前设备身份，再决定资料的查询范围。'
    decision, model, knowledge, records = run(['这里只讲文件归类。', phrase], [
        answer('原文写道：“' + phrase + '”', ['资料1'], 'wrong'),
        answer('原文写道：“' + phrase + '”', ['资料2'], 'correct')])
    assert decision.proposal['kind'] == 'answer' and decision.proposal['source_ids'] == ['资料2']
    assert len(model.requests) == 3 and len(knowledge.calls) == 1
    assert [meta['code'] for kind, meta in records if kind == 'tool_result' and meta['status'] == 'error'] == ['public_quote_not_supported']


def test_source_limit_is_eight_unique_provided_ids_and_never_unknown_or_duplicate_ids():
    texts = ['第' + str(i) + '份合成资料。' for i in range(1, 10)]
    ids = ['资料' + str(i) for i in range(1, 9)]
    decision, model, _, _ = run(texts, [answer('前八份材料分别说明八个合成主题。', ids)])
    assert decision.proposal['source_ids'] == ids and len(model.requests) == 2
    schema = next(tool['function'] for tool in model.requests[0]['tools'] if tool['function']['name'] == 'propose_reply')
    assert schema['parameters']['properties']['source_ids']['maxItems'] == 8
    for invalid in [ids + ['资料9'], ['资料1', '资料1'], ['资料99']]:
        decision, _, _, records = run(texts, [answer('无效来源声明。', invalid)])
        assert decision.proposal is None or decision.proposal['kind'] != 'answer'
        assert any(meta.get('code') == 'invalid_source_ids' for kind, meta in records)


def test_a_verified_attributed_historical_save_statement_is_not_a_claim_of_current_import():
    phrase = '历史提交已经保存到本地版本库。'
    decision, _, knowledge, _ = run([phrase], [answer('原文写道：“' + phrase + '”这是对版本记录的描述。', ['资料1'])])
    assert decision.proposal['kind'] == 'answer'
    assert len(knowledge.calls) == 1


@pytest.mark.parametrize('claim', ['我已经保存了这个视频。', '这条视频已入库。', '我已上传资料。',
    '我已删除记录。', '我已导入视频。', '我已重置任务。', '我已收藏视频。', '我已经完成索引。'])
def test_a_verified_quote_never_exempts_a_false_write_claim_outside_its_span(claim):
    phrase = '历史提交已经保存到本地版本库。'
    decision, _, _, records = run([phrase], [answer('原文写道：“' + phrase + '”' + claim, ['资料1'])])
    assert decision.proposal is None or decision.proposal['kind'] != 'answer'
    assert any(meta.get('code') == 'write_success_not_supported' for kind, meta in records)


def test_bare_quotation_or_old_history_does_not_exempt_operational_success_language():
    phrase = '历史提交已经保存到本地版本库。'
    decision, _, _, records = run([phrase], [answer('“' + phrase + '”', ['资料1'])])
    assert decision.proposal is None or decision.proposal['kind'] != 'answer'
    assert any(meta.get('code') == 'write_success_not_supported' for kind, meta in records)
    decision, _, _, records = run(['本轮只有另一段原文。'], [answer('原文写道：“' + phrase + '”', ['资料1'])],
        conversation_context={'prior_questions': ['先前的版本记录问题'], 'previous_answer': phrase})
    assert decision.proposal is None or decision.proposal['kind'] != 'answer'
    assert any(meta.get('code') == 'public_quote_not_supported' for kind, meta in records)
