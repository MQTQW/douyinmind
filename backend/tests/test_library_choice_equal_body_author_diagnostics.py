"""Expected-public-body row diagnostics are fixed counts, never identity grants."""
import json

import pytest

from tests.test_library_choice_weak_receipt_dom import browser,surface,complete,args,target,adapter,TEXT
from app.mention_receiver import choice_browser as module


def inspect(surface,state):
    return surface.page.evaluate(module.CHOICE_REPLY_STATE_JS,{**args(state),'readback_pagination':True})


def test_known_canonical_B_row_has_matching_avatar_title_and_flat_root_counts(surface):
    state=complete(surface);value=inspect(surface,state)
    for key in ('identity_B','avatar_B','first_title_B','avatar_title_same_canonical','target_title_A','parent_root','public_row','body_interval','header_three_parts','header_arrow_shape','header_arrow_public'):
        assert value['readback_equal_'+key+'_count']==1
    assert value['readback_equal_identity_missing_count']==0
    assert value['matching_reply_count']==1


@pytest.mark.parametrize('change,expected',[
    ('both_self',{'identity_missing':1,'avatar_self':1,'first_title_self':1,'avatar_title_both_self':1,'avatar_title_same_canonical':0}),
    ('avatar_other',{'identity_missing':1,'avatar_B':0,'first_title_B':1,'avatar_title_same_canonical':0}),
    ('missing_title',{'identity_missing':1,'avatar_B':1,'title_link':1,'first_title_A':1,'avatar_title_same_canonical':0}),
    ('title_without_href',{'identity_missing':1,'avatar_B':1,'first_title_uncanonical':1,'avatar_title_same_canonical':0}),
    ('arrow_missing',{'identity_missing':1,'avatar_B':1,'first_title_B':1,'header_three_parts':0,'header_arrow_shape':0}),
    ('extra_title',{'identity_missing':1,'avatar_B':1,'first_title_B':1,'title_link':3}),
])
def test_noncanonical_or_conflicting_authors_are_diagnosed_but_not_accepted(surface,change,expected):
    state=complete(surface)
    surface.page.evaluate('''change=>{
      const row=document.getElementById('receipt'),avatar=row.querySelector('.comment-item-avatar a'),
        titles=row.querySelectorAll('.comment-item-info-wrap a');
      if(change==='both_self'){avatar.href='/user/self';titles[0].href='/user/self';}
      if(change==='avatar_other')avatar.href='/user/not-B';
      if(change==='missing_title')titles[0].querySelector('[data-click-from="title"]').removeAttribute('data-click-from');
      if(change==='title_without_href')titles[0].removeAttribute('href');
      if(change==='arrow_missing')row.querySelector('.ym1IYI4C').remove();
      if(change==='extra_title'){const third=titles[1].cloneNode(true);third.href='/user/other-title';titles[1].after(third);}
    }''',change)
    value=inspect(surface,state)
    assert value['readback_own_text_equal_count']==1
    assert value['matching_reply_count']==value['existing_matching_reply_count']==0
    assert {key:value['readback_equal_'+key+'_count'] for key in expected}==expected
    assert all(type(value[key]) is int for key in module._READBACK_EQUAL_AUTHOR_COUNTS)


def test_missing_semantic_avatar_structure_is_not_replaced_by_body_mention(surface):
    state=complete(surface)
    surface.page.locator('#receipt .comment-item-avatar').evaluate("e=>e.className='unrecognized-avatar-layout'")
    value=inspect(surface,state)
    assert value['readback_equal_avatar_region_count']==0
    assert value['readback_equal_identity_missing_count']==1
    assert value['matching_reply_count']==0


def test_equal_body_author_diagnostics_are_logged_without_body_or_urls(surface,monkeypatch,tmp_path):
    state=complete(surface);frozen=target(surface,state)
    surface.page.locator('#receipt .comment-item-avatar a').evaluate("e=>e.href='/user/self'")
    surface.page.locator('#receipt .comment-item-info-wrap a').first.evaluate("e=>e.href='/user/self'")
    base=tmp_path/'assistant';base.mkdir()
    (base/'choice-observation-enabled.json').write_text(json.dumps({'choice_id':state['choice_id']}),encoding='utf-8')
    monkeypatch.setattr(module,'CHOICE_OBSERVATION_BASE',base)
    receiver=adapter(surface,state,monkeypatch)
    result=module.observe_existing_choice_reply(receiver,frozen,TEXT)
    assert result['outcome']=='delivery_unknown'
    diagnostic=receiver.last_choice_readback_diagnostics
    assert diagnostic['counts']['readback_equal_avatar_title_both_self_count']==1
    assert diagnostic['counts']['readback_equal_identity_missing_count']==1
    path=base/'choice-observations'/(state['choice_id']+'.jsonl');raw=path.read_text(encoding='ascii')
    last=json.loads(raw.splitlines()[-1])
    assert last['counts']['readback_equal_avatar_title_both_self_count']==1
    assert not any(value in raw for value in ('/user/','https://','cookie','claim_token',TEXT))
    assert set(result['counts'])==module._READBACK_DURABLE_COUNTS
