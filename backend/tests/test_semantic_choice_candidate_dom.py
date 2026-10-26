"""Raw native text wakes an exact UI row, never lets a model pick a row."""
import pytest

from tests.test_library_choice_weak_receipt_dom import browser,surface,complete,args
from app.mention_receiver import choice_browser as module


RAW='请帮我放到生活资料那个库里，谢谢'


def read(surface,state,text=RAW):
    values=module._session_args(state,[],candidate_text=text)
    return surface.page.evaluate(module.CHOICE_THREAD_JS,values)


def test_unenumerated_raw_text_keeps_real_body_and_parent_evidence(surface):
    state=complete(surface)
    surface.page.locator('#selection .FduGc_lz').evaluate('(e,text)=>e.textContent=text',RAW)
    value=read(surface,state)
    assert value['ok'] is True and len(value['details'])==1
    detail=value['details'][0]
    assert detail['command_text']==RAW and detail['evidence']['selection_text']==RAW
    assert detail['evidence']['real_mention'] is False and detail['evidence']['root_chain_verified'] is True
    assert len(module._session_args(state,[],candidate_text=RAW)['allowed_selections'])==1


@pytest.mark.parametrize('change',['wrong_text','duplicate_row','extra_body','wrong_A','wrong_B','other_root'])
def test_raw_wakeup_does_not_remove_identity_fullbody_unique_or_parent_gates(surface,change):
    state=complete(surface)
    surface.page.locator('#selection .FduGc_lz').evaluate('(e,text)=>e.textContent=text',RAW)
    surface.page.evaluate('''change=>{
      const row=document.getElementById('selection');
      if(change==='wrong_text')row.querySelector('.FduGc_lz').textContent='different actual source';
      if(change==='duplicate_row')row.after(row.cloneNode(true));
      if(change==='extra_body')row.querySelector('.FduGc_lz').after(Object.assign(document.createElement('div'),{textContent:'another actual body'}));
      if(change==='wrong_A')row.querySelector('.comment-item-avatar a').href='/user/another-A';
      if(change==='wrong_B')row.querySelectorAll('.comment-item-info-wrap a')[1].href='/user/another-B';
      if(change==='other_root')document.getElementById('root').after(row);
    }''',change)
    value=read(surface,state)
    assert value.get('ok') is False or value['details']==[]


def test_long_raw_reply_does_not_require_growing_a_forms_dictionary(surface):
    state=complete(surface);text='请帮我放到生活资料那个库里。'+'这是我原话补充。'*80
    assert len(text)>512
    surface.page.locator('#selection .FduGc_lz').evaluate('(e,value)=>e.textContent=value',text)
    value=read(surface,state,text)
    assert value['ok'] is True and value['details'][0]['command_text']==text


def test_default_forms_path_still_rejects_unenumerated_body(surface):
    state=complete(surface)
    surface.page.locator('#selection .FduGc_lz').evaluate('(e,text)=>e.textContent=text',RAW)
    value=surface.page.evaluate(module.CHOICE_THREAD_JS,args(state))
    assert value['ok'] is True and value['details']==[]
