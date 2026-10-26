"""Legacy bytes, explicit all-local and trusted save-choice context boundaries."""
from copy import deepcopy

import pytest

from app.mention_agent.contracts import Binding,canonical_json,validate_envelope
from app.mention_agent.engine import AgentEngine
from app.mention_agent.service import AgentService
from tests.test_all_local_knowledge_engine import binding,envelope,Model,call,Knowledge
from tests.test_library_choice_evidence import built


def test_library_serialization_keeps_exact_original_seven_fields_and_all_is_explicit():
    legacy={'binding_id':'legacy','instance_id':'desktop','sender_profile_url':binding().sender_profile_url,
            'receiver_profile_url':binding().receiver_profile_url,'knowledge_base_id':6,
            'public_reply_allowed':True,'active':True}
    restored=Binding.from_dict(legacy)
    assert restored.query_scope=='library' and not restored.is_all_local
    assert restored.to_dict()==legacy and canonical_json(restored.to_dict())==canonical_json(legacy)
    all_payload={**legacy,'knowledge_base_id':None,'query_scope':'all_local'}
    assert Binding.from_dict(all_payload).to_dict()==all_payload
    assert Binding.from_dict(all_payload).is_all_local


@pytest.mark.parametrize('changes',[
    {'knowledge_base_id':None},{'knowledge_base_id':0},{'knowledge_base_id':True},
    {'query_scope':'library'},{'query_scope':'all_local'},{'query_scope':'all_local','knowledge_base_id':0},
    {'query_scope':'all_local','knowledge_base_id':6},{'query_scope':'unknown','knowledge_base_id':None},
])
def test_no_implicit_none_numeric_sentinel_or_extra_scope_variant(changes):
    payload=binding(all_local=False).to_dict()|changes
    with pytest.raises(ValueError):Binding.from_dict(payload)


def test_dataclass_constructor_cannot_bypass_scope_id_relationship():
    with pytest.raises(ValueError):Binding('bad','desktop',binding().sender_profile_url,binding().receiver_profile_url,None,True)
    with pytest.raises(ValueError):Binding('bad','desktop',binding().sender_profile_url,binding().receiver_profile_url,6,True,query_scope='all_local')


def test_normal_envelope_still_requires_real_mention_and_unknown_evidence_kind_rejects():
    original=envelope()
    assert validate_envelope(original,binding())==original
    for change in ({'real_mention':False},{'source_kind':'made_up_reply'},{'source_kind':None}):
        bad=deepcopy(original);bad['evidence'].update(change)
        with pytest.raises(ValueError):validate_envelope(bad,binding())


def test_strict_choice_envelope_is_only_exception_to_real_mention():
    selected,env=built(mention=False)
    assert validate_envelope(env,selected)==env and env['comment_text']=='编程库'
    with pytest.raises(ValueError):validate_envelope(env,binding())
    bad=deepcopy(env);bad['evidence']['root_chain_verified']=False
    with pytest.raises(ValueError):validate_envelope(bad,selected)


class ChoiceKnowledge:
    def describe(self):return {'id':7,'name':'编程库','total_count':0,'ready_count':0}
    def query(self,*_):raise AssertionError('saving must not query a fake body')


class Import:
    def __init__(self,*,deny=False):self.calls=[];self.deny=deny
    def save_current_video(self,kb):
        self.calls.append(kb)
        if self.deny:raise ValueError('explicit_save_command_required')
        return {'status':'succeeded'}
    def proposal(self):return {'kind':'import_status','text':'合成资料已完成入库。','source_ids':[],'sources':[],
                              'import_job_id':'fixture-job','import_state':'succeeded'}


def context(env):
    return {'choice_id':env['evidence']['choice_id'],'root_comment_text':env['evidence']['root_comment_text'],
            'selection_reference_id':env['reference_id'],'selected_library_name':'编程库'}


def test_actual_selection_text_preserved_and_model_chooses_save_with_trusted_background():
    selected,env=built(mention=False);model=Model([call('save_current_video',{'knowledge_base_id':7},'save')]);ingestion=Import()
    before=deepcopy(env)
    result=AgentEngine(model,ChoiceKnowledge(),ingestion=ingestion,trusted_save_selection=context(env),
        automatic_import_receipt=True).run(selected,env)
    assert result.proposal['kind']=='import_status' and ingestion.calls==[7]
    assert env==before and model.requests[0][1]['content']=='编程库'
    assert '@B 请保存当前视频' in model.requests[0][0]['content']
    assert '不授予保存' in model.requests[0][0]['content']


@pytest.mark.parametrize('key,value',[
    ('choice_id','b'*32),('selection_reference_id','other-reference'),
    ('selected_library_name','其他库'),('root_comment_text','@B 保存另一个视频'),
])
def test_trusted_context_must_match_actual_choice_and_selected_binding(key,value):
    selected,env=built(mention=False);model=Model([]);ingestion=Import()
    altered=context(env)|{key:value}
    result=AgentEngine(model,ChoiceKnowledge(),ingestion=ingestion,trusted_save_selection=altered).run(selected,env)
    assert result.review_code=='trusted_save_selection_invalid' and model.requests==[] and ingestion.calls==[]


def test_choice_without_trusted_context_cannot_start_model_or_save():
    selected,env=built(mention=False);model=Model([]);ingestion=Import()
    result=AgentEngine(model,ChoiceKnowledge(),ingestion=ingestion).run(selected,env)
    assert result.review_code=='trusted_save_selection_invalid' and model.requests==[]


def test_valid_background_never_overrides_real_ingestion_permission_callback():
    selected,env=built(mention=False);ingestion=Import(deny=True)
    model=Model([call('save_current_video',{'knowledge_base_id':7},'save'),
                 call('request_clarification',{'reason_code':'unsupported_action'},'clarify')])
    result=AgentEngine(model,ChoiceKnowledge(),ingestion=ingestion,trusted_save_selection=context(env),
                       automatic_import_receipt=True).run(selected,env)
    assert result.proposal['kind']=='clarification' and ingestion.calls==[7]


def test_model_cannot_supply_trusted_choice_context_as_tool_authority():
    selected,env=built(mention=False);ingestion=Import()
    model=Model([call('save_current_video',{'knowledge_base_id':7,'trusted_save_selection':context(env)},'bad'),
                 call('request_clarification',{'reason_code':'unsupported_action'},'clarify')])
    result=AgentEngine(model,ChoiceKnowledge(),ingestion=ingestion,trusted_save_selection=context(env)).run(selected,env)
    assert result.proposal['kind']=='clarification' and ingestion.calls==[]


def test_context_is_not_accepted_on_regular_mention_even_with_import_object():
    selected,choice=built();normal=envelope()
    normal['sender_profile_url']=selected.sender_profile_url;normal['receiver_profile_url']=selected.receiver_profile_url
    model=Model([])
    result=AgentEngine(model,ChoiceKnowledge(),ingestion=Import(),trusted_save_selection=context(choice)).run(selected,normal)
    assert result.review_code=='trusted_save_selection_invalid' and model.requests==[]


def test_service_uses_explicit_all_scope_for_default_sql_only_tool_and_refuses_all_import(tmp_path):
    class Store:
        def put_binding(self,value):self.value=value
    store=Store()
    service=AgentService(store,binding(),database=tmp_path/'not-opened.sqlite3',model=Model([]))
    assert service.knowledge.query_scope=='all_local' and service.knowledge.knowledge_base_id is None
    assert store.value==binding().to_dict()
    with pytest.raises(ValueError,match='import_all_scope_forbidden'):
        AgentService(store,binding(),knowledge=Knowledge(),model=Model([]),allow_import=True)


def test_service_forwards_program_selection_context_without_rewriting_actual_child(monkeypatch):
    from app.mention_agent import service as module
    from app.mention_agent.engine import Decision
    selected,env=built(mention=False);captured={}
    class Store:
        def put_binding(self,value):self.binding=value
        def get_binding(self,_):return self.binding
        def list_tasks(self,_):return []
        def enqueue(self,_,value):
            self.row={'task_id':'fixture-task','state':'queued','envelope':deepcopy(value)}
            return self.row
        def claim(self,*_,**__):return {**self.row,'claim_token':'fixture-token'}
        def needs_review(self,_,__,reason):self.row.update(state='needs_review',reason_code=reason)
        def get_task(self,_):return self.row
        def get_reply(self,_):return None
    class Engine:
        def __init__(self,model,knowledge,**options):captured.update(options)
        def run(self,binding,value,**_):
            captured['actual_envelope']=deepcopy(value)
            return Decision(review_code='fixture_stop')
    monkeypatch.setattr(module,'AgentEngine',Engine)
    supplied=context(env);store=Store();model=Model([])
    service=AgentService(store,selected,knowledge=ChoiceKnowledge(),model=model,allow_import=True,
        import_factory=lambda *_,**__:Import(),trusted_save_selection=supplied)
    assert service.process(env)['state']=='needs_review'
    assert captured['trusted_save_selection']==supplied
    assert captured['actual_envelope']==env and captured['actual_envelope']['comment_text']=='编程库'
    assert model.requests==[]
