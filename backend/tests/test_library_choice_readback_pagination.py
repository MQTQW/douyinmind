"""Readback-only bounded paging, synthetic DOM and no account/network/state."""
from copy import deepcopy

import pytest

from tests.test_library_choice_weak_receipt_dom import (
    browser, surface, complete, target, adapter, args, TEXT,
)
from app.mention_receiver import choice_browser as module
from app.mention_receiver.browser import ReceiverPageError, ReceiverInterventionRequired


def paged(surface, *, pages=1, keep=False, duplicate=False, delay=0, label='展开更多'):
    """A public layout supplied by Root, reproduced with synthetic identities.

    A-side shape does not establish the real B-side structure or live cause.
    More is a native BUTTON directly in the original Root's replyContainer.
    """
    surface.page.evaluate('''({pages,keep,duplicate,delay,label})=>{
      const receipt=document.getElementById('receipt');
      const copy=receipt.cloneNode(true);receipt.remove();
      const root=document.getElementById('root');
      const group=[...root.querySelectorAll('.replyContainer')].find(e=>e.closest('[data-e2e="comment-item"]')===root);
      const button=document.createElement('button');button.className='wJHGjWeA';button.id='readback-more';button.textContent=label;
      group.append(button);window.readbackClickCount=0;
      button.onclick=()=>{
        window.readbackClickCount++;
        setTimeout(()=>{
          if(window.readbackClickCount<pages){
            const row=copy.cloneNode(true);row.id='page-progress-'+window.readbackClickCount;
            row.querySelector('.FduGc_lz').textContent='另一个无关公开回复';button.before(row);
          }else{
            if(!copy.isConnected)button.before(copy);
            else{const row=copy.cloneNode(true);row.id='page-progress-'+window.readbackClickCount;
              row.querySelector('.FduGc_lz').textContent='另一个无关公开回复';button.before(row);}
            if(duplicate){const second=copy.cloneNode(true);second.id='duplicate-receipt';button.before(second);}
          }
          if(!keep&&window.readbackClickCount>=pages)button.remove();
        },delay);
      };
    }''', {'pages':pages,'keep':keep,'duplicate':duplicate,'delay':delay,'label':label})


def observe(surface, monkeypatch, *, pages=1, keep=False, duplicate=False, delay=0, label='展开更多'):
    state=complete(surface);frozen=target(surface,state)
    paged(surface,pages=pages,keep=keep,duplicate=duplicate,delay=delay,label=label)
    receiver=adapter(surface,state,monkeypatch)
    return module.observe_existing_choice_reply(receiver,frozen,TEXT),receiver


def test_readback_loads_plain_native_more_after_selection_already_visible(surface,monkeypatch):
    result,receiver=observe(surface,monkeypatch)
    assert result['outcome']=='b_view_visible'
    assert result['readback_quality']=='weak_unique_root'
    assert receiver.last_choice_readback_diagnostics['counts']['readback_expansion_count']==1
    assert surface.page.evaluate('window.readbackClickCount')==1
    assert surface.page.evaluate('window.sendCount')==0


def test_visible_same_body_does_not_skip_known_unread_page(surface,monkeypatch):
    state=complete(surface);frozen=target(surface,state)
    public_receipt=surface.page.locator('#receipt').evaluate('e=>e.outerHTML')
    paged(surface)
    surface.page.locator('#readback-more').evaluate('(e,html)=>e.insertAdjacentHTML("beforebegin",html)',public_receipt)
    receiver=adapter(surface,state,monkeypatch)
    result=module.observe_existing_choice_reply(receiver,frozen,TEXT)
    assert result['outcome']=='delivery_unknown'
    assert result['verification'] is None


def test_unread_more_at_budget_end_is_unknown_even_with_exact_receipt(surface,monkeypatch):
    result,receiver=observe(surface,monkeypatch,keep=True)
    diagnostics=receiver.last_choice_readback_diagnostics
    assert result['outcome']=='delivery_unknown'
    assert diagnostics['reason_code']=='choice_readback_bound_exhausted'
    assert diagnostics['counts']['readback_expansion_count']==2
    assert diagnostics['counts']['readback_more_count']==1
    assert surface.page.evaluate('window.readbackClickCount')==2


def test_same_label_can_load_second_page_only_after_real_row_progress(surface,monkeypatch):
    result,receiver=observe(surface,monkeypatch,pages=2,delay=40)
    assert result['outcome']=='b_view_visible'
    assert receiver.last_choice_readback_diagnostics['counts']['readback_expansion_count']==2
    assert receiver.last_choice_readback_diagnostics['counts']['readback_progress_count']==2
    assert surface.page.evaluate('window.readbackClickCount')==2


@pytest.mark.parametrize('label',['展开更多回复','展开5条回复'])
def test_existing_precise_reply_count_labels_work_only_in_readback_scope(surface,monkeypatch,label):
    assert observe(surface,monkeypatch,label=label)[0]['outcome']=='b_view_visible'


def test_default_selection_expansion_still_stops_at_selection_without_more_click(surface,monkeypatch):
    state=complete(surface);paged(surface)
    receiver=adapter(surface,state,monkeypatch)
    found=module._expanded_choice_thread(receiver,args(state))
    assert len(found['details'])==1
    assert surface.page.evaluate('window.readbackClickCount')==0


@pytest.mark.parametrize('position',['body','selected_child','div_control','duplicate'])
def test_unsupported_or_multiple_unread_controls_are_unknown_without_click(surface,monkeypatch,position):
    state=complete(surface);frozen=target(surface,state);paged(surface)
    surface.page.evaluate('''position=>{
      const b=document.getElementById('readback-more');
      if(position==='body')document.querySelector('#root .FduGc_lz').append(b);
      if(position==='selected_child')document.getElementById('selection').append(b);
      if(position==='div_control'){
        const d=document.createElement('div');d.setAttribute('data-e2e','video-comment-more');d.textContent=b.textContent;b.replaceWith(d);
      }
      if(position==='duplicate'){const c=b.cloneNode(true);c.id='duplicate-more';b.after(c);}
    }''',position)
    receiver=adapter(surface,state,monkeypatch)
    result=module.observe_existing_choice_reply(receiver,frozen,TEXT)
    assert result['outcome']=='delivery_unknown'
    assert receiver.last_choice_readback_diagnostics['reason_code'] in {'choice_readback_expander_unsupported','choice_readback_expander_ambiguous'}
    assert surface.page.evaluate('window.readbackClickCount')==0


def test_sibling_root_more_is_not_clicked_or_borrowed(surface,monkeypatch):
    state=complete(surface);frozen=target(surface,state)
    surface.page.evaluate('''()=>{
      const sibling=document.createElement('article');sibling.setAttribute('data-e2e','comment-item');
      sibling.innerHTML='<div class="replyContainer"><button>展开更多</button></div>';
      document.getElementById('root').after(sibling);window.foreignClicks=0;
      sibling.querySelector('button').onclick=()=>window.foreignClicks++;
    }''')
    receiver=adapter(surface,state,monkeypatch)
    result=module.observe_existing_choice_reply(receiver,frozen,TEXT)
    assert result['outcome']=='b_view_visible'
    assert receiver.last_choice_readback_diagnostics['counts']['readback_expansion_count']==0
    assert surface.page.evaluate('window.foreignClicks')==0


def test_visible_receipt_with_unchanged_more_never_reclicks_or_confirms(surface,monkeypatch):
    state=complete(surface);frozen=target(surface,state)
    receipt=surface.page.locator('#receipt').evaluate('e=>e.outerHTML');paged(surface)
    surface.page.locator('#readback-more').evaluate('''(e,html)=>{
      e.insertAdjacentHTML('beforebegin',html);e.onclick=()=>window.readbackClickCount++;
    }''',receipt)
    receiver=adapter(surface,state,monkeypatch)
    result=module.observe_existing_choice_reply(receiver,frozen,TEXT)
    assert result['outcome']=='delivery_unknown'
    assert receiver.last_choice_readback_diagnostics['reason_code']=='choice_readback_no_progress'
    assert receiver.last_choice_readback_diagnostics['counts']['readback_expansion_count']==1
    assert surface.page.evaluate('window.readbackClickCount')==1


@pytest.mark.parametrize('change',['root','actor','selection','duplicate_selection'])
def test_scope_changes_during_page_load_never_confirm(surface,monkeypatch,change):
    state=complete(surface);frozen=target(surface,state);paged(surface)
    surface.page.locator('#readback-more').evaluate('''(b,change)=>{
      const original=b.onclick;b.onclick=()=>{original();
        if(change==='root')document.querySelector('#root .FduGc_lz').textContent='different original';
        if(change==='actor')document.body.insertAdjacentHTML('beforeend','<div id="comment-input-container"><a href="https://www.douyin.com/user/wrong-B"><img width="20" height="20"></a></div>');
        if(change==='selection')document.querySelector('#selection .FduGc_lz').textContent='different selection';
        if(change==='duplicate_selection'){const copy=document.getElementById('selection').cloneNode(true);copy.id='another-selection';b.before(copy);}
      };
    }''',change)
    result=module.observe_existing_choice_reply(adapter(surface,state,monkeypatch),frozen,TEXT)
    assert result['outcome']=='delivery_unknown' and result['verification'] is None
    assert surface.page.evaluate('window.readbackClickCount')==1
    assert surface.page.evaluate('window.sendCount')==0


@pytest.mark.parametrize('mode',['stop','intervention'])
def test_stop_or_intervention_after_expansion_propagates_with_no_escape_or_input(surface,monkeypatch,mode):
    state=complete(surface);frozen=target(surface,state);paged(surface,delay=1000)
    receiver=adapter(surface,state,monkeypatch);keys=[]
    surface.page.locator('#editor').evaluate("e=>e.textContent='preserve operator draft'")
    monkeypatch.setattr(surface.page.keyboard,'press',lambda key:keys.append(key))
    if mode=='stop':
        receiver.choice_should_stop=lambda:surface.page.evaluate('window.readbackClickCount')>0
        exception=ReceiverPageError
    else:
        def challenge():
            if surface.page.evaluate('window.readbackClickCount'):
                raise ReceiverInterventionRequired('synthetic challenge')
        receiver.check_intervention=challenge;exception=ReceiverInterventionRequired
    with pytest.raises(exception):
        module.observe_existing_choice_reply(receiver,frozen,TEXT)
    assert keys==[] and surface.page.evaluate('window.sendCount')==0
    assert surface.page.locator('#editor').inner_text()=='preserve operator draft'


def test_returned_readback_diagnostics_are_fixed_and_contain_no_body_or_target(surface,monkeypatch):
    result,receiver=observe(surface,monkeypatch,pages=2)
    assert receiver.last_choice_readback_diagnostics['reason_code']=='choice_readback_complete'
    assert all(type(v) is int and 0<=v<=1000000 for v in result['counts'].values())
    assert not any(k in result for k in ('nonce','claim_token','authorization','url','body','target'))
    assert set(result)=={'outcome','exact_body_in_original_thread','verification','reply_sha256',
                         'observed_at_utc','counts','readback_quality'}
    assert not any(k.startswith('readback_') for k in result['counts'])


def test_initial_collapsed_root_and_later_more_share_two_click_total_budget(surface,monkeypatch):
    state=complete(surface);frozen=target(surface,state)
    surface.page.evaluate('''()=>{
      const root=document.getElementById('root'),ask=document.getElementById('ask'),
        selection=document.getElementById('selection'),receipt=document.getElementById('receipt');
      const group=ask.parentElement;ask.remove();selection.remove();receipt.remove();
      const button=document.createElement('button');button.textContent='展开5条回复';group.append(button);window.readbackClickCount=0;
      button.onclick=()=>{window.readbackClickCount++;
        if(window.readbackClickCount===1){button.before(ask,selection);button.textContent='展开更多';}
        else{button.before(receipt);button.remove();}
      };
    }''')
    receiver=adapter(surface,state,monkeypatch)
    result=module.observe_existing_choice_reply(receiver,frozen,TEXT)
    assert result['outcome']=='b_view_visible', (result,receiver.last_choice_readback_diagnostics,
        surface.page.evaluate('window.readbackClickCount'))
    assert surface.page.evaluate('window.readbackClickCount')==2
    assert surface.page.evaluate('window.sendCount')==0


@pytest.mark.parametrize('change',['actor','duplicate','plain_more','loaded_row','foreign_row'])
def test_initial_empty_reply_container_does_not_bypass_identity_unique_or_first_page_scope(surface,monkeypatch,change):
    state=complete(surface);frozen=target(surface,state)
    surface.page.evaluate('''change=>{
      const root=document.getElementById('root'),ask=document.getElementById('ask'),
        selection=document.getElementById('selection'),receipt=document.getElementById('receipt');
      const group=ask.parentElement;ask.remove();selection.remove();receipt.remove();
      const button=document.createElement('button');button.textContent='展开5条回复';group.append(button);
      window.readbackClickCount=0;button.onclick=()=>window.readbackClickCount++;
      if(change==='actor')document.body.insertAdjacentHTML('beforeend',
        '<div id="comment-input-container"><a href="https://www.douyin.com/user/wrong-B"><img width="20" height="20"></a></div>');
      if(change==='duplicate')button.after(button.cloneNode(true));
      if(change==='plain_more')button.textContent='展开更多';
      if(change==='loaded_row')group.prepend(ask);
      if(change==='foreign_row'){const row=document.createElement('article');row.setAttribute('data-e2e','comment-item');group.append(row);row.append(button);}
    }''',change)
    result=module.observe_existing_choice_reply(adapter(surface,state,monkeypatch),frozen,TEXT)
    assert result['outcome']=='delivery_unknown' and result['verification'] is None
    assert surface.page.evaluate('window.readbackClickCount')==0
    assert surface.page.evaluate('window.sendCount')==0


@pytest.mark.parametrize('noavatar',[False,True])
def test_paginated_observation_records_in_real_temporary_store_without_changing_send_barrier(surface,monkeypatch,tmp_path,noavatar):
    """Actual Store validation/append, fictional live_ui fixture, fake old gate."""
    from datetime import datetime, timezone
    import hashlib
    import sqlite3
    from app.mention_agent.choice_evidence import make_choice_envelope
    from app.mention_agent.contracts import Binding
    from app.mention_agent.store import AgentStore
    from tests.test_library_choice_browser_dom import A, B
    from tests.test_library_choice_weak_receipt_dom import SELECT

    state=complete(surface)
    binding=Binding('synthetic-selected5','choice-instance',A,B,5,True)
    detail=surface.page.evaluate(module.CHOICE_THREAD_JS,args(state))['details'][0]
    env=make_choice_envelope({'source_profile_url':A,'exact_text':SELECT,
                             'semantic_fingerprint':'synthetic-paginated-choice'},detail,binding,'live_ui')
    store=AgentStore(tmp_path/'agent.sqlite3');store.put_binding(binding.to_dict())
    task=store.enqueue(binding.binding_id,env);claim=store.claim(task['task_id'])
    reply=store.complete(task['task_id'],claim['claim_token'],{'kind':'clarification','text':TEXT,'source_ids':[],'sources':[]})
    receiver=adapter(surface,state,monkeypatch)
    receiver._reply_identity_checked_at=__import__('time').monotonic()
    proof=module._frozen_choice_verification(reply['target'],module.choice_reply_state(receiver,reply['target'],TEXT))
    reserved=store.reserve_send(task['task_id'],proof)
    assert reserved is not None
    store.finish_send(task['task_id'],reserved['send_token'],'delivery_unknown',{
        'adapter_outcome':'delivery_unknown','submit_gate_passed':True,'exact_body_in_original_thread':False,
        'reply_sha256':hashlib.sha256(TEXT.encode()).hexdigest(),'observed_at_utc':datetime.now(timezone.utc).isoformat()})
    def frozen_rows():
        with sqlite3.connect(store.path.as_uri()+'?mode=ro',uri=True) as db:
            return {name:db.execute('SELECT * FROM '+name+' ORDER BY 1').fetchall() for name in ('agent_tasks','agent_replies')}
    before=frozen_rows()
    if noavatar:
        from tests.test_library_choice_noavatar_readback import unlink_avatar
        unlink_avatar(surface)
    paged(surface)
    observation=module.observe_existing_choice_reply(receiver,reply['target'],TEXT)
    assert observation['outcome']=='b_view_visible'
    certificate=store.record_reply_readback(task['task_id'],observation)
    assert certificate['scope']=='b_view_only' and certificate['readback_quality']=='weak_unique_root'
    assert frozen_rows()==before
    assert store.reserve_send(task['task_id'],proof) is None
    assert store.get_reply(task['task_id'])['state']=='delivery_unknown'
    assert surface.page.evaluate('window.sendCount')==0
