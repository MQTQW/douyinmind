"""B's own linkless avatar is only accepted by readback, never author intake."""
import pytest

from tests.test_library_choice_weak_receipt_dom import browser,surface,complete,args,target,adapter,TEXT
from app.mention_receiver import choice_browser as module


def unlink_avatar(surface):
    surface.page.locator('#receipt .comment-item-avatar').evaluate('''region=>{
      const a=region.querySelector('a');a.replaceWith(...a.childNodes);
      const img=region.querySelector('img');img.width=24;img.height=24;
    }''')


def inspect(surface,state,readback=True):
    values=args(state)
    if readback:values['readback_pagination']=True
    return surface.page.evaluate(module.CHOICE_REPLY_STATE_JS,values)


def test_linkless_avatar_canonical_B_to_A_is_readback_only_weak(surface,monkeypatch):
    state=complete(surface);frozen=target(surface,state);unlink_avatar(surface)
    assert inspect(surface,state,False)['matching_reply_count']==0
    value=inspect(surface,state)
    assert value['matching_reply_count']==value['existing_matching_reply_count']==1
    assert value['strict_matching_reply_count']==0 and value['weak_matching_reply_count']==1
    result=module.observe_existing_choice_reply(adapter(surface,state,monkeypatch),frozen,TEXT)
    assert result['outcome']=='b_view_visible' and result['readback_quality']=='weak_unique_root'
    assert surface.page.evaluate('window.sendCount')==0


@pytest.mark.parametrize('change',[
    'wrong_B','wrong_A','missing_arrow','duplicate_header','duplicate_avatar','empty_href_anchor',
    'canonical_avatar_link','different_avatar_link','anchor_without_href','other_href_element',
    'duplicate_body','extra_prose','other_root','competing_A','hidden','actor',
    'avatar_region_href','avatar_region_anchor','avatar_region_xlink',
])
def test_noavatar_readback_rejects_conflicting_or_ambiguous_rows(surface,monkeypatch,change):
    state=complete(surface);frozen=target(surface,state);unlink_avatar(surface)
    surface.page.evaluate('''change=>{
      const row=document.getElementById('receipt'),avatar=row.querySelector('.comment-item-avatar'),
        header=row.querySelector('.comment-item-info-wrap'),titles=header.querySelectorAll('a');
      if(change==='wrong_B')titles[0].href='/user/other-B';
      if(change==='wrong_A')titles[1].href='/user/other-A';
      if(change==='missing_arrow')row.querySelector('.ym1IYI4C').remove();
      if(change==='duplicate_header')header.after(header.cloneNode(true));
      if(change==='duplicate_avatar')avatar.after(avatar.cloneNode(true));
      if(change==='empty_href_anchor')avatar.insertAdjacentHTML('beforeend','<a href=""></a>');
      if(change==='canonical_avatar_link')avatar.insertAdjacentHTML('beforeend','<a href="/user/choice-browser-B"></a>');
      if(change==='different_avatar_link')avatar.insertAdjacentHTML('beforeend','<a href="/user/other-person"></a>');
      if(change==='anchor_without_href')avatar.insertAdjacentHTML('beforeend','<a></a>');
      if(change==='other_href_element')avatar.insertAdjacentHTML('beforeend','<span href="/user/choice-browser-B"></span>');
      if(change==='avatar_region_href')avatar.setAttribute('href','/user/other-person');
      if(change==='avatar_region_xlink')avatar.setAttribute('xlink:href','/user/other-person');
      if(change==='avatar_region_anchor'){const a=document.createElement('a');a.className=avatar.className;a.href='/user/other-person';a.append(...avatar.childNodes);avatar.replaceWith(a);}
      if(change==='duplicate_body'){const copy=row.cloneNode(true);copy.id='same-body-copy';row.after(copy);}
      if(change==='extra_prose')row.querySelector('.comment-item-stats-container').parentElement.after(Object.assign(document.createElement('div'),{textContent:'another prose block'}));
      if(change==='other_root')document.getElementById('root').after(row);
      if(change==='competing_A'){const copy=document.getElementById('selection').cloneNode(true);copy.querySelector('.FduGc_lz').textContent='我还在解释项目';row.before(copy);}
      if(change==='hidden')row.style.opacity='0';
      if(change==='actor')document.body.insertAdjacentHTML('beforeend','<div id="comment-input-container"><a href="/user/other-B"><img width="24" height="24"></a></div>');
    }''',change)
    value=inspect(surface,state)
    assert value['matching_reply_count']==0
    assert surface.page.evaluate('window.sendCount')==0


def test_linkless_direct_ancestry_is_never_promoted_to_direct_quality(surface):
    state=complete(surface);unlink_avatar(surface)
    surface.page.locator('#selection').evaluate("e=>e.append(document.getElementById('receipt'))")
    value=inspect(surface,state)
    assert value['strict_matching_reply_count']==0
    assert value['reply_readback_quality']!='direct_selected_parent'


def test_offscreen_exact_candidate_gets_one_readonly_scroll_then_must_reverify_Root(surface,monkeypatch):
    state=complete(surface);frozen=target(surface,state);unlink_avatar(surface)
    surface.page.locator('#receipt').evaluate("e=>e.style.marginTop='4500px'")
    value=inspect(surface,state)
    assert value['readback_noavatar_identity_count']==1
    assert value['readback_noavatar_public_count']==0
    assert value['readback_scroll_candidate_count']==1
    receiver=adapter(surface,state,monkeypatch)
    result=module.observe_existing_choice_reply(receiver,frozen,TEXT)
    # Scrolling cannot excuse a now-offscreen original Root/selection. All
    # frozen gates must still pass together; this fixture intentionally cannot.
    assert result['outcome']=='delivery_unknown'
    assert receiver.last_choice_readback_diagnostics['counts']['readback_scroll_count']==1
    assert surface.page.evaluate('window.sendCount')==0


def test_stop_after_candidate_scroll_propagates_without_escape_or_input(surface,monkeypatch):
    from app.mention_receiver.browser import ReceiverPageError
    state=complete(surface);frozen=target(surface,state);unlink_avatar(surface)
    surface.page.locator('#receipt').evaluate("e=>e.style.marginTop='4500px'")
    receiver=adapter(surface,state,monkeypatch);keys=[]
    receiver.choice_should_stop=lambda:surface.page.evaluate('window.scrollY')>0
    monkeypatch.setattr(surface.page.keyboard,'press',lambda key:keys.append(key))
    with pytest.raises(ReceiverPageError):
        module.observe_existing_choice_reply(receiver,frozen,TEXT)
    assert keys==[] and surface.page.evaluate('window.sendCount')==0
