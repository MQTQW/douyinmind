"""Bounded owned-headless layout only; fake pages and intercepted synthetic DOM."""
from types import SimpleNamespace

import pytest

from app.mention_receiver import choice_browser as module
from app.mention_receiver.browser import ReceiverPageError,ReceiverInterventionRequired
from tests.test_library_choice_weak_receipt_dom import browser,surface,complete,target,adapter,TEXT
from tests.test_library_choice_noavatar_readback import unlink_avatar,inspect


def fake(*,height=800,configured=1600):
    page=SimpleNamespace(viewport_size={'width':1100,'height':height},changes=[])
    def resize(value):page.changes.append(dict(value));page.viewport_size=dict(value)
    page.set_viewport_size=resize
    return SimpleNamespace(page=page,readback_viewport_height=configured,choice_should_stop=lambda:False,
                           check_intervention=lambda:None)


def test_guard_expands_height_only_and_restores_without_stop_checkpoint_in_finally():
    receiver=fake()
    with module._readback_viewport(receiver):
        assert receiver.page.viewport_size=={'width':1100,'height':1600}
        receiver.choice_should_stop=lambda:True
    assert receiver.page.viewport_size=={'width':1100,'height':800}
    assert receiver.last_choice_readback_viewport_diagnostics['restored']==1


@pytest.mark.parametrize('error',[ReceiverPageError('synthetic'),ReceiverInterventionRequired('synthetic')])
def test_original_failure_is_preserved_and_layout_restored(error):
    receiver=fake()
    with pytest.raises(type(error)) as raised:
        with module._readback_viewport(receiver):raise error
    assert raised.value is error
    assert receiver.page.viewport_size=={'width':1100,'height':800}


def test_restore_failure_blocks_success_but_does_not_mask_human_intervention():
    receiver=fake();calls=[]
    def setter(value):
        calls.append(dict(value))
        if len(calls)>1:raise RuntimeError('synthetic restore failure')
    receiver.page.set_viewport_size=setter
    with pytest.raises(ReceiverPageError):
        with module._readback_viewport(receiver):pass
    assert receiver.last_choice_readback_viewport_diagnostics['restore_failed']==1
    calls.clear();challenge=ReceiverInterventionRequired('synthetic challenge')
    with pytest.raises(ReceiverInterventionRequired) as raised:
        with module._readback_viewport(receiver):raise challenge
    assert raised.value is challenge


@pytest.mark.parametrize('configured',[None,True,1440,1601,'1600'])
def test_no_exact_trusted_optin_makes_no_layout_change(configured):
    receiver=fake(configured=configured)
    with module._readback_viewport(receiver):pass
    assert receiver.page.changes==[]


def test_large_existing_viewport_is_not_shrunk_and_preexisting_stop_does_not_resize():
    receiver=fake(height=2000)
    with module._readback_viewport(receiver):pass
    assert receiver.page.changes==[]
    receiver=fake();receiver.choice_should_stop=lambda:True
    with pytest.raises(ReceiverPageError):
        with module._readback_viewport(receiver):pass
    assert receiver.page.changes==[]


def test_all_frozen_rows_visible_together_under1600_then_original_layout_restored(surface,monkeypatch):
    state=complete(surface);frozen=target(surface,state);unlink_avatar(surface)
    surface.page.locator('#receipt').evaluate('''e=>{
      const top=e.getBoundingClientRect().top;e.style.marginTop=Math.max(0,1100-top)+'px';
    }''')
    surface.page.set_viewport_size({'width':1100,'height':800})
    assert inspect(surface,state)['matching_reply_count']==0
    receiver=adapter(surface,state,monkeypatch);receiver.readback_viewport_height=1600
    result=module.observe_existing_choice_reply(receiver,frozen,TEXT)
    assert result['outcome']=='b_view_visible' and result['readback_quality']=='weak_unique_root'
    assert result['counts']['matching_reply_count']==result['counts']['existing_matching_reply_count']==1
    assert receiver.last_choice_readback_viewport_diagnostics=={'configured':1,'expanded':1,'restored':1,'restore_failed':0}
    assert surface.page.viewport_size=={'width':1100,'height':800}
    assert surface.page.evaluate('window.sendCount')==0
