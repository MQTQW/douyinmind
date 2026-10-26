"""Historical locator only; fake UI and temporary ledgers, no real effects."""
from copy import deepcopy
from types import SimpleNamespace

import pytest

from app.assistant.native_lane import NativeLane
from app.assistant.native_source_store import NativeSourceStore
from app.assistant.native_events import project_page
from tests.test_assistant_native_lane import native_choice_case, choice_native_system


A='https://www.douyin.com/user/legacy-context-A'
B='https://www.douyin.com/user/legacy-context-B'
V='7999999000000000011'
H='8900000000000000099'


def candidate():
    return {'sender_uid':'8001','sender_sec_uid':'legacy-context-A','sender_profile_url':A,
        'recipient_uid':'9001','receiver_profile_url':B,'video_id':V,'parent_id_hint':H}


def session(key='a',**changes):
    value={'choice_id':key*64,'root_task_id':'old-root-'+key,'state':'waiting_library',
        'root_binding':{'sender_profile_url':A,'receiver_profile_url':B,'active':True},
        'root_envelope':{'sender_profile_url':A,'receiver_profile_url':B,'video_id':V,
            'reference_id':'old-ui-'+key,'native_comment_id':None,'comment_text':'@6 请保存这个视频'}}
    value.update(changes);return value


def historical():
    return {'source_id':'old-historical','state':'historical','historical':True,'hold_code':None,
        'task_id':None,'reference_id':None,'plan':None,'candidate':{**candidate(),
            'source_kind':'native_notice_candidate','notice_type':45,'at_sub_type':2,
            'native_comment_id':H,'content':'@6  请保存这个视频'}}


class LocatorStore:
    def __init__(self):
        self.row=historical();self.linked=None;self.orphans=[];self.conflict=False;self.lookups=[]
    def lookup_source_for_task(self,_):return deepcopy(self.linked)
    def find_sources_by_ui_reference(self,_):return deepcopy(self.orphans)
    def admission_current(self,_):return bool(self.linked and self.linked.get('current'))
    def find_source_by_native_comment(self,uid,video,cid):
        self.lookups.append((uid,video,cid));return deepcopy(self.row)
    def semantic_conflicts(self,_):return self.conflict


def lane_for(sessions=None):
    sessions=sessions or [session()]
    choices=SimpleNamespace(store=SimpleNamespace(list_waiting=lambda _:deepcopy(sessions),list_asks_to_observe=lambda _:[]))
    base=SimpleNamespace(adapter=SimpleNamespace(),registry=None,queue=None,conversation=None,
        should_stop=lambda:False,choice_controller=choices)
    store=LocatorStore();return NativeLane(base,SimpleNamespace(),store),store


def test_history_can_only_locate_unique_existing_context_without_mutating_any_row():
    lane,store=lane_for();before=deepcopy(store.row)
    assert lane._reply_session(candidate())['choice_id']=='a'*64
    assert store.row==before and store.lookups==[('9001',V,H)]


@pytest.mark.parametrize('key,value',[
    ('state','fresh'),('state','held_conflict'),('historical',False),
    ('hold_code','native_identity_conflict'),('task_id','other-old-task'),
    ('reference_id','orphan-ref'),('plan',{}),
])
def test_nonhistorical_conflicted_or_linked_rows_cannot_be_context_locators(key,value):
    lane,store=lane_for();store.row[key]=value
    assert lane._reply_session(candidate()) is None


@pytest.mark.parametrize('key,value',[
    ('native_comment_id','9999'),('source_kind','native_reply_notice_candidate'),
    ('notice_type',31),('at_sub_type',1),('sender_uid','8002'),('sender_sec_uid','another-A'),
    ('sender_profile_url',B),('recipient_uid','9002'),('receiver_profile_url',A),
    ('video_id','7999999000000000022'),('content','@6 其他请求'),
])
def test_wrong_identity_video_kind_or_body_never_binds_an_old_context(key,value):
    lane,store=lane_for();store.row['candidate'][key]=value
    assert lane._reply_session(candidate()) is None


def test_semantic_conflict_and_multiple_pending_choices_are_not_disambiguated_by_history():
    lane,store=lane_for();store.conflict=True
    assert lane._reply_session(candidate()) is None
    lane,store=lane_for([session('a'),session('b')])
    assert lane._reply_session(candidate()) is None


def test_existing_native_but_not_current_and_native_orphan_never_fall_back_to_history():
    lane,store=lane_for();store.linked={'source_id':'native-old','state':'admitted','historical':False,'current':False}
    assert lane._reply_session(candidate()) is None and store.lookups==[]
    lane,store=lane_for();store.orphans=[{'state':'planned'}]
    assert lane._reply_session(candidate()) is None and store.lookups==[]


def test_wrong_root_binding_and_missing_history_fail_closed():
    lane,store=lane_for();store.row=None
    assert lane._reply_session(candidate()) is None
    s=session();s['root_binding']['sender_profile_url']=B
    lane,store=lane_for([s])
    assert lane._reply_session(candidate()) is None


def test_native_cid_path_remains_strict_and_does_not_need_history():
    s=session();s['root_envelope']['native_comment_id']=H
    lane,store=lane_for([s]);store.row=None
    assert lane._reply_session(candidate())['choice_id']==s['choice_id'] and not store.lookups
    s['root_envelope']['native_comment_id']='9999';lane,store=lane_for([s])
    assert lane._reply_session(candidate()) is None


def test_real_temporary_history_stays_historical_only_fresh_reply_creates_child(native_choice_case,tmp_path):
    case=native_choice_case
    lane,fake,root_task,old_session,adapter=choice_native_system(case)
    root_reply=deepcopy(case.agent.get_reply(case.agent.list_tasks()[0]['task_id']))
    now=case.clock
    c=deepcopy(fake.candidate)
    store=NativeSourceStore(tmp_path/'historical-context-source.sqlite3',c['recipient_uid'],
        c['receiver_profile_url'],create=True,clock=lambda:now[0])
    def projection(rows,channel):
        return project_page({'status_code':0,'notice_list_v2':rows},channel,
            receiver_uid=c['recipient_uid'],receiver_sec_uid=c['receiver_profile_url'].rsplit('/',1)[-1],
            profile_allowed=lambda profile:profile==c['sender_profile_url'])
    old={'nid_str':'8900000000000000101','type':45,'user_id':c['recipient_uid'],
        'aweme_id':c['video_id'],'create_time':str(int(now[0])-20),
        'at':{'sub_type':2,'aweme':{'aweme_id':c['video_id']},
            'user_info':{'uid':c['sender_uid'],'sec_uid':c['sender_sec_uid']},
            'content':root_task['envelope']['comment_text'],'comment_status':1,'comment_unvisible':0,
            'schema_url':'aweme://aweme/detail/'+c['video_id']+'?cid='+c['parent_id_hint']}}
    store.record_page(store.prepare_channel(9,'601'),projection([old],9),next_cursor=('0','0'),has_more=False)
    store.record_page(store.prepare_channel(7,'2'),projection([],7),next_cursor=('0','0'),has_more=False)
    store.arm_if_ready(True);now[0]+=2;store.set_verified()
    old_row=store.find_source_by_native_comment(c['recipient_uid'],c['video_id'],c['parent_id_hint'])
    fresh={'nid_str':'8900000000000000102','type':31,'user_id':c['recipient_uid'],'create_time':str(int(now[0])),
        'comment':{'comment_type':2,'comment_unvisible':0,'parent_id':c['parent_id_hint'],
            'aweme':{'aweme_id':c['video_id']},'comment':{'cid':c['native_comment_id'],
                'aweme_id':c['video_id'],'status':1,'text':c['content'],
                'user':{'uid':c['sender_uid'],'sec_uid':c['sender_sec_uid']}}}}
    store.record_page(store.prepare_channel(7,'2'),projection([fresh],7),next_cursor=('0','0'),has_more=False,allow_live=True)
    lane.source_store=store
    rows=lane.collect()
    assert len(rows)==1 and rows[0]['envelope']['evidence']['real_mention'] is False
    assert rows[0]['envelope']['native_comment_id'] is None
    child=case.queue.enqueue(**rows[0]);lane.admitted([child])
    assert case.queue.list_tasks()['total']==2
    assert case.store.get(old_session['choice_id'])['child_task_id']==child['id']
    current=store.find_source_by_native_comment(c['recipient_uid'],c['video_id'],c['parent_id_hint'])
    assert current==old_row and current['historical'] is True and current['state']=='historical'
    assert current['task_id'] is None and current['reference_id'] is None and current['plan'] is None
    assert store.lookup_source_for_task(root_task['id']) is None
    assert case.agent.get_reply(case.agent.list_tasks()[0]['task_id'])==root_reply
    assert len(adapter.reads)==1 and lane.collect()==[]
