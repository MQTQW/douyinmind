"""Fresh offline DOM: notification readiness must not count outside comments."""
import pytest
from playwright.sync_api import sync_playwright

from app.mention_receiver.browser import NOTICE_JS,NOTICE_READY_JS


STYLE='<style>body{margin:0} main{padding:12px} pre{white-space:pre-wrap} aside{position:fixed;right:20px;top:100px;width:420px;height:300px;background:white;overflow:auto}</style>'
COVER='<img width="32" height="32" src="data:image/svg+xml,%3Csvg%20xmlns=%27http://www.w3.org/2000/svg%27%20width=%2732%27%20height=%2732%27/%3E">'


def card(sender='inside'):
    return '<div><a href="https://www.douyin.com/user/'+sender+'">合成作者</a><span>在评论中提到了你</span><pre>@合成B 解释资料</pre>'+COVER+'</div>'


def document(pane,*,main='',extra='',controls='notice-pane'):
    return STYLE+'<header><button id="notify" aria-controls="'+controls+'" aria-expanded="true">通知</button></header><main><h1>合成B</h1>'+main+'</main><aside id="notice-pane" role="region" aria-labelledby="notify"><h2>互动消息</h2>'+pane+'</aside>'+extra


@pytest.fixture(scope='module')
def browser():
    with sync_playwright() as pw:
        value=pw.chromium.launch(channel='msedge',headless=True,proxy={'server':'http://127.0.0.1:9'},
            args=['--disable-background-networking','--disable-component-update','--disable-sync','--disable-quic','--no-first-run'])
        yield value
        value.close()


@pytest.fixture
def page(browser):
    context=browser.new_context(service_workers='block',viewport={'width':1200,'height':900},offline=True)
    attempts=[]
    context.route('**/*',lambda route:(attempts.append(route.request.url),route.abort()))
    value=context.new_page()
    yield value
    assert value.url=='about:blank' and attempts==[]
    context.close()


def test_unrelated_pre_does_not_make_known_empty_owned_pane_unready(page):
    page.set_content(document('<div>暂无消息</div>',main='<pre>@普通文本，不是通知</pre>'))
    result=page.evaluate(NOTICE_READY_JS)
    assert result is not False
    assert result['items']==[] and result['potential_mention_count']==0 and result['known_end_or_empty'] is True


def test_outside_lookalike_card_is_not_an_original_notification_candidate(page):
    page.set_content(document(card(),main=card('outside')))
    result=page.evaluate(NOTICE_JS)
    assert len(result['items'])==1
    assert result['items'][0]['source_profile_url']=='https://www.douyin.com/user/inside'
    assert result['items'][0]['pre_index']==1  # global index remains the real locator


def test_inside_unparsed_at_card_still_blocks_readiness(page):
    page.set_content(document('<pre>@合成B 尚未解析完整</pre><div>暂无消息</div>'))
    assert page.evaluate(NOTICE_READY_JS) is False


def test_two_distinct_owned_panes_are_not_resolved_by_first_or_empty_marker(page):
    page.set_content(document('<div>暂无消息</div>',controls='notice-pane other-pane',extra='<aside id="other-pane" role="region" aria-labelledby="notify" style="left:10px;top:450px"><h2>互动消息</h2><div>暂无消息</div></aside>'))
    assert page.evaluate(NOTICE_READY_JS) is False


def test_unowned_title_cannot_hide_unparsed_page_content(page):
    page.set_content(STYLE+'<main><h1>合成B</h1><div>互动消息</div><pre>@未解析，不能忽略</pre><div>暂无消息</div></main>')
    assert page.evaluate(NOTICE_READY_JS) is False


def test_unlinked_floating_title_alone_cannot_hide_real_page_mentions(page):
    page.set_content(STYLE+'<main><h1>合成B</h1><pre>@尚未解析的真实提醒</pre></main><aside role="dialog"><h2>互动消息</h2><div>暂无消息</div></aside>')
    assert page.evaluate(NOTICE_READY_JS) is False


def test_nested_same_pane_is_one_owner_and_hidden_old_pane_is_ignored(page):
    nested='<section style="position:fixed;right:25px;top:140px;width:380px;height:220px"><h3>互动消息</h3>'+card()+'</section>'
    old='<aside id="old-pane" role="region" aria-labelledby="notify" style="display:none"><h2>互动消息</h2><pre>@旧内容</pre></aside>'
    page.set_content(document(nested,main='<pre>@窗外普通文本</pre>',extra=old))
    value=page.evaluate(NOTICE_READY_JS)
    assert value is not False and len(value['items'])==1
    assert value['items'][0]['pre_index']==1


def test_observed_notice_component_marker_can_prove_scope_without_aria(page):
    # Shape from the already saved public NoticeEntry component: explicit
    # data-e2e root + own title/listBox, not a claim about current live DOM.
    page.set_content(STYLE+'<main><h1>合成B</h1><pre>@窗外代码文本</pre></main><aside data-e2e="listDlgTest-container"><div><div>互动消息</div></div><section>'+card()+'</section></aside>')
    value=page.evaluate(NOTICE_READY_JS)
    assert value is not False and len(value['items'])==1
    assert value['items'][0]['pre_index']==1


def test_two_observed_component_roots_remain_ambiguous_even_when_both_empty(page):
    pane='<aside data-e2e="listDlgTest-container"><div>互动消息</div><div>暂无消息</div></aside>'
    page.set_content(STYLE+'<main><h1>合成B</h1></main>'+pane+pane.replace('<aside','<aside style="left:10px;top:450px"',1))
    assert page.evaluate(NOTICE_READY_JS) is False
