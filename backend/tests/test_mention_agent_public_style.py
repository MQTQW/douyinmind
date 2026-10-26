"""New public rendering only; real evidence and old stored text stay intact."""
from copy import deepcopy

import pytest

from app.mention_agent.engine import AgentEngine, TOOLS
from app.mention_agent.service import AgentService
from app.mention_agent.store import AgentStore
from tests.test_mention_agent_engine_review import (
    FictionalKnowledge, ScriptModel, binding, envelope, call, query_turn, proposal_turn,
)


def run(turns, *, knowledge=None, text='请说说怎样整理文件，详细讲清楚一点。'):
    records=[];model=ScriptModel(turns)
    result=AgentEngine(model,knowledge or FictionalKnowledge()).run(binding(),envelope(text),
        record=lambda *row:records.append(row))
    return result,model,records


def test_public_answer_has_no_generated_source_footer_but_keeps_actual_sources():
    result,model,records=run([query_turn(),proposal_turn('先把文件按主题整理，再挑出当前需要的那一份。')])
    assert result.proposal['text']=='先把文件按主题整理，再挑出当前需要的那一份。'
    assert result.proposal['source_ids']==['资料1']
    assert result.proposal['sources'][0]['chunk_id']=='synthetic-chunk'
    refs=next(meta['evidence_refs'] for kind,meta in records if kind=='tool_result' and 'evidence_refs' in meta)
    assert refs[0]['source_id']=='资料1' and refs[0]['text_sha256']
    prompt=model.requests[0][0]['content']
    assert '简单问题简洁回答' in prompt and '用户要求详细' in prompt
    assert '不必每次用‘视频里说’开头' in prompt and '不套固定的总分总' in prompt
    assert '不冒充真人' in prompt and '不声称能够规避' in prompt
    assert '程序会追加来源标签' not in prompt and '尽量150字' not in prompt


def test_model_supplied_valid_footer_is_removed_after_source_validation_and_persisted_privately(tmp_path):
    model=ScriptModel([query_turn(),proposal_turn('先按主题整理文件。\n[来源：资料1]')])
    store=AgentStore(tmp_path/'agent.sqlite3')
    result=AgentService(store,binding(),model=model,knowledge=FictionalKnowledge()).process(envelope())
    reply=store.get_reply(result['task_id'])
    assert reply['text']=='先按主题整理文件。' and result['reply']['text']==reply['text']
    assert reply['source_ids']==['资料1'] and reply['sources']==result['reply']['sources']
    assert reply['sources'][0]['chunk_id']=='synthetic-chunk'


@pytest.mark.parametrize('turns,code',[
    ([proposal_turn()], 'query_required_before_answer'),
    ([query_turn(),call('propose_reply',{'text':'编造的答案。','source_ids':['资料99']},'fake-id')], 'invalid_source_ids'),
    ([query_turn(),proposal_turn('整理文件。\n[来源：资料99]')], 'undeclared_source_reference'),
    ([query_turn(),proposal_turn('资料99还说了其他结论。')], 'undeclared_source_reference'),
    ([query_turn(),proposal_turn('[来源：资料1]')], 'invalid_public_reply'),
])
def test_no_footer_style_cannot_hide_missing_or_fake_grounding(turns,code):
    result,model,records=run(turns)
    assert result.proposal is None or result.proposal['kind']!='answer'
    assert code in [meta.get('code') for kind,meta in records if kind=='tool_result']


def test_empty_actual_evidence_still_cannot_support_an_answer():
    result,_,records=run([query_turn(),proposal_turn()],knowledge=FictionalKnowledge(result_extra={'evidence':[]}))
    assert result.proposal is None or result.proposal['kind']!='answer'
    assert 'invalid_source_ids' in [meta.get('code') for kind,meta in records if kind=='tool_result']


def test_detailed_body_can_use_existing_final_length_budget_without_footer_reserve():
    body='根据主题整理文件。'*33
    assert 280<len(body)<=320
    seen=[]
    class Capture(ScriptModel):
        def complete(self,messages,tools):
            seen.append(deepcopy(tools));return super().complete(messages,tools)
    model=Capture([query_turn(),proposal_turn(body)])
    result=AgentEngine(model,FictionalKnowledge(evidence_extra={'text':body})).run(binding(),envelope('请详细解释。'))
    assert result.proposal['text']==body
    tool=next(item for item in seen[0] if item['function']['name']=='propose_reply')
    assert tool['function']['parameters']['properties']['text']['maxLength']==320
    assert next(item for item in TOOLS if item['function']['name']=='propose_reply')['function']['parameters']['properties']['text']['maxLength']==280


def test_over_existing_final_length_budget_is_still_rejected():
    result,_,records=run([query_turn(),proposal_turn('甲'*321)])
    assert result.proposal is None or result.proposal['kind']!='answer'
    assert 'invalid_public_reply' in [meta.get('code') for kind,meta in records if kind=='tool_result']


def test_old_persisted_reply_is_not_rendered_again_or_replaced(tmp_path):
    current=binding();env=envelope();store=AgentStore(tmp_path/'agent.sqlite3')
    store.put_binding(current.to_dict());task=store.enqueue(current.binding_id,env);claim=store.claim(task['task_id'])
    old={'kind':'answer','text':'以前保存的回答。\n[来源：资料1]','source_ids':['资料1'],
        'sources':[{'source_id':'资料1','chunk_id':'old-synthetic-chunk'}]}
    store.complete(task['task_id'],claim['claim_token'],old)
    model=ScriptModel([])
    result=AgentService(store,current,model=model,knowledge=FictionalKnowledge()).process(env)
    assert result['reply']['text']==old['text'] and result['created'] is False and not model.requests
    assert store.get_reply(task['task_id'])['proposal']==old
