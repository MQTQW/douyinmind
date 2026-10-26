"""Bounded notification-pane loading, fake visible/virtualized cards only."""
from copy import deepcopy
import json
import shutil
import subprocess

import pytest

from app.mention_receiver.browser import ReceiverDetailError, ReceiverPageError, ReceiverInterventionRequired
from app.mention_receiver.browser import NOTICE_HISTORY_CONTAINER_JS, NOTICE_JS
from tests.test_mention_original_target_lookup import receiver, notice, detail, TARGET, VIDEO


OTHER = lambda key='recent': notice(key, exact_text='@6 another request')


class Pane:
    def __init__(self,page): self.page=page;self.disposed=False
    def as_element(self): return None if self.page.unknown_pane else self
    def dispose(self): self.disposed=True
    def evaluate(self,script,args):
        if not isinstance(args,dict):
            rows=self.page.current()
            item=next(row for row in rows if row['pre_index']==args)
            return self.page.absolute.get(item['semantic_fingerprint'],self.page.screen*100+10)
        self.page.actions.append(args['action'])
        if args['action']=='seek':self.page.screen=int(args['top']//100)
        elif args['action']=='next' and not self.page.no_progress:
            self.page.screen=min(self.page.screen+1,len(self.page.pages())-1)
        return {'top':self.page.screen*100,'at_end':self.page.screen>=len(self.page.pages())-1}


def setup(monkeypatch,pages,details=None):
    value,page,calls=receiver(monkeypatch,[],{})
    page.screen=0;page.unknown_pane=False;page.no_progress=False;page.actions=[];page.handles=[];page.absolute={}
    page.pages=lambda: pages(len(page.navigations)) if callable(pages) else pages
    def current():
        result=deepcopy(page.pages()[page.screen])
        for index,row in enumerate(result):row['pre_index']=len(page.navigations)*100+page.screen*10+index
        return result
    page.current=current
    def goto(url,**_):page.navigations.append(url);page.screen=0
    page.goto=goto
    def handle(_script):
        item=Pane(page);page.handles.append(item);return item
    page.evaluate_handle=handle
    monkeypatch.setattr(value,'notifications',current)
    def inspect(candidate,*,keep_open=False):
        assert keep_open is True
        assert candidate in page.current(), 'a card must be currently visible with its freshly read index'
        calls.append(deepcopy(candidate))
        if callable(details):return details(candidate,len(calls))
        return deepcopy((details or {}).get(candidate['semantic_fingerprint'],detail()))
    monkeypatch.setattr(value,'detail',inspect)
    return value,page,calls


def test_zero_initial_match_loads_old_root_then_reseeks_each_inspection(monkeypatch):
    value,page,calls=setup(monkeypatch,[[OTHER()],[notice('old')],[OTHER('older')],[OTHER('oldest')]])
    assert value.open_original_from_notifications(TARGET)['video_id']==VIDEO
    assert len(calls)==2 and len(page.navigations)==3
    assert all(row['semantic_fingerprint']=='old' for row in calls)
    assert all(handle.disposed for handle in page.handles)
    assert value.last_original_lookup['history_scrolls']==9


def test_initial_nonzero_path_never_acquires_or_scrolls_history(monkeypatch):
    value,page,calls=setup(monkeypatch,[[notice('here')],[notice('hidden-duplicate')]])
    def forbidden(_):raise AssertionError('history must not run for initial nonzero candidate')
    page.evaluate_handle=forbidden
    value.open_original_from_notifications(TARGET)
    assert len(calls)==2 and page.actions==[] and 'history_scrolls' not in value.last_original_lookup


def test_unknown_or_nonunique_pane_never_scrolls_the_whole_document(monkeypatch):
    value,page,calls=setup(monkeypatch,[[OTHER()]])
    page.unknown_pane=True
    with pytest.raises(ReceiverDetailError):value.open_original_from_notifications(TARGET)
    assert calls==[] and page.actions==[] and page.handles[0].disposed


@pytest.mark.parametrize('no_progress',[False,True])
def test_end_or_no_progress_is_bounded_and_cannot_invent_a_candidate(monkeypatch,no_progress):
    value,page,calls=setup(monkeypatch,[[OTHER()],[OTHER('older')]])
    page.no_progress=no_progress
    with pytest.raises(ReceiverDetailError) as error:value.open_original_from_notifications(TARGET)
    assert error.value.diagnostics['reason_code']=='original_notification_not_found'
    assert page.actions.count('next')==1 and calls==[]


def test_no_more_than_three_scroll_steps_per_self_snapshot(monkeypatch):
    value,page,calls=setup(monkeypatch,[[OTHER(str(i))] for i in range(8)])
    with pytest.raises(ReceiverDetailError):value.open_original_from_notifications(TARGET)
    assert page.actions.count('next')==3 and calls==[]


def test_candidate_set_drift_on_fresh_full_rescan_holds_before_click(monkeypatch):
    def pages(n):return [[OTHER()],[notice('old')]] if n==1 else [[OTHER()],[notice('old'),notice('new')]]
    value,page,calls=setup(monkeypatch,pages)
    with pytest.raises(ReceiverDetailError) as error:value.open_original_from_notifications(TARGET)
    assert error.value.diagnostics['reason_code']=='original_notification_changed' and calls==[]


def test_competing_same_video_cards_on_different_screens_both_inspected_then_held(monkeypatch):
    value,page,calls=setup(monkeypatch,[[OTHER()],[notice('one')],[notice('two')]])
    with pytest.raises(ReceiverDetailError):value.open_original_from_notifications(TARGET)
    assert len(calls)==2 and value.last_original_lookup['match_count']==2


def test_duplicate_same_fingerprint_at_distinct_content_positions_is_not_overlap(monkeypatch):
    value,page,calls=setup(monkeypatch,[[OTHER()],[notice('dup')],[notice('dup')]])
    with pytest.raises(ReceiverDetailError) as error:value.open_original_from_notifications(TARGET)
    assert error.value.diagnostics['reason_code']=='original_notification_not_unique' and calls==[]


def test_matching_card_on_fourth_scroll_is_outside_this_bound(monkeypatch):
    value,page,calls=setup(monkeypatch,[[OTHER(str(i))] for i in range(4)]+[[notice('old')]])
    with pytest.raises(ReceiverDetailError):value.open_original_from_notifications(TARGET)
    assert calls==[] and page.actions.count('next')==3


def test_stop_during_lazy_load_prevents_further_scrolling_or_escape(monkeypatch):
    value,page,calls=setup(monkeypatch,[[OTHER()],[notice('old')]])
    stopped=[False];value.choice_should_stop=lambda:stopped[0]
    page.wait_for_timeout=lambda _:stopped.__setitem__(0,True)
    with pytest.raises(ReceiverPageError):value.open_original_from_notifications(TARGET)
    assert page.actions==['seek'] and calls==[] and page.keys==[]


def test_challenge_during_load_propagates_without_any_cleanup_input(monkeypatch):
    value,page,calls=setup(monkeypatch,[[OTHER()],[notice('old')]])
    challenge=[False]
    page.wait_for_timeout=lambda _:challenge.__setitem__(0,True)
    def check():
        if challenge[0]:raise ReceiverInterventionRequired('synthetic')
    monkeypatch.setattr(value,'check_intervention',check)
    with pytest.raises(ReceiverInterventionRequired):value.open_original_from_notifications(TARGET)
    assert page.actions==['seek'] and calls==[] and page.keys==[]


def test_identity_mismatch_prevents_history_lookup(monkeypatch):
    value,page,calls=setup(monkeypatch,[[OTHER()],[notice('old')]])
    def wrong():raise ReceiverInterventionRequired('wrong synthetic actor')
    monkeypatch.setattr(value,'identity',wrong)
    with pytest.raises(ReceiverInterventionRequired):value.open_original_from_notifications(TARGET)
    assert page.handles==[] and calls==[]


@pytest.mark.parametrize('actual_popup',[False,True])
def test_actual_container_predicate_rejects_shared_app_root_but_allows_local_popup(actual_popup):
    # Execute the production pane predicate, stubbing only the pre-existing
    # notification parser. No browser, DOM engine, network or account is used.
    node=shutil.which('node')
    assert node, 'installed Node runtime required for the isolated DOM predicate'
    predicate=NOTICE_HISTORY_CONTAINER_JS.replace(
        'const state = ('+NOTICE_JS+')();','const state = globalThis.__noticeState;')
    assert predicate!=NOTICE_HISTORY_CONTAINER_JS
    harness=r'''
const input=JSON.parse(require('fs').readFileSync(0,'utf8'));
class El{
 constructor(name,text='',style={}){this.name=name;this.innerText=text;this.style=style;this.children=[];this.parentElement=null;this.clientHeight=200;this.scrollHeight=200;}
 add(...children){for(const child of children){child.parentElement=this;this.children.push(child);}return this;}
 contains(other){return this===other||this.children.some(child=>child.contains(other));}
 getBoundingClientRect(){return {width:400,height:300,top:0,bottom:300};}
}
const html=new El('html'),body=new El('body'),app=new El('app','main application',{position:'relative'});
const heading=new El('h1','profile B'),title=new El('span','互动消息');
const pane=new El('div','notification-like prose',{overflowY:'auto'});pane.scrollHeight=1000;
const pre=new El('pre','@6 another request');pane.add(pre);html.add(body);body.add(app);app.add(heading);
if(input.popup){const popup=new El('div','notification popup',{position:'absolute'});app.add(popup);popup.add(title,pane);}
else{app.add(title,pane);}
globalThis.innerHeight=1000;globalThis.innerWidth=1400;
globalThis.getComputedStyle=el=>({position:'static',overflowY:'visible',...el.style});
globalThis.__noticeState={items:[{pre_index:0}]};
globalThis.document={body,documentElement:html,querySelectorAll:selector=>{
 if(selector==='pre')return [pre];
 if(selector==='h1,main,[role="main"]')return [heading];
 return [app,heading,title,pane];
}};
const result=Function('return ('+input.predicate+')()')();
process.stdout.write(JSON.stringify(result===pane));
'''
    result=subprocess.run([node,'-e',harness],input=json.dumps({'predicate':predicate,'popup':actual_popup}),
        text=True,encoding='utf-8',stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=5)
    assert result.returncode==0,result.stderr
    assert json.loads(result.stdout) is actual_popup
