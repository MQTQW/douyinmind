"""Actual service/engine/import ledger/publisher, with fake providers and UI."""
from datetime import datetime, timezone
import json

from app.mention_agent.contracts import Binding
from app.mention_agent.imports import ImportTools
from app.mention_agent.model import ModelTurn
from app.mention_agent.publisher import ReplyPublisher
from app.mention_agent.service import AgentService
from app.mention_agent.store import AgentStore


def scope(tmp_path):
    binding=Binding('bound-import','desktop-import','https://www.douyin.com/user/sender-synthetic',
                    'https://www.douyin.com/user/receiver-synthetic',7,True,True)
    store=AgentStore(tmp_path/'agent.sqlite3');store.put_binding(binding.to_dict())
    env={'command_id':'command-import-01','reference_id':'reference-import-01','reference_quality':'verified_ui',
         'native_comment_id':None,'sender_profile_url':binding.sender_profile_url,'receiver_profile_url':binding.receiver_profile_url,
         'video_id':'7999999000000000011','comment_text':'@助手 请把这个视频加入“测试库”知识库。测试编号 DM-IMPORT-01。',
         'source_mode':'live_ui','evidence':{'real_mention':True,'sender_verified':True,'video_verified':True,
                                          'unique_count':1,'observed_at_utc':datetime.now(timezone.utc).isoformat()}}
    return binding,store,env


class Knowledge:
    def describe(self):return {'id':7,'name':'测试库','total_count':0,'ready_count':0}
    def query(self,question):raise AssertionError('Import receipts must not pretend to be RAG excerpts')


class Model:
    def __init__(self,calls):self.calls=list(calls);self.count=0
    def complete(self,messages,tools):
        assert {'save_current_video','get_import_status','propose_import_status'} <= {t['function']['name'] for t in tools}
        name,args=self.calls[self.count];self.count+=1
        return ModelTurn({'role':'assistant','content':None,'tool_calls':[{'id':'call-'+str(self.count),
            'type':'function','function':{'name':name,'arguments':json.dumps(args)}}]}, {}, 'tool_calls')


class Backend:
    def __init__(self,bad_index=False):self.posts=0;self.gets=0;self.body=None;self.bad_index=bad_index
    def _response(self):
        return dict(self.body,state='succeeded',membership_exists=True,cache_status='done',
                    indexed_content_chunks=0 if self.bad_index else 3)
    def submit(self,body):self.posts+=1;self.body=body.copy();return self._response()
    def status(self,request_id):
        self.gets+=1;assert request_id==self.body['request_id'];return self._response()


class Adapter:
    def __init__(self):self.submits=0
    def prepare_reply(self,target,text):
        return {k:target[k] for k in ('sender_profile_url','receiver_profile_url','video_id','comment_text',
            'reference_id','native_comment_id')} | {'unique_count':1,'real_mention':True,'current_receiver_verified':True,
            'observed_at_utc':datetime.now(timezone.utc).isoformat()}
    def submit_reply(self,target,text,*,before_submit):
        before_submit();self.submits+=1
        return {'outcome':'b_view_visible','exact_body_in_original_thread':True}
    def save_reply_evidence(self,*args):pass


def service(store,binding,model,backend):
    return AgentService(store,binding,knowledge=Knowledge(),model=model,allow_import=True,
        import_factory=lambda *a,**k:ImportTools(*a,**k,client=backend,wait_seconds=0))


def test_import_completion_then_single_publication_and_reopen_dedup(tmp_path):
    binding,store,env=scope(tmp_path);backend=Backend();model=Model([
        ('save_current_video',{'knowledge_base_id':7}),('propose_import_status',{})])
    result=service(store,binding,model,backend).process(env)
    assert result['state']=='answer_ready'
    assert result['reply']['kind']=='import_status' and result['reply']['source_ids']==[]
    assert '已加入“测试库”' in result['reply']['text']
    assert backend.posts==1 and backend.gets>=1 and model.count==2
    adapter=Adapter();published=ReplyPublisher(store,binding,adapter,tmp_path/'reply-evidence').publish(result['task_id'])
    assert published['state']=='b_view_visible' and adapter.submits==1
    fresh=AgentStore(tmp_path/'agent.sqlite3')
    replay=service(fresh,binding,Model([]),backend).process(env)
    assert replay['state']=='b_view_visible' and replay['created'] is False and replay['ran_model'] is False
    assert ReplyPublisher(fresh,binding,adapter,tmp_path/'reply-evidence').publish(result['task_id'])['state']=='not_sendable'
    assert backend.posts==1 and adapter.submits==1


def test_new_status_comment_reuses_import_without_second_post(tmp_path):
    binding,store,env=scope(tmp_path);backend=Backend()
    first=service(store,binding,Model([('save_current_video',{'knowledge_base_id':7}),('propose_import_status',{})]),backend).process(env)
    followup={**env,'command_id':'command-status-02','reference_id':'reference-status-02',
              'comment_text':'@助手 请查询当前视频的入库进度，测试编号 DM-STATUS-02。'}
    second=service(store,binding,Model([('get_import_status',{}),('propose_import_status',{})]),backend).process(followup)
    assert second['state']=='answer_ready' and '已加入“测试库”' in second['reply']['text']
    assert store.get_task_ingestion(first['task_id'])['job_id']==store.get_task_ingestion(second['task_id'])['job_id']
    assert backend.posts==1


def test_false_backend_success_cannot_produce_completed_reply(tmp_path):
    binding,store,env=scope(tmp_path);backend=Backend(bad_index=True)
    model=Model([('save_current_video',{'knowledge_base_id':7}),('get_import_status',{}),('propose_import_status',{})])
    result=service(store,binding,model,backend).process(env)
    assert backend.posts==1 and result['state']=='answer_ready'
    assert '尚未确认完成' in result['reply']['text']
    assert '已加入' not in result['reply']['text']
