"""One legacy positive-readback ownership regression on intercepted Edge HTML.

Reuses fresh non-persistent offline contexts; no profile/account or actual send.
"""
import html

from tests.test_mention_agent_modal_reply_dom import (
    installed_browser, surface, document, state, B, OTHER, ANSWER,
)


def test_b_wrapper_cannot_borrow_nested_c_text_as_its_own_visible_reply(surface):
    surface.load(document())
    nested=(f'<article data-e2e="comment-item"><a href="{OTHER}" style="display:none">隐藏C</a>'
            f'<div>{html.escape(ANSWER)}</div></article>')
    wrapper=(f'<article data-e2e="comment-item" id="apparent-b-reply"><a href="{B}">虚构B</a>'
             f'<div>{nested}</div></article>')
    surface.page.locator('#original-root').evaluate('(element,value)=>element.insertAdjacentHTML("beforeend",value)',wrapper)
    result=state(surface)
    assert result['ok'] is True and result['current_receiver_verified'] is True
    assert result['matching_reply_count']==0,'C text inside a B wrapper must not establish B-authored readback'
    assert result['existing_matching_reply_count']==1,'ambiguous existing text remains a conservative no-send barrier'
    assert surface.page.evaluate('window.submitCount')==0
