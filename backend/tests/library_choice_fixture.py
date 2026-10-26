"""Synthetic frozen-choice contract for Store/Bridge integration tests."""
from datetime import datetime, timezone
from types import SimpleNamespace

from app.assistant.library_choice import LibraryChoiceStore, DATABASE_NAME
from app.mention_agent.contracts import Binding
from app.mention_agent.intake import make_envelope
from app.mention_agent.choice_evidence import choice_reference_id, _identifier


def authorization(binding, *, grant='grant-root', action='query'):
    result={'registry_id':'registry-choice','user_id':'user-choice','device_id':'device-choice',
        'grant_id':grant,'binding_id':binding.binding_id,'instance_id':binding.instance_id,
        'knowledge_base_id':binding.knowledge_base_id,'public_reply_allowed':binding.public_reply_allowed,
        'action':action,'user_revision':1,'device_revision':1,'grant_revision':1,'grant_version':1}
    if binding.is_all_local:result['query_scope']='all_local'
    return result


def choice_fixture(tmp_path, *, store=None, root_number=1, clock=None, source_mode='isolated',all_local_root=False,
                   option_ordinals=None,option_names=None):
    binding=Binding('root-choice','choice-instance','https://www.douyin.com/user/choice-A',
        'https://www.douyin.com/user/choice-B',None if all_local_root else 7,True,True,
        query_scope='all_local' if all_local_root else 'library')
    selected={identity:Binding('selected-'+str(identity),binding.instance_id,binding.sender_profile_url,
        binding.receiver_profile_url,identity,True,True) for identity in (7,8)}
    text='@助手 请保存这个视频。根指令'+str(root_number)
    notice={'source_profile_url':binding.sender_profile_url,'exact_text':text,'semantic_fingerprint':'root-cover-'+str(root_number)}
    proof={'sender_profile_url':binding.sender_profile_url,'receiver_profile_url':binding.receiver_profile_url,
        'command_text':text,'video_id':'7999999000000000033','native_comment_id':None,'unique_count':1,
        'real_mention':True,'sender_verified':True,'video_verified':True}
    detail={'ok':True,**proof,'evidence':proof,'source_url':'https://www.douyin.com/video/'+proof['video_id'],
        'diagnostics':{'id_conflict':False,'command_match_count':1,'visible_video_ids':[proof['video_id']]}}
    root=make_envelope(notice,detail,binding,source_mode)
    options=[{'knowledge_base_id':identity,'name':name,
        'authorization':authorization(selected[identity],grant='grant-'+str(identity),action='import')}
        for identity,name in ((7,'编程库'),(8,'生活库'))]
    for option in options:
        if option_ordinals is not None:option['ordinal']=option_ordinals[option['knowledge_base_id']]
        if option_names is not None:option['name']=option_names[option['knowledge_base_id']]
    clock=clock or (lambda:datetime.now(timezone.utc).timestamp())
    store=store or LibraryChoiceStore.prepare(tmp_path/DATABASE_NAME,clock=clock)
    ask_text='你有2个知识库：'+'；'.join(str(option.get('ordinal',index))+'.'+option['name']
        for index,option in enumerate(options,1))+'。请回复编号或库名。'
    row=store.create(root_task_id='root-task-'+str(root_number),root_binding=binding.to_dict(),root_envelope=root,
        ask_ref='ask-intent-'+str(root_number),ask_text=ask_text,options=options,authorization=authorization(binding))
    ask_reply_ref=_identifier('verified-ui-ask',{'root':root['reference_id'],'text':ask_text,'author':binding.receiver_profile_url})
    return SimpleNamespace(store=store,root_binding=binding,selected_bindings=selected,root_envelope=root,
        options=options,created=row,ask_text=ask_text,ask_ref=row['ask_ref'],ask_reply_ref=ask_reply_ref,clock=clock)


def visible(case):
    proof={'source_kind':'library_choice_ask','sender_profile_url':case.root_binding.sender_profile_url,
        'receiver_profile_url':case.root_binding.receiver_profile_url,'ask_author_profile_url':case.root_binding.receiver_profile_url,
        'receiver_verified':True,'video_id':case.root_envelope['video_id'],'root_reference_id':case.root_envelope['reference_id'],
        'root_comment_text':case.root_envelope['comment_text'],'ask_ref':case.ask_ref,'ask_reply_ref':case.ask_reply_ref,
        'ask_text':case.ask_text,'unique_count':1,'exact_body_in_original_thread':True,
        'reference_quality':'verified_ui','native_comment_id':None,
        'observed_at_utc':datetime.fromtimestamp(case.clock(),timezone.utc).isoformat()}
    return case.store.record_ask_outcome(case.created['choice_id'],ask_ref=case.ask_ref,outcome='b_view_visible',
        ask_reply_ref=case.ask_reply_ref,proof=proof)


def selection(case, *, text='1', target=7, native=None, parent_scope='verified_ask_parent', mention=False):
    binding=case.selected_bindings[target]
    reference=choice_reference_id(sender=binding.sender_profile_url,receiver=binding.receiver_profile_url,
        video_id=case.root_envelope['video_id'],native_comment_id=native,comment_text=text,
        root_reference_id=case.root_envelope['reference_id'],ask_reply_ref=case.ask_reply_ref)
    proof={'source_kind':'library_choice_reply','evidence_version':1,'choice_id':case.created['choice_id'],
        'instance_id':binding.instance_id,'real_mention':mention,'sender_verified':True,'receiver_verified':True,
        'video_verified':True,'unique_count':1,'direct_reply_to_ask':parent_scope=='verified_ask_parent',
        'parent_scope':parent_scope,'root_chain_verified':True,'ask_visible_unique':True,
        'receiver_reply_target_verified':True,'unique_pending_choice':True,
        'sender_profile_url':binding.sender_profile_url,'receiver_profile_url':binding.receiver_profile_url,
        'video_id':case.root_envelope['video_id'],'root_reference_id':case.root_envelope['reference_id'],
        'root_comment_text':case.root_envelope['comment_text'],'ask_ref':case.ask_ref,'ask_reply_ref':case.ask_reply_ref,
        'ask_text':case.ask_text,'selection_reference_id':reference,'selection_text':text,
        'observed_at_utc':datetime.fromtimestamp(case.clock(),timezone.utc).isoformat()}
    return {'command_id':_identifier('command',{'binding_id':binding.binding_id,'instance_id':binding.instance_id,
        'reference_id':reference,'comment_text':text,'source_mode':case.root_envelope['source_mode']}),
        'reference_id':reference,'reference_quality':'native' if native is not None else 'verified_ui',
        'native_comment_id':native,'sender_profile_url':binding.sender_profile_url,'receiver_profile_url':binding.receiver_profile_url,
        'video_id':case.root_envelope['video_id'],'comment_text':text,'source_mode':case.root_envelope['source_mode'],'evidence':proof}


def reserve(case, *, envelope=None, target=7, revision=None):
    envelope=envelope or selection(case,target=target)
    row=case.store.get(case.created['choice_id'])
    return case.store.reserve_selection(case.created['choice_id'],expected_revision=revision or row['revision'],
        selection_ref=envelope['reference_id'],selection_envelope=envelope,selected_kb_id=target,
        selected_authorization=next(option['authorization'] for option in case.options if option['knowledge_base_id']==target),
        proof=envelope['evidence'])
