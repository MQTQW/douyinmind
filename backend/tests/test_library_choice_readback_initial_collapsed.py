"""Actual B fixed-count shape, rendered with fictional identities offline."""
import pytest

from tests.test_library_choice_weak_receipt_dom import browser,surface,complete,args,target,adapter,TEXT
from tests.test_library_choice_readback_pagination import paged
from app.mention_receiver import choice_browser as module


def initial_collapsed(surface):
    state=complete(surface);frozen=target(surface,state)
    surface.page.evaluate('''()=>{
      const root=document.getElementById('root'),ask=document.getElementById('ask'),
        selection=document.getElementById('selection'),receipt=document.getElementById('receipt');
      const group=ask.parentElement;ask.remove();selection.remove();receipt.remove();group.remove();
      const wrapper=document.createElement('div');wrapper.id='initial-reply-wrapper';root.append(wrapper);
      const button=document.createElement('button');button.textContent='展开5条回复';button.id='initial-numeric';wrapper.append(button);
      window.readbackClickCount=0;
      button.onclick=()=>{window.readbackClickCount++;button.remove();wrapper.className='replyContainer';
        wrapper.append(ask,selection);
        const more=document.createElement('button');more.textContent='展开更多';more.id='later-more';wrapper.append(more);
        more.onclick=()=>{window.readbackClickCount++;more.before(receipt);more.remove();};
      };
    }''')
    return state,frozen


def test_exact_real_B_initial_numeric_grandchild_can_open_then_strict_more(surface,monkeypatch):
    state,frozen=initial_collapsed(surface)
    before=surface.page.evaluate(module.CHOICE_THREAD_JS,{**args(state),'readback_pagination':True})
    assert before['counts']['descendant_count']==before['counts']['ask_count']==0
    assert before['counts']['readback_parent_reply_container_count']==0
    assert before['counts']['readback_grandparent_root_more_count']==1
    result=module.observe_existing_choice_reply(adapter(surface,state,monkeypatch),frozen,TEXT)
    assert result['outcome']=='b_view_visible'
    assert result['readback_quality']=='weak_unique_root'
    assert surface.page.evaluate('window.readbackClickCount')==2
    assert surface.page.evaluate('window.sendCount')==0


@pytest.mark.parametrize('change',[
    'plain_more','more_replies','all_replies','direct_root','deeper_wrapper','selected_child','div_control','duplicate','loaded_row',
])
def test_initial_exception_does_not_accept_other_shapes_or_partly_loaded_threads(surface,change):
    state,_=initial_collapsed(surface)
    surface.page.evaluate('''change=>{
      const root=document.getElementById('root'),b=document.getElementById('initial-numeric');
      if(change==='plain_more')b.textContent='展开更多';
      if(change==='more_replies')b.textContent='展开更多回复';
      if(change==='all_replies')b.textContent='展开全部回复';
      if(change==='direct_root')root.append(b);
      if(change==='deeper_wrapper'){const d=document.createElement('div');b.replaceWith(d);d.append(b);}
      if(change==='selected_child'||change==='loaded_row'){
        const row=document.createElement('article');row.setAttribute('data-e2e','comment-item');root.append(row);
        if(change==='selected_child')row.append(b);
      }
      if(change==='div_control'){const d=document.createElement('div');d.setAttribute('data-e2e','video-comment-more');d.textContent=b.textContent;b.replaceWith(d);}
      if(change==='duplicate')b.after(b.cloneNode(true));
    }''',change)
    result=surface.page.evaluate(module.CHOICE_THREAD_JS,{**args(state),'readback_pagination':True})
    assert result['ok'] is True
    assert result['expand_index'] is None
    assert result['counts']['readback_unsupported_more_count']>=1 or result['counts']['expand_control_count']==2
    assert surface.page.evaluate('window.readbackClickCount')==0


def test_later_numeric_button_without_reply_container_stays_unsupported(surface,monkeypatch):
    state=complete(surface);frozen=target(surface,state);paged(surface,label='展开5条回复')
    surface.page.locator('#readback-more').evaluate('''b=>{
      const root=document.getElementById('root'),wrapper=document.createElement('div');root.append(wrapper);wrapper.append(b);
    }''')
    receiver=adapter(surface,state,monkeypatch)
    result=module.observe_existing_choice_reply(receiver,frozen,TEXT)
    assert result['outcome']=='delivery_unknown'
    assert receiver.last_choice_readback_diagnostics['reason_code']=='choice_readback_expander_unsupported'
    assert surface.page.evaluate('window.readbackClickCount')==0


def test_initial_default_intake_selector_result_is_unchanged(surface):
    state,_=initial_collapsed(surface)
    result=surface.page.evaluate(module.CHOICE_THREAD_JS,args(state))
    assert result['expand_index'] is not None and result['expand_kind']=='native_button'
    assert 'readback_more_count' not in result['counts']


def test_initial_numeric_control_rechecks_current_B_before_click(surface,monkeypatch):
    state,frozen=initial_collapsed(surface)
    surface.page.evaluate('''()=>document.body.insertAdjacentHTML('beforeend',
      '<div id="comment-input-container"><a href="https://www.douyin.com/user/wrong-B"><img width="20" height="20"></a></div>')''')
    result=module.observe_existing_choice_reply(adapter(surface,state,monkeypatch),frozen,TEXT)
    assert result['outcome']=='delivery_unknown'
    assert surface.page.evaluate('window.readbackClickCount')==0
