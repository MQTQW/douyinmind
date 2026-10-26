"""A permission change during nonce read must prevent the one public click.

Real temporary ledgers plus a fake publisher adapter; no browser/account/send.
"""
from datetime import datetime,timezone

import pytest

from app.mention_agent.contracts import Binding
from app.mention_agent.publisher import ReplyPublisher
from tests.test_assistant_choice_controller import case,root,draft


class RecordingAdapter:
    def __init__(self):self.clicks=0
    def prepare_reply(self,target,text):
        fields=('sender_profile_url','receiver_profile_url','video_id','comment_text','reference_id','native_comment_id')
        return {key:target[key] for key in fields}|{'real_mention':True,'unique_count':1,
            'current_receiver_verified':True,'observed_at_utc':datetime.now(timezone.utc).isoformat()}
    def submit_reply(self,target,text,*,before_submit):
        before_submit()
        self.clicks+=1
        return {'outcome':'b_view_visible','exact_body_in_original_thread':True}
    def clear_prepared_reply(self,*args):pass
    def save_reply_evidence(self,*args):pass


@pytest.mark.parametrize('changed_scope',['owner_permission','catalog_menu'])
def test_root_authority_revoked_during_reserved_nonce_read_prevents_submit(case,changed_scope,monkeypatch):
    task,session,_=root(case);identity=draft(case,task,session)
    original=case.agent.get_reply;changed=[False]
    def read_then_revoke(task_id):
        reply=original(task_id)
        if task_id==identity and reply and reply['state']=='reserved' and not changed[0]:
            changed[0]=True
            if changed_scope=='owner_permission':
                case.registry.set_local_owner_access(case.prior['user_id'],case.prior['device_id'],
                    query_all_local=False,catalog_read_allowed=False,public_reply_allowed=False,
                    verified_by='synthetic-owner')
            else:case.catalog[0]['name']='发表门闩期间已经改名的库'
        return reply
    monkeypatch.setattr(case.agent,'get_reply',read_then_revoke)
    adapter=RecordingAdapter();binding=Binding.from_dict(task['binding'])
    publisher=ReplyPublisher(case.agent,binding,adapter,case.agent.path.parent/'reply-evidence',
        scope_authorizer=lambda current:current==binding and case.controller.authority_current(task),
        publication_allowed=lambda:True)
    result=publisher.publish(identity)
    assert changed[0] and not case.controller.authority_current(task)
    # Running/unpaused publication alone cannot override newly revoked scope.
    assert adapter.clicks==0
    assert result['state']=='failed_no_submit' and result['submit_gate_passed'] is False
