"""Trace full text must be the evidence actually sent to the model, never more."""
from copy import deepcopy
import json

import pytest

from app.core.config import settings
from app.services import rag_service as module
# Reuse the real retrieval/fusion + in-memory scope boundary from the existing
# diagnostic integration checks, with only external clients replaced.
from tests.test_retrieval_observation_integration import offline_retrieval, scoped_db, document


def answer_with_capture(service, db, monkeypatch, streaming, query='检修前关闭主电源后还要满足什么条件？'):
    calls=[]
    def chat(**kwargs):
        calls.append(kwargs)
        return "先关闭主电源，不能带电拆卸。[来源: 资料1]"
    def stream(**kwargs):
        calls.append(kwargs)
        yield "先关闭主电源，不能带电拆卸。[来源: 资料1]"
    monkeypatch.setattr(module.llm_client,'chat',chat)
    monkeypatch.setattr(module.llm_client,'stream_chat',stream)
    if streaming:
        events=list(service.answer_stream(db,query,None,knowledge_base_id=1))
        assert events[-1]==('done',{'ok':True})
        assert not any(name=='error' for name,_ in events)
        result=next(payload for name,payload in events if name=='meta')
    else:
        result=service.answer(db,query,None,knowledge_base_id=1)
    assert len(calls)==1, 'Viewing evidence must not add a judge or summary model call'
    separator='\n\n内容片段：\n' if result['route_type']=='db_content' else '\n\n相关内容：\n'
    context=calls[0]['user_prompt'].split(separator,1)[1]
    actual_bodies=[block.split('\n',1)[1] for block in context.split('\n\n---\n\n')]
    assert [chunk['full_text'] for chunk in result['trace']['chunks']]==actual_bodies
    assert result['trace']['context_stats']['provided_chars']==len(context)
    assert len(context)<=settings.rag_prompt_max_context_chars
    return result,context


@pytest.mark.parametrize('streaming',[False,True])
@pytest.mark.parametrize('outage',[False,True])
def test_full_evidence_preserves_important_tail_and_never_returns_excluded_candidates(
    offline_retrieval,scoped_db,monkeypatch,streaming,outage,
):
    state=offline_retrieval
    state.outage=outage
    monkeypatch.setattr(settings,'rag_context_count',1)
    monkeypatch.setattr(settings,'rag_prompt_max_context_chars',8000)
    important='不得在电源接通时进行拆卸，重启前必须等待17秒。'
    wanted=document('local:1','[00:00–00:45] 检修前关闭主电源。'+
                    '同一段原文还说明检查绝缘、现场标识和交接记录。'*16+'\n[00:45–00:50] '+important)
    excluded=document('other:1','[00:00–00:05] EXCLUDED_BY_CONTEXT_LIMIT 这是另一段检修原文。',video='other')
    foreign=document('foreign:1','[00:00–00:05] CROSS_SCOPE_PRIVATE 检修时关闭主电源。',video='foreign')
    metadata={**document('local:9','',index=9),'text':'视频标题：METADATA_NOT_EVIDENCE\n作者：合成'}
    state.dense=[wanted,excluded,foreign]
    state.documents=[wanted,excluded,foreign,metadata]
    before=deepcopy(state.documents)
    result,context=answer_with_capture(module.RagService(),scoped_db,monkeypatch,streaming)
    assert len(result['trace']['chunks'])==1
    chunk=result['trace']['chunks'][0]
    assert chunk['chunk_id']==wanted['chunk_id']
    assert chunk['text']==wanted['text'][:200]
    assert chunk['full_text']==wanted['text']
    assert important not in chunk['text'] and important in chunk['full_text']
    assert important in context and '\n[00:45–00:50]' in chunk['full_text']
    serialized=json.dumps(result,ensure_ascii=False)
    assert all(private not in serialized for private in ('EXCLUDED_BY_CONTEXT_LIMIT','CROSS_SCOPE_PRIVATE','METADATA_NOT_EVIDENCE'))
    assert result['trace']['retrieval_details']['counts']['final_hits']==2
    assert result['trace']['context_stats']['provided_chunks']==1
    assert len(state.embeddings)==1
    assert state.documents==before


@pytest.mark.parametrize('streaming',[False,True])
def test_full_evidence_reports_only_the_budget_bounded_first_hit_including_its_marker(
    offline_retrieval,scoped_db,monkeypatch,streaming,
):
    state=offline_retrieval
    monkeypatch.setattr(settings,'rag_context_count',8)
    monkeypatch.setattr(settings,'rag_prompt_max_context_chars',180)
    wanted=document('local:1','[00:00–00:05] 检修前必须关闭主电源。'+
                    '这仍然是同一段原文里的背景说明。'*30+
                    'AFTER_BUDGET_PRIVATE 原完整文稿末尾另有重要参数。')
    state.dense=[wanted]
    state.documents=[wanted]
    result,context=answer_with_capture(module.RagService(),scoped_db,monkeypatch,streaming)
    chunk=result['trace']['chunks'][0]
    assert '[此片段超过长度限制，后文未提供]' in context
    assert '[此片段超过长度限制，后文未提供]' in chunk['full_text']
    assert chunk['full_text']!=wanted['text']
    assert chunk['text']==chunk['full_text'][:200]
    assert 'AFTER_BUDGET_PRIVATE' not in json.dumps(result,ensure_ascii=False)
    assert 'AFTER_BUDGET_PRIVATE' not in context


@pytest.mark.parametrize('streaming',[False,True])
def test_library_summary_full_evidence_is_also_the_actual_provided_context(
    offline_retrieval,scoped_db,monkeypatch,streaming,
):
    state=offline_retrieval
    monkeypatch.setattr(settings,'rag_context_count',8)
    monkeypatch.setattr(settings,'rag_prompt_max_context_chars',8000)
    wanted=document('local:1','[00:00–00:05] 检修先关闭主电源。'+
                    '本段是现场交接和检查情况的原始记录。'*18+
                    '\n[00:05–00:10] 不能省略验电，重启必须等待17秒。')
    state.dense=[wanted]
    state.documents=[wanted]
    result,context=answer_with_capture(module.RagService(),scoped_db,monkeypatch,streaming,query='总结一下')
    assert result['route_type']=='db_content'
    assert result['trace']['chunks'][0]['full_text']==wanted['text']
    assert '不能省略验电，重启必须等待17秒。' in context
    assert '不能省略验电' not in result['trace']['chunks'][0]['text']
