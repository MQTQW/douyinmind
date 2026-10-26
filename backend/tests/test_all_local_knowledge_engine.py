"""All-local is a distinct owner-scoped inventory, never default-KB fallback.

Only temporary SQL and fake retrieval/model objects. No Chroma/provider calls.
"""
from copy import deepcopy
from datetime import datetime, timezone
from hashlib import sha256
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.db.base import Base
from app.models.entities import KnowledgeBase, KnowledgeBaseVideo, VideoCache
from app.mention_agent.contracts import Binding
from app.mention_agent.engine import AgentEngine, TOOLS
from app.mention_agent.knowledge import LocalKnowledgeTools
from app.services.retrieval_errors import RetrievalHits


VIDEOS=[str(7999999000000000100+i) for i in range(5)]
A='https://www.douyin.com/user/all-owner'
B='https://www.douyin.com/user/all-assistant'


def binding(*, all_local=True):
    return Binding('owner-all-binding' if all_local else 'owner-library-binding','owner-device',A,B,
                   None if all_local else 6,True,query_scope='all_local' if all_local else 'library')


def envelope(text='请解释资料中的处理方法。'):
    return {'command_id':'all-command','reference_id':'all-reference','reference_quality':'verified_ui',
            'native_comment_id':None,'sender_profile_url':A,'receiver_profile_url':B,'video_id':VIDEOS[0],
            'comment_text':text,'source_mode':'isolated','evidence':{'real_mention':True,'unique_count':1,
                'sender_verified':True,'video_verified':True,'observed_at_utc':datetime.now(timezone.utc).isoformat()}}


def hit(index=0,chunk='1'):
    return {'platform_item_id':VIDEOS[index],'chunk_id':VIDEOS[index]+':'+chunk,'title':'合成资料'+str(index),
            'text':'语音文字稿：\n[00:01–00:05] 先检查土壤是否湿润。然后再决定是否需要浇水。'}


class Rag:
    def __init__(self,hits):self.hits=hits;self.calls=[]
    def _dense_retrieve(self,question,scope):
        assert scope is not None
        self.calls.append((question,set(scope)))
        return RetrievalHits(self.hits,mode='hybrid')
    def _context_hits(self,hits,scope):
        assert scope is not None
        return hits


@pytest.fixture
def database(tmp_path):
    path=tmp_path/'owner-materials.sqlite3'
    engine=create_engine('sqlite:///'+str(path))
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        db.add_all([KnowledgeBase(id=6,name='编程库',name_key='code'),KnowledgeBase(id=7,name='另一个库',name_key='other')])
        db.add_all([VideoCache(platform_item_id=video,title='合成资料'+str(i),status='done' if i<3 else 'pending')
                    for i,video in enumerate(VIDEOS[:4])])
        db.flush()
        db.add_all([KnowledgeBaseVideo(knowledge_base_id=6,platform_item_id=VIDEOS[0]),
                    KnowledgeBaseVideo(knowledge_base_id=7,platform_item_id=VIDEOS[0]),
                    KnowledgeBaseVideo(knowledge_base_id=7,platform_item_id=VIDEOS[1])])
        db.commit()
    engine.dispose()
    return path


def test_all_inventory_includes_other_library_and_unassigned_done_once(database):
    before=sha256(database.read_bytes()).hexdigest()
    rag=Rag([hit(0),hit(0),hit(1),hit(2),hit(3),hit(4)])
    tools=LocalKnowledgeTools(database,None,query_scope='all_local',rag=rag)
    assert tools.describe()=={'id':None,'name':'全部入库资料','total_count':3,'ready_count':3,'query_scope':'all_local'}
    result=tools.query('说明处理方法')
    assert rag.calls==[('说明处理方法',set(VIDEOS[:3]))]
    assert [item['platform_item_id'] for item in result['evidence']]==VIDEOS[:3]
    assert len({item['source_id'] for item in result['evidence']})==3
    assert result['knowledge_base_id'] is None and result['query_scope']=='all_local'
    assert sha256(database.read_bytes()).hexdigest()==before


def test_explicit_library_never_inherits_all_inventory(database):
    rag=Rag([hit(0),hit(1),hit(2)])
    tools=LocalKnowledgeTools(database,6,rag=rag)
    assert 'query_scope' not in tools.describe()
    result=tools.query('说明处理方法')
    assert rag.calls[0][1]=={VIDEOS[0]}
    assert [item['platform_item_id'] for item in result['evidence']]==[VIDEOS[0]]
    assert 'query_scope' not in result


def test_empty_all_is_explicit_empty_set_and_never_calls_retriever(database):
    engine=create_engine('sqlite:///'+str(database))
    with Session(engine) as db:
        for row in db.query(VideoCache).all():row.status='pending'
        db.commit()
    engine.dispose()
    rag=Rag([hit(4)])
    result=LocalKnowledgeTools(database,None,query_scope='all_local',rag=rag).query('任何问题')
    assert result['status']=='empty' and result['evidence']==[] and result['query_scope']=='all_local'
    assert rag.calls==[]


def test_original_video_focus_intersects_all_instead_of_default_library(database):
    rag=Rag([hit(0),hit(1),hit(2)])
    result=LocalKnowledgeTools(database,None,query_scope='all_local',focus_video_id=VIDEOS[2],rag=rag).query('这个视频')
    assert rag.calls[0][1]=={VIDEOS[2]}
    assert [item['platform_item_id'] for item in result['evidence']]==[VIDEOS[2]]


@pytest.mark.parametrize('kb,scope',[(None,'library'),(0,'all_local'),(6,'all_local'),(None,'unknown'),(None,[])])
def test_implicit_none_or_fake_kbid_cannot_construct_all_access(database,kb,scope):
    with pytest.raises(ValueError):LocalKnowledgeTools(database,kb,query_scope=scope,rag=Rag([]))


class Knowledge:
    def __init__(self,*,all_local=True):self.calls=[];self.all_local=all_local
    def describe(self):
        result={'id':None if self.all_local else 6,'name':'全部入库资料' if self.all_local else '编程库','total_count':3,'ready_count':3}
        if self.all_local:result['query_scope']='all_local'
        return result
    def query(self,question):
        self.calls.append(question)
        result={'status':'ok','knowledge_base_id':None if self.all_local else 6,'knowledge_base_name':self.describe()['name'],
                'retrieval_mode':'hybrid','evidence':[{**hit(2),'source_id':'资料1','url':'https://www.douyin.com/video/'+VIDEOS[2]}]}
        if self.all_local:result['query_scope']='all_local'
        return result


def call(name,args,identity):
    import json
    return SimpleNamespace(message={'role':'assistant','tool_calls':[{'id':identity,'type':'function',
        'function':{'name':name,'arguments':json.dumps(args,ensure_ascii=False)}}]},usage={'total_tokens':5},finish_reason='tool_calls')


class Model:
    def __init__(self,turns):self.turns=list(turns);self.requests=[];self.tools=[]
    def complete(self,messages,tools):
        self.requests.append(deepcopy(messages));self.tools.append(deepcopy(tools))
        return self.turns.pop(0)


def test_all_engine_schema_binds_scope_and_queries_unassigned_source_without_mutating_old_tools():
    before=deepcopy(TOOLS);knowledge=Knowledge()
    model=Model([call('query_knowledge_base',{'question':'处理方法'},'q'),
        call('propose_reply',{'text':'资料建议先检查土壤，再决定是否浇水。','source_ids':['资料1']},'a')])
    result=AgentEngine(model,knowledge).run(binding(),envelope())
    assert result.proposal['kind']=='answer' and knowledge.calls==['处理方法']
    schema=next(item['function']['parameters'] for item in model.tools[0] if item['function']['name']=='query_knowledge_base')
    assert schema['required']==['question'] and set(schema['properties'])=={'question'}
    assert result.proposal['sources'][0]['platform_item_id']==VIDEOS[2]
    assert TOOLS==before


def test_all_model_cannot_supply_legacy_id_to_change_scope():
    knowledge=Knowledge();model=Model([call('query_knowledge_base',{'knowledge_base_id':6,'question':'越界'},'bad'),
        call('query_knowledge_base',{'question':'正确范围'},'good'),
        call('propose_reply',{'text':'先查看土壤，再决定是否浇水。','source_ids':['资料1']},'a')])
    result=AgentEngine(model,knowledge).run(binding(),envelope())
    assert result.proposal['kind']=='answer' and knowledge.calls==['正确范围']


def test_all_does_not_accept_unmarked_library_result_with_null_id():
    knowledge=Knowledge()
    original=knowledge.query
    def wrong(question):
        result=original(question);result.pop('query_scope');return result
    knowledge.query=wrong
    model=Model([call('query_knowledge_base',{'question':'查找'},'q')])
    result=AgentEngine(model,knowledge,max_rounds=1).run(binding(),envelope())
    assert result.proposal is None


def test_all_import_is_refused_before_model_even_if_factory_object_was_supplied():
    model=Model([])
    result=AgentEngine(model,Knowledge(),ingestion=object()).run(binding(),envelope())
    assert result.review_code=='import_all_scope_forbidden' and model.requests==[]


def test_two_verbatim_bases_still_required_in_all_mode_from_actual_tool_text():
    first='先检查土壤是否湿润。';second='然后再决定是否需要浇水。'
    model=Model([call('query_knowledge_base',{'question':'处理方法'},'q'),
        call('propose_reply',{'text':'1.「'+first+'」；2.「'+second+'」','source_ids':['资料1'],
            'evidence_quotes':[{'source_id':'资料1','excerpt':first},{'source_id':'资料1','excerpt':second}]},'a')])
    result=AgentEngine(model,Knowledge()).run(binding(),envelope('请给出两条原文依据。'))
    assert result.proposal['kind']=='answer' and result.proposal['source_ids']==['资料1']
