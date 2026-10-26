"""Thread proof must follow visible ancestry, never the last same-user post."""
import html

import pytest
from playwright.sync_api import sync_playwright

from app.mention_receiver.browser import THREAD_REFERENCE_JS


A = "https://www.douyin.com/user/synthetic-a"
B = "https://www.douyin.com/user/synthetic-b"
OTHER = "https://www.douyin.com/user/synthetic-other"
ROOT = "@6 请说明Git的四个区域。"
CHILD = "@6 详细说说第二点。"


def comment(body, *, owner=A, recipient=B, children="", hidden=False):
    suffix = body.removeprefix("@6")
    return (f'<article data-e2e="comment-item" style="{"display:none" if hidden else ""}">'
            f'<a href="{owner}">发送者</a><div><a href="{recipient}">@6</a>{html.escape(suffix)}</div>'
            f'{children}</article>')


@pytest.fixture(scope="module")
def browser():
    with sync_playwright() as p:
        browser = p.chromium.launch(channel="msedge", headless=True)
        yield browser
        browser.close()


@pytest.fixture
def extract(browser):
    context = browser.new_context(viewport={"width":1100,"height":1000}, service_workers="block")
    requests = []
    context.route("**/*", lambda route: (requests.append(route.request.url), route.abort()))
    page = context.new_page()
    def run(body, *, text=CHILD, roots=None):
        page.set_content('<style>article,div{display:block;min-height:24px;padding:4px}</style>'+body)
        return page.evaluate(THREAD_REFERENCE_JS, {"sender":A,"receiver":B,"text":text,
            "reference_id":"current-ref", "roots":roots if roots is not None else [{"reference_id":"root-ref","comment_text":ROOT}]})
    yield run
    assert page.url == "about:blank" and requests == []
    context.close()


def test_root_is_its_own_thread(extract):
    assert extract(comment(CHILD)) == {"quality":"current_comment","root_reference_id":"current-ref"}


def test_only_verified_visible_ancestor_reuses_prior_context(extract):
    assert extract(comment(ROOT, children=comment(CHILD))) == {"quality":"verified_ancestor","root_reference_id":"root-ref"}


@pytest.mark.parametrize("ancestor", [
    {"owner":OTHER}, {"recipient":OTHER}, {"hidden":True},
])
def test_changed_ancestor_identity_or_hidden_root_does_not_reuse_context(extract, ancestor):
    result = extract(comment(ROOT, children=comment(CHILD), **ancestor))
    assert result == {"quality":"independent","root_reference_id":"current-ref"}


def test_nearby_same_sender_root_is_not_an_ancestor(extract):
    assert extract(comment(ROOT)+comment(CHILD))["quality"] == "current_comment"


def test_unknown_or_truncated_ancestor_does_not_borrow_context(extract):
    assert extract(comment("@6 请说明Git的四个区...", children=comment(CHILD)))["quality"] == "independent"


def test_repeated_identical_current_comments_are_ambiguous(extract):
    assert extract(comment(ROOT, children=comment(CHILD))+comment(CHILD))["quality"] == "independent"


def test_hidden_duplicate_does_not_hide_the_unique_visible_thread(extract):
    result = extract(comment(CHILD, hidden=True)+comment(ROOT, children=comment(CHILD)))
    assert result["quality"] == "verified_ancestor"


def test_ambiguous_recorded_root_identifiers_fail_closed(extract):
    roots=[{"reference_id":"r1","comment_text":ROOT},{"reference_id":"r2","comment_text":ROOT}]
    assert extract(comment(ROOT, children=comment(CHILD)), roots=roots)["quality"] == "independent"
