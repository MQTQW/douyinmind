"""Only fixed-count diagnostics: no live browser/profile/network or secrets."""
import json

import pytest

from tests.test_library_choice_weak_receipt_dom import browser,surface,complete,args,target,adapter,TEXT
from tests.test_library_choice_readback_pagination import paged
from app.mention_receiver import choice_browser as module


@pytest.mark.parametrize('kind,expected',[
    ('native',{'readback_native_button_count':1,'readback_parent_reply_container_count':1,
               'readback_root_owned_more_count':1,'readback_unsupported_more_count':0}),
    ('body',{'readback_native_button_count':1,'readback_parent_reply_container_count':0,
             'readback_root_owned_more_count':1,'readback_unsupported_more_count':1}),
    ('wrapper',{'readback_native_button_count':1,'readback_parent_reply_container_count':0,
                'readback_reply_container_ancestor_count':1,'readback_unsupported_more_count':1}),
    ('legacy',{'readback_native_button_count':0,'readback_legacy_control_count':1,
               'readback_parent_reply_container_count':1,'readback_unsupported_more_count':1}),
    ('span',{'readback_span_control_count':1,'readback_native_button_count':0,'readback_unsupported_more_count':1}),
    ('child',{'readback_root_owned_more_count':0,'readback_parent_owned_by_root_count':0,
              'readback_unsupported_more_count':1}),
])
def test_counts_distinguish_exact_rejection_conditions_without_changing_gate(surface,kind,expected):
    state=complete(surface);paged(surface)
    surface.page.evaluate('''kind=>{
      const b=document.getElementById('readback-more');
      if(kind==='body')document.querySelector('#root .FduGc_lz').append(b);
      if(kind==='wrapper'){const d=document.createElement('div');b.replaceWith(d);d.append(b);}
      if(kind==='legacy'||kind==='span'){
        const d=document.createElement(kind==='span'?'span':'div');d.setAttribute('data-e2e','video-comment-more');
        d.textContent=b.textContent;b.replaceWith(d);
      }
      if(kind==='child')document.getElementById('selection').append(b);
    }''',kind)
    value=surface.page.evaluate(module.CHOICE_THREAD_JS,{**args(state),'readback_pagination':True})
    assert value['ok'] is True
    assert {key:value['counts'][key] for key in expected}==expected
    assert (value['expand_index'] is not None)==(kind=='native')
    assert all(type(value['counts'][key]) is int for key in module._READBACK_LAYOUT_COUNTS)
    assert surface.page.evaluate('window.readbackClickCount')==0


@pytest.mark.parametrize('label,key',[
    ('展开更多','readback_label_plain_more_count'),('展开5条回复','readback_label_numeric_replies_count'),
    ('查看更多回复','readback_label_more_replies_count'),('展开全部回复','readback_label_all_replies_count'),
    ('查看回复','readback_label_bare_replies_count'),
])
def test_labels_leave_page_only_as_fixed_category_counts(surface,label,key):
    state=complete(surface);paged(surface,label=label)
    value=surface.page.evaluate(module.CHOICE_THREAD_JS,{**args(state),'readback_pagination':True})
    label_counts={name:value['counts'][name] for name in module._READBACK_LAYOUT_COUNTS if 'label_' in name}
    assert label_counts[key]==1 and sum(label_counts.values())==1


def test_opt_in_log_and_receiver_diagnostics_keep_only_fixed_counts_and_old_store_contract(surface,monkeypatch,tmp_path):
    state=complete(surface);frozen=target(surface,state);paged(surface)
    surface.page.locator('#readback-more').evaluate('''b=>{
      const d=document.createElement('div');d.setAttribute('data-e2e','video-comment-more');d.textContent=b.textContent;b.replaceWith(d);
    }''')
    base=tmp_path/'assistant';base.mkdir()
    (base/'choice-observation-enabled.json').write_text(json.dumps({'choice_id':state['choice_id']}),encoding='utf-8')
    monkeypatch.setattr(module,'CHOICE_OBSERVATION_BASE',base)
    receiver=adapter(surface,state,monkeypatch)
    result=module.observe_existing_choice_reply(receiver,frozen,TEXT)
    diagnostic=receiver.last_choice_readback_diagnostics
    assert diagnostic['reason_code']=='choice_readback_expander_unsupported'
    assert diagnostic['counts']['readback_legacy_control_count']==1
    assert diagnostic['counts']['readback_native_button_count']==0
    assert result['outcome']=='delivery_unknown' and set(result)=={
        'outcome','exact_body_in_original_thread','verification','reply_sha256','observed_at_utc','counts','readback_quality'}
    raw=(base/'choice-observations'/(state['choice_id']+'.jsonl')).read_text(encoding='ascii')
    records=[json.loads(line) for line in raw.splitlines()]
    last=records[-1]
    assert last['stage']=='readback' and last['counts']['readback_legacy_control_count']==1
    assert all(type(number) is int and 0<=number<=1000000 for number in last['counts'].values())
    assert not any(value in raw for value in (TEXT,'https://','cookie','claim_token','sender_profile_url'))
    assert surface.page.evaluate('window.readbackClickCount')==0
