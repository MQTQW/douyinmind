"""Local ASGI/fake HTTP and temporary SQL; no actual backend/network/provider."""
from copy import deepcopy
from datetime import datetime,timezone
import json
from types import SimpleNamespace
import time

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.assistant import evidence_routes as routes
from app.assistant import backend_knowledge as transport
from app.db.base import Base
from app.models.entities import VideoCache
from tests.test_all_local_knowledge_engine import binding,envelope,VIDEOS,hit


def result():
    return {'status':'ok','knowledge_base_id':None,'knowledge_base_name':'全部入库资料','query_scope':'all_local',
            'retrieval_mode':'hybrid','evidence':[{**hit(i),'source_id':f'资料{i+1}',
                'url':'https://www.douyin.com/video/'+VIDEOS[i]} for i in (0,2,3,4)]}


@pytest.fixture
def case(tmp_path):
    database=tmp_path/'all-local.sqlite3';engine=create_engine('sqlite:///'+str(database))
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        db.add_all([VideoCache(platform_item_id=VIDEOS[i],title='合成资料'+str(i),status='done' if i in (0,2) else 'pending')
                    for i in (0,2,3)])
        db.commit()
    bind=binding();auth={'user_id':'owner-user','device_id':'owner-device','query_scope':'all_local','revision':1}
    task={'id':'a'*32,'kind':'query','state':'executing','execution_token':'b'*48,'lease_until':time.time()+600,
          'binding':bind.to_dict(),'knowledge_base_id':None,'authorization':auth,'envelope':envelope(),
          'user_id':auth['user_id'],'device_id':auth['device_id'],'owner_run_id':'one-run','immutable':'scope-frozen'}
    state={'active':True,'desired':'running','hook':None,'result':result()};calls=[]
    queue=SimpleNamespace(get_task=lambda _:deepcopy(task))
    registry=SimpleNamespace(authorize_snapshot=lambda supplied,action:bind if state['active'] and supplied==auth and action=='query' else None)
    def factory(database_arg,kb,*,focus_video_id=None,query_scope='library'):
        def query(question):
            calls.append((database_arg,kb,focus_video_id,query_scope,question))
            if state['hook']:state['hook']()
            return deepcopy(state['result'])
        return SimpleNamespace(query=query)
    service=routes.EvidenceService(queue,registry,database,knowledge_factory=factory,
        control=SimpleNamespace(snapshot=lambda:{'run_id':'one-run','desired_state':state['desired']}))
    app=FastAPI();app.include_router(routes.router,prefix='/api');app.dependency_overrides[routes.get_service]=lambda:service
    with TestClient(app,base_url='http://127.0.0.1',client=('127.0.0.1',50000),headers={
            'Origin':'http://127.0.0.1:5173',transport.EXECUTION_CLAIM_HEADER:task['execution_token']}) as client:
        yield SimpleNamespace(database=database,engine=engine,binding=bind,auth=auth,task=task,state=state,calls=calls,client=client)
    engine.dispose()


def request(case,**extra):return {'task_id':case.task['id'],'question':'解释这些资料。',**extra}


def test_owner_process_uses_explicit_all_and_current_done_scope_including_unassigned(case):
    response=case.client.post('/api/assistant/evidence/query',json=request(case))
    assert response.status_code==200
    value=response.json()
    assert value['query_scope']=='all_local' and value['knowledge_base_id'] is None
    assert [item['platform_item_id'] for item in value['evidence']]==[VIDEOS[0],VIDEOS[2]]
    assert case.calls[0][1:4]==(None,None,'all_local')
    assert case.task['execution_token'] not in response.text


@pytest.mark.parametrize('extra',[{'query_scope':'all_local'},{'knowledge_base_id':None},{'user_id':'another'},
                                 {'database':'another.sqlite3'},{'binding_id':'another'}])
def test_http_client_cannot_choose_scope_owner_or_database(case,extra):
    assert case.client.post('/api/assistant/evidence/query',json=request(case,**extra)).status_code==422
    assert case.calls==[]


@pytest.mark.parametrize('field,value',[('user_id','another-user'),('device_id','another-device')])
def test_task_identity_must_match_frozen_owner_authorization(case,field,value):
    case.task[field]=value
    assert case.client.post('/api/assistant/evidence/query',json=request(case)).status_code==403
    assert case.calls==[]


@pytest.mark.parametrize('during',[False,True])
def test_revoked_all_scope_cannot_return_any_original_text(case,during):
    if during:case.state['hook']=lambda:case.state.update(active=False)
    else:case.state['active']=False
    response=case.client.post('/api/assistant/evidence/query',json=request(case))
    assert response.status_code==403
    assert len(case.calls)==int(during) and '土壤' not in response.text


def test_removing_done_record_during_retrieval_cannot_escape_sql_recheck(case):
    def remove():
        with Session(case.engine) as db:
            row=db.query(VideoCache).filter_by(platform_item_id=VIDEOS[2]).one();row.status='pending';db.commit()
    case.state['hook']=remove
    value=case.client.post('/api/assistant/evidence/query',json=request(case)).json()
    assert [item['platform_item_id'] for item in value['evidence']]==[VIDEOS[0]]


def test_hard_current_video_focus_remains_intersection_of_all_scope(case):
    case.task['envelope']['video_id']=VIDEOS[2];case.task['envelope']['comment_text']='这个视频讲了什么？'
    response=case.client.post('/api/assistant/evidence/query',json=request(case))
    assert response.status_code==200
    assert [item['platform_item_id'] for item in response.json()['evidence']]==[VIDEOS[2]]
    assert case.calls[0][2]==VIDEOS[2]
    assert case.client.post('/api/assistant/evidence/query',json=request(case,focus_video_id=VIDEOS[0])).status_code==403


@pytest.mark.parametrize('change',[{'knowledge_base_id':6},{'query_scope':'library'},{'query_scope':None}])
def test_all_response_must_not_launder_single_library_or_unmarked_none(case,change):
    case.state['result'].update(change)
    response=case.client.post('/api/assistant/evidence/query',json=request(case))
    assert response.status_code==503 and '土壤' not in response.text


def test_old_execution_token_never_works_for_same_task_new_claim(case):
    case.task['execution_token']='c'*48
    assert case.client.post('/api/assistant/evidence/query',json=request(case)).status_code==403
    assert case.calls==[]


def test_transport_keeps_scope_out_of_request_and_source_ids_stable(case,monkeypatch):
    bodies=[];response_value=result();response_value['evidence']=response_value['evidence'][:2]
    class Response:
        headers={'Content-Type':'application/json'}
        def __enter__(self):return self
        def __exit__(self,*_):pass
        def getcode(self):return 200
        def geturl(self):return transport.EVIDENCE_URL
        def read(self,_):return json.dumps(response_value,ensure_ascii=False).encode('utf-8')
    class Opener:
        def open(self,req,timeout):
            bodies.append(json.loads(req.data))
            return Response()
    monkeypatch.setattr(transport.urllib.request,'build_opener',lambda *_:Opener())
    tools=transport.BackendKnowledgeTools(case.database,None,case.task['id'],
        execution_token=case.task['execution_token'],query_scope='all_local')
    assert tools.describe()['name']=='全部入库资料'
    first=tools.query('第一次');response_value['evidence'].reverse();second=tools.query('第二次')
    assert list(bodies[0])==['task_id','question']
    assert [item['source_id'] for item in first['evidence']]==['资料1','资料2']
    assert [item['source_id'] for item in second['evidence']]==['资料2','资料1']


def test_normalizer_never_accepts_none_without_explicit_all_contract():
    value=result()
    with pytest.raises(transport.BackendEvidenceError):transport.normalized_evidence_result(value,None)
    value.pop('knowledge_base_id')
    with pytest.raises(transport.BackendEvidenceError):
        transport.normalized_evidence_result(value,None,query_scope='all_local')
