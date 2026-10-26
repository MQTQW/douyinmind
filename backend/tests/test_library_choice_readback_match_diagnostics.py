"""Fixed matching-gate counters, fictional DOM with blocked networking."""
import json

import pytest

from tests.test_library_choice_weak_receipt_dom import browser,surface,complete,args,target,adapter,TEXT
from app.mention_receiver import choice_browser as module


def inspect(surface,state):
    return surface.page.evaluate(module.CHOICE_REPLY_STATE_JS,{**args(state),'readback_pagination':True})


def test_positive_flat_readback_explains_actor_exact_body_and_parent_counts(surface):
    state=complete(surface);value=inspect(surface,state)
    assert value['matching_reply_count']==1 and value['selected_source_count']==1
    assert value['readback_current_receiver_verified']==1
    assert value['readback_b_author_row_count']==2
    assert value['readback_own_text_equal_count']==value['readback_b_exact_text_equal_count']==1
    assert value['readback_b_direct_selected_parent_count']==0
    assert value['readback_b_flat_root_parent_count']==value['readback_b_to_sender_header_count']==1
    assert value['readback_b_plain_body_match_count']==1


@pytest.mark.parametrize('change,expect',[
    ('body',{'existing_matching_reply_count':0,'readback_b_exact_text_equal_count':0}),
    ('other_target',{'matching_reply_count':0,'existing_matching_reply_count':1,'readback_b_to_sender_header_count':0}),
    ('extra_prose',{'matching_reply_count':0,'existing_matching_reply_count':1,'readback_b_plain_body_match_count':0}),
    ('competing_A',{'matching_reply_count':0,'existing_matching_reply_count':1,'selected_source_count':2}),
])
def test_counters_distinguish_failures_without_weakening_match(surface,change,expect):
    state=complete(surface)
    surface.page.evaluate('''change=>{
      const receipt=document.getElementById('receipt');
      if(change==='body')receipt.querySelector('.FduGc_lz').textContent='different complete public body';
      if(change==='other_target')receipt.querySelectorAll('.comment-item-info-wrap a')[1].href='https://www.douyin.com/user/other-A';
      if(change==='extra_prose')receipt.querySelector('.comment-item-stats-container').parentElement.insertAdjacentHTML('afterend','<div>另一段正文</div>');
      if(change==='competing_A'){
        const row=document.getElementById('selection').cloneNode(true);row.id='another-A-reply';
        row.querySelector('.FduGc_lz').textContent='我在解释项目';receipt.before(row);
      }
    }''',change)
    value=inspect(surface,state)
    assert {key:value[key] for key in expect}==expect
    assert all(type(value[key]) is int and 0<=value[key]<=1000000 for key in module._READBACK_MATCH_COUNTS)


def test_optin_preserves_original_seven_match_counts_without_changing_store_result(surface,monkeypatch,tmp_path):
    state=complete(surface);frozen=target(surface,state)
    base=tmp_path/'assistant';base.mkdir()
    (base/'choice-observation-enabled.json').write_text(json.dumps({'choice_id':state['choice_id']}),encoding='utf-8')
    monkeypatch.setattr(module,'CHOICE_OBSERVATION_BASE',base)
    receiver=adapter(surface,state,monkeypatch)
    result=module.observe_existing_choice_reply(receiver,frozen,TEXT)
    last=json.loads((base/'choice-observations'/(state['choice_id']+'.jsonl')).read_text(encoding='ascii').splitlines()[-1])
    assert set(result['counts'])==module._READBACK_DURABLE_COUNTS
    assert all(last['counts'][key]==result['counts'][key] for key in module._READBACK_DURABLE_COUNTS)
    assert last['counts']['readback_b_exact_text_equal_count']==1
    assert last['counts']['readback_current_receiver_verified']==1
    assert set(result)=={'outcome','exact_body_in_original_thread','verification','reply_sha256','observed_at_utc','counts','readback_quality'}
    assert not any(word in json.dumps(last) for word in ('https://','cookie','claim_token','sender_profile_url',TEXT))


def test_lookup_stage_index_is_bounded_fixed_integer_only(tmp_path,monkeypatch):
    base=tmp_path/'assistant';base.mkdir()
    (base/'choice-observation-enabled.json').write_text(json.dumps({'choice_id':'c'*32}),encoding='utf-8')
    monkeypatch.setattr(module,'CHOICE_OBSERVATION_BASE',base)
    module._choice_observation({'choice_id':'c'*32},'failure','original_lookup_unavailable',lookup_stage_index=3,unknown_private='secret')
    records=(base/'choice-observations'/('c'*32+'.jsonl')).read_text(encoding='ascii')
    assert json.loads(records.splitlines()[-1])['counts']=={'lookup_stage_index':3}
    assert 'secret' not in records
