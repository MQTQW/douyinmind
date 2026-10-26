"""Modal reply boundaries on fully intercepted, synthetic offline pages.

Each test uses a new non-persistent Edge context, without login/profile state.
All requests are fulfilled with fixture HTML or aborted; none is forwarded.
Clicks only invoke local fixture JavaScript and never publish to a platform.
"""
import html
import time
from types import SimpleNamespace

import pytest
from playwright.sync_api import sync_playwright

from app.mention_agent.publisher import ReplyPreflightError
from app.mention_receiver.browser import NormalPageReceiver, ReceiverIdentity, REPLY_STATE_JS, SELF_URL


A = "https://www.douyin.com/user/modal-fictional-A"
B = "https://www.douyin.com/user/modal-fictional-B"
OTHER = "https://www.douyin.com/user/modal-fictional-other"
NUMBER = "123456789"
NAME = "虚构A"
VID = "7999999000000000011"
OTHER_VID = "7999999000000000022"
QUESTION = "@虚构B 请把这个视频加入“测试知识库”知识库。测试编号 DM-MODAL-FIXTURE-01。"
ANSWER = "已完成这条虚构视频的入库。"
HEADER = f"回复@{NAME}: {QUESTION}"
URL = f"https://www.douyin.com/user/self?modal_id={VID}"
TARGET = dict(sender_profile_url=A, receiver_profile_url=B, video_id=VID, native_comment_id=None,
              comment_text=QUESTION, reference_id="modal-fixture-reference", source_mode="live_ui")


def header_node(text, *, hidden=False, identity="parent-header-0"):
    style = ' style="display:none"' if hidden else ""
    return f'<span class="parent-reference" id="{identity}"{style}>{html.escape(text)}</span>'


def document(*, headers=None, outside="", public_numbers=(NUMBER,), actor_links=(),
             extra_editor=False, send_count=1, show_editor=True, draft=ANSWER):
    if headers is None:
        headers = [(HEADER, False)]
    header_html = "".join(header_node(text, hidden=hidden, identity=f"parent-header-{index}")
                          for index, (text, hidden) in enumerate(headers))
    numbers = "".join(f'<p class="public-number">抖音号：{html.escape(number)}</p>' for number in public_numbers)
    actors = ("<div id=\"comment-input-container\">" + "".join(
        f'<a href="{html.escape(actor, quote=True)}"><img alt="虚构头像"></a>' for actor in actor_links) + "</div>"
        if actor_links else "")
    extra = '<div contenteditable="true" role="combobox" id="extra-editor">第二输入框</div>' if extra_editor else ""
    sends = "".join(f'<span class="send-control" id="send-{index}" onclick="postReply()"><svg><path '
                    'fill="#FE2C55" d="M17.5 30C23.851"></path></svg></span>' for index in range(send_count))
    suffix = QUESTION[len("@虚构B"):]
    visible = "block" if show_editor else "none"
    return f'''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><style>
      body{{margin:0;padding:8px;font:14px/1.5 sans-serif}}
      article,section,div{{display:block;min-height:20px;padding:4px}}
      span{{display:inline-block;min-width:30px;min-height:24px}}
      .parent-reference{{display:block}}svg{{width:36px;height:36px}}img{{width:20px;height:20px}}
      [contenteditable]{{white-space:pre-wrap}}a{{display:inline-block;min-height:20px}}
      </style><body>{numbers}{actors}
      <section data-e2e="detail-video-info" data-e2e-aweme-id="{VID}">虚构视频信息</section>
      <article data-e2e="comment-item" id="original-root"><a href="{A}">{NAME}</a>
      <div class="original-body"><a href="{B}">@虚构B</a>{html.escape(suffix)}</div>
      <span id="open-reply" onclick="openReply()">回复</span></article>
      {outside}
      <section class="comment-input-container" id="global-composer" style="display:{visible}">
        {header_html}<div class="comment-input-inner-container">
        <div contenteditable="true" role="combobox" id="composer-editor">{html.escape(draft)}</div>
        {sends}</div></section>{extra}
      <script>
      window.submitCount=0;window.replyOpenCount=0;
      function openReply(){{window.replyOpenCount++;document.getElementById('global-composer').style.display='block';}}
      function postReply(){{
        window.submitCount++;
        const child=document.createElement('article');child.setAttribute('data-e2e','comment-item');
        const author=document.createElement('a');author.href='{B}';author.textContent='虚构B';
        const body=document.createElement('div');body.textContent=document.getElementById('composer-editor').innerText;
        child.append(author,body);document.getElementById('original-root').append(child);
        document.getElementById('composer-editor').textContent='';
      }}
      </script></body></html>'''


@pytest.fixture(scope="module")
def installed_browser():
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(channel="msedge", headless=True)
        yield browser
        browser.close()


@pytest.fixture
def surface(installed_browser):
    context = installed_browser.new_context(viewport={"width": 1100, "height": 1000}, service_workers="block")
    fixture = {"html": ""}
    requests = []

    def intercept(route):
        request = route.request
        if request.is_navigation_request() and request.resource_type == "document":
            requests.append((request.url, "fixture_fulfilled"))
            route.fulfill(status=200, content_type="text/html; charset=utf-8", body=fixture["html"])
        else:
            requests.append((request.url, "aborted"))
            route.abort()

    context.route("**/*", intercept)
    page = context.new_page()

    def load(content, *, url=URL):
        fixture["html"] = content
        page.goto(url, wait_until="domcontentloaded")

    yield SimpleNamespace(page=page, load=load, requests=requests)
    context.close()
    assert requests and all(action in {"fixture_fulfilled", "aborted"} for _, action in requests)


def state(surface, **changes):
    return surface.page.evaluate(REPLY_STATE_JS, {
        "sender": A, "receiver": B, "wanted": QUESTION, "video": VID,
        "native_comment_id": None, "reply": ANSWER, "identity_fresh": True,
        "expected_public_number": NUMBER, **changes,
    })


def send_ready(value):
    return (value.get("ok") is True and value.get("current_receiver_verified") is True
            and value.get("composer_unique") is True and value.get("composer_owned") is True
            and value.get("composer_scope") == "modal" and value.get("send_control_count") == 1
            and (value.get("composer_text") or "").strip() == ANSWER)


def adapter(surface, monkeypatch):
    page = surface.page
    value = NormalPageReceiver(page, NUMBER, B)
    navigations, details = [], []
    monkeypatch.setattr(page, "goto", lambda url, **kwargs: navigations.append(url))
    monkeypatch.setattr(value, "identity", lambda: ReceiverIdentity(NUMBER, "虚构B"))
    notice = {"source_profile_url": A, "exact_text": QUESTION, "semantic_fingerprint": "/fixture-cover.jpg"}
    monkeypatch.setattr(value, "notifications", lambda: [notice])

    def detail(notice, *, keep_open=False):
        details.append(keep_open)
        return dict(sender_profile_url=A, receiver_profile_url=B, video_id=VID,
                    native_comment_id=None, command_text=QUESTION)

    monkeypatch.setattr(value, "detail", detail)
    return value, navigations, details


def test_complete_parent_header_and_fresh_self_number_bind_global_editor(surface):
    surface.load(document())
    value = state(surface)
    assert send_ready(value)
    assert value["parent_header_count"] == 1
    assert value["detail"]["command_text"] == QUESTION
    assert surface.page.url == URL


@pytest.mark.parametrize("headers,outside", [
    ([(f"回复@{NAME}", False)], ""),
    ([(f"回复@{NAME}: @虚构B 另一个问题", False)], ""),
    ([(HEADER[:28] + "…", False)], ""),
    ([(HEADER, True), (f"回复@{NAME}: 已换成另一个原评论", False)], ""),
    ([(f"回复@{NAME}: 错误正文", False)], '<aside class="comment-input-container">' + header_node(HEADER) + "</aside>"),
    ([(HEADER, False), (HEADER, False)], ""),
])
def test_nickname_truncation_wrong_hidden_or_other_container_header_cannot_authorize(surface, headers, outside):
    surface.load(document(headers=headers, outside=outside))
    value = state(surface)
    assert not send_ready(value)
    assert value.get("composer_owned") is False
    assert surface.page.evaluate("window.submitCount") == 0


@pytest.mark.parametrize("options", [{"extra_editor": True}, {"send_count": 2}, {"send_count": 0}])
def test_editor_and_send_control_must_be_unique(surface, options):
    surface.load(document(**options))
    assert not send_ready(state(surface))
    assert surface.page.evaluate("window.submitCount") == 0


@pytest.mark.parametrize("numbers", [(), ("888888888",), (NUMBER, "888888888")])
def test_missing_wrong_or_conflicting_public_self_number_does_not_verify_actor(surface, numbers):
    surface.load(document(public_numbers=numbers))
    value = state(surface)
    assert value["current_receiver_verified"] is False
    assert not send_ready(value)


@pytest.mark.parametrize("fresh", [False, None, "true", 1])
def test_modal_actor_requires_explicit_fresh_identity_evidence(surface, fresh):
    surface.load(document())
    value = state(surface, identity_fresh=fresh)
    assert value["current_receiver_verified"] is False
    assert not send_ready(value)


@pytest.mark.parametrize("links", [(OTHER,), (B, OTHER)])
def test_contradictory_composer_actor_links_override_modal_fallback(surface, links):
    surface.load(document(actor_links=links))
    assert state(surface)["current_receiver_verified"] is False
    assert not send_ready(state(surface))


def test_different_modal_video_cannot_reuse_same_self_number_and_header(surface):
    surface.load(document(), url=f"https://www.douyin.com/user/self?modal_id={OTHER_VID}")
    assert not send_ready(state(surface))
    surface.load(document())
    assert state(surface, video=OTHER_VID)["ok"] is False


def test_foreign_origin_cannot_impersonate_self_modal_with_same_public_markup(surface):
    # This URL is intercepted and fulfilled locally too; no DNS/HTTP fallback
    # or navigation to a real service is used by the fixture.
    surface.load(document(), url=f"https://modal-fixture.invalid/user/self?modal_id={VID}")
    value = state(surface)
    assert value["current_receiver_verified"] is False
    assert not send_ready(value)


@pytest.mark.parametrize("age", [121, -1])
def test_adapter_rejects_expired_or_future_identity_timestamp(surface, age):
    surface.load(document())
    value = NormalPageReceiver(surface.page, NUMBER, B)
    value._reply_identity_checked_at = time.monotonic() - age
    with pytest.raises(ReplyPreflightError, match="current_actor_not_verified"):
        value._reply_state(TARGET, ANSWER)
    assert surface.page.evaluate("window.submitCount") == 0


def test_prepare_preserves_notification_modal_then_one_gate_and_fixture_click(surface, monkeypatch):
    surface.load(document(show_editor=False, draft=""))
    value, navigations, details = adapter(surface, monkeypatch)
    verification = value.prepare_reply(TARGET, ANSWER)
    assert verification["video_id"] == VID and verification["current_receiver_verified"] is True
    assert navigations == [SELF_URL, SELF_URL], "final proof reopens the verified notification, never the video's default comment page"
    assert details == [True, True]
    assert surface.page.evaluate("window.replyOpenCount") == 1
    assert surface.page.evaluate("window.submitCount") == 0
    assert send_ready(state(surface))
    gates = []
    observed = value.submit_reply(TARGET, ANSWER, before_submit=lambda: gates.append("one_gate"))
    assert gates == ["one_gate"]
    assert surface.page.evaluate("window.submitCount") == 1
    assert observed == {"outcome": "b_view_visible", "exact_body_in_original_thread": True}


@pytest.mark.parametrize("change", ["header", "actor", "video", "draft"])
def test_prepared_modal_target_changes_are_rejected_before_send_gate(surface, monkeypatch, change):
    surface.load(document(show_editor=False, draft=""))
    value, _, _ = adapter(surface, monkeypatch)
    value.prepare_reply(TARGET, ANSWER)
    if change == "header":
        surface.page.locator("#parent-header-0").evaluate("node=>node.textContent='回复@虚构A: 已切换到另一条评论'")
    elif change == "actor":
        surface.page.evaluate("""other=>{
          const box=document.createElement('div');box.id='comment-input-container';
          const a=document.createElement('a');a.href=other;const img=document.createElement('img');
          img.alt='虚构矛盾账号';a.append(img);box.append(a);document.body.append(box);
        }""", OTHER)
    elif change == "video":
        surface.page.evaluate("url=>history.replaceState({},'',url)", f"/user/self?modal_id={OTHER_VID}")
    else:
        surface.page.locator("#composer-editor").fill("已有草稿被人为改动")
    gates = []
    with pytest.raises(ReplyPreflightError):
        value.submit_reply(TARGET, ANSWER, before_submit=lambda: gates.append("forbidden"))
    assert gates == []
    assert surface.page.evaluate("window.submitCount") == 0


def test_gate_cancellation_prevents_modal_fixture_click(surface, monkeypatch):
    surface.load(document(show_editor=False, draft=""))
    value, _, _ = adapter(surface, monkeypatch)
    value.prepare_reply(TARGET, ANSWER)

    def stop():
        raise ReplyPreflightError("synthetic_run_stopped")

    with pytest.raises(ReplyPreflightError, match="synthetic_run_stopped"):
        value.submit_reply(TARGET, ANSWER, before_submit=stop)
    assert surface.page.evaluate("window.submitCount") == 0
