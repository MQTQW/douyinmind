"""Task measurements survive waiting/publication and never adopt other runs."""
from copy import deepcopy

import pytest

from app.assistant.legacy_bridge import (
    LegacyBridge, recorded_stage_timings, recorded_usage, video_processing_timing,
)
from app.assistant.queue import AssistantQueue
from app.assistant.runtime import AssistantRuntime
from tests.test_assistant_product_bridge import system, receive
from tests.test_assistant_legacy_extensions import QueryKnowledge, QueryModel
from tests.test_mention_agent_import_end_to_end import Backend, Model, Adapter


def step(kind, elapsed=None, tool=None):
    metadata={}
    if elapsed is not None:
        metadata['elapsed_ms']=elapsed
    if tool:
        metadata['tool_name']=tool
    return {'kind':kind,'metadata':metadata}


def test_incomplete_or_unmeasured_groups_remain_unknown():
    steps=[step('model_request'),step('model_response',8),step('model_request'),
           step('tool_call',tool='query_knowledge_base'),step('tool_result',4,'query_knowledge_base'),
           step('tool_call',tool='query_knowledge_base')]
    assert recorded_stage_timings(steps)=={}
    assert recorded_usage([])=={'model_responses':0}
    assert 'elapsed_ms' not in recorded_usage([],elapsed_ms=True)
    steps.extend([step('model_response',2),step('tool_result',0,'query_knowledge_base')])
    assert recorded_stage_timings(steps)=={'model_ms':10,'retrieval_ms':4}


def test_invalid_group_duration_does_not_become_partial_total():
    for value in [True,-1,float('inf'),'4']:
        steps=[step('model_request'),step('model_response',5),step('model_request'),step('model_response',value)]
        assert 'model_ms' not in recorded_stage_timings(steps)


def projection():
    return {'run_id':'synthetic-run','request_id':'synthetic-job','video_id':'7999999000000000011',
            'stages_seconds':{'download':2.5,'recognition':0,'indexing':1.2}}


@pytest.mark.parametrize('mutation', ['other_request','other_video','extra_stage','bool','nan','huge','empty_run','blank_run','path_run'])
def test_optional_video_measurement_cannot_adopt_wrong_or_invalid_run(mutation):
    value=projection()
    if mutation=='other_request': value['request_id']='other-job'
    elif mutation=='other_video': value['video_id']='7999999000000000022'
    elif mutation=='extra_stage': value['stages_seconds']['estimated_total']=50
    elif mutation=='bool': value['stages_seconds']['download']=True
    elif mutation=='nan': value['stages_seconds']['recognition']=float('nan')
    elif mutation=='huge': value['stages_seconds']['download']=10**1000
    elif mutation=='empty_run': value['run_id']=''
    elif mutation=='blank_run': value['run_id']=' '
    elif mutation=='path_run': value['run_id']='invalid/run'
    job={'job_id':'synthetic-job','video_id':'7999999000000000011'}
    assert video_processing_timing(value,job) is None


def test_waiting_or_publication_cannot_rewrite_actual_execution_duration(tmp_path):
    b,a,e,r,m,q,h,c=system(tmp_path)
    task=receive(q,h,r,{**e,'comment_text':'@助手 请根据编程知识库说明Git。'})
    model=QueryModel();adapter=Adapter()
    bridge=LegacyBridge(r,q,c,agent_path=a.path,conversation=h,database=tmp_path/'unused-business.db',
        model_factory=lambda:model,knowledge_factory=lambda _:QueryKnowledge(),adapter=adapter)
    runtime=AssistantRuntime(q,c,execute=bridge.execute,publish=bridge.publish,reconcile=bridge.reconcile)
    ready=runtime.execute_once()
    measured=ready['details']['timings']['execution_ms']
    assert type(measured) is int and measured>=0
    assert ready['usage']['elapsed_ms']==measured
    assert set(ready['details']['timings']) >= {'model_ms','retrieval_ms'}
    # Simulate a long later update, not an elapsed processing interval.
    with a._tx() as db:
        db.execute('UPDATE agent_tasks SET updated_at=created_at+7200 WHERE task_id=?',(ready['agent_task_id'],))
    reconciled=bridge.reconcile(q.get_task(task['id']))
    assert reconciled['details']['timings']['execution_ms']==measured
    legacy_input=deepcopy(q.get_task(task['id']))
    legacy_input['details'].pop('timings')
    legacy_input['usage']['elapsed_ms']=7200000
    unknown=bridge.reconcile(legacy_input)
    assert 'execution_ms' not in unknown['details'].get('timings',{})
    assert 'elapsed_ms' not in unknown['usage']
    published=runtime.publish_once()
    assert published['state']=='b_view_visible' and adapter.submits==1 and len(model.requests)==2
    assert published['details']['timings']['execution_ms']==measured
    assert published['usage']['elapsed_ms']==measured


def test_exact_video_processing_record_survives_fast_import_publication_and_reopen(tmp_path):
    b,a,e,r,m,q,h,c=system(tmp_path)
    receive(q,h,r,{**e,'comment_text':'@助手 存到编程库'})
    class TimedBackend(Backend):
        def _response(self):
            response=super()._response()
            response['video_processing']={'run_id':'synthetic-exact-run',
                'request_id':self.body['request_id'],'video_id':self.body['video_id'],
                'stages_seconds':{'download':2.5,'recognition':0,'indexing':1.2}}
            return response
    backend=TimedBackend();model=Model([('save_current_video',{'knowledge_base_id':7})]);adapter=Adapter()
    bridge=LegacyBridge(r,q,c,agent_path=a.path,conversation=h,database=tmp_path/'unused-business.db',
        import_client=backend,model_factory=lambda:model,knowledge_factory=lambda _:QueryKnowledge(),adapter=adapter)
    runtime=AssistantRuntime(q,c,execute=bridge.execute,publish=bridge.publish,reconcile=bridge.reconcile)
    ready=runtime.execute_once()
    assert ready['state']=='ready_to_publish'
    visible=runtime.publish_once()
    processing=visible['details']['timings']['video_processing']
    assert processing['request_id']==a.get_task_ingestion(visible['agent_task_id'])['job_id']
    assert processing['stages_seconds']=={'download':2.5,'recognition':0,'indexing':1.2}
    assert 'retrieval_ms' not in visible['details']['timings']
    reopened=AssistantQueue(q.path).get_task(visible['id'])
    assert reopened['details']['timings']==visible['details']['timings']
    assert backend.posts==1 and model.count==1 and adapter.submits==1
