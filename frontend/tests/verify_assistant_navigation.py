"""Fresh-browser regression for chat/assistant navigation using an in-page fake SSE.

Every API is synthetic and all external requests are aborted. This does not use
an existing profile, model, account, backend database, or real comment sender.
"""
import json
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

from playwright.sync_api import expect, sync_playwright

BASE = "http://127.0.0.1:5187"
ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "backend/app/storage/ui_review/2026-10-21/assistant-product-demo-v2"

FAKE_STREAM = r"""(() => {
  const nativeFetch = window.fetch.bind(window);
  const encoder = new TextEncoder();
  window.__navigationTest = {calls: [], aborts: 0, cancellations: 0, finish: null};
  window.fetch = async (input, init = {}) => {
    if (new URL(typeof input === 'string' ? input : input.url, location.href).pathname !== '/api/chat/ask/stream') return nativeFetch(input, init);
    const body = JSON.parse(init.body);
    const test = window.__navigationTest;
    test.calls.push(body);
    let controller;
    const emit = (event, data) => controller.enqueue(encoder.encode(`event: ${event}\ndata: ${JSON.stringify(data)}\n\n`));
    const aborted = () => { test.aborts++; controller.error(new DOMException('Aborted', 'AbortError')); };
    const stream = new ReadableStream({start(value) { controller = value; }, cancel() { test.cancellations++; }});
    init.signal?.addEventListener('abort', aborted, {once: true});
    emit('sources', {sources: []});
    emit('delta', {text: '隔离测试回答第一部分。'});
    test.finish = (keepOpen = false) => {
      emit('delta', {text: '切换助手后继续完成。'});
      emit('meta', {session_id: 501, route_type: 'vector', answer: '隔离测试回答第一部分。切换助手后继续完成。', sources: [], latency_ms: 100});
      emit('done', {ok: true});
      init.signal?.removeEventListener('abort', aborted);
      if (!keepOpen) controller.close(); test.finish = null;
    };
    return new Response(stream, {headers: {'Content-Type': 'text/event-stream'}});
  };
})();"""


def run():
    calls, blocked, errors = [], [], []
    libraries = [{"id": 7, "name": "学习与工作（隔离导航）", "video_count": 2, "ready_count": 2},
                 {"id": 8, "name": "生活灵感（隔离导航）", "video_count": 1, "ready_count": 1}]
    overview = {"available": True, "control": {"state": "running", "receiver_state": "ready",
                 "worker_state": "error", "worker_reason": "tool_error"}, "counts": {},
                "capabilities": {"start": False, "pause": True, "stop": True}, "binding": None}

    def handle(route):
        request = route.request
        parsed = urlparse(request.url)
        if parsed.netloc != "127.0.0.1:5187":
            blocked.append(request.url); route.abort(); return
        if not parsed.path.startswith("/api/"):
            route.continue_(); return
        calls.append({"path": parsed.path, "method": request.method})
        assert request.method == "GET", "All synthetic chat POSTs must stay inside the fake fetch"
        if parsed.path == "/api/auth/douyin/login/status": data = {"status": "logged_in", "message": "隔离测试"}
        elif parsed.path == "/api/knowledge-bases": data = {"success": True, "items": libraries, "total": 2}
        elif parsed.path == "/api/knowledge/stats": data = {"success": True, "video_cache": {"done": 3, "pending": 0, "failed": 0}}
        elif parsed.path == "/api/chat/sessions": data = {"success": True, "items": [{"id": 501, "title": "隔离中途导航问题", "knowledge_base_id": 8, "collection_id": "all", "message_count": 2, "created_at": "2026-10-21T09:00:00Z"}], "total": 1}
        elif parsed.path == "/api/assistant/overview": data = overview
        elif parsed.path == "/api/assistant/tasks": data = {"items": [], "total": 0, "page": 1, "page_size": 10}
        elif parsed.path == "/api/favorites/collections" or "/videos" in parsed.path: data = {"success": True, "items": [], "total": 0}
        else: raise AssertionError(f"Unexpected route {parsed.path}")
        route.fulfill(content_type="application/json; charset=utf-8", body=json.dumps(data, ensure_ascii=False))

    with sync_playwright() as p:
        browser = p.chromium.launch(channel="msedge", headless=True)
        context = browser.new_context(viewport={"width": 1440, "height": 1000}, locale="zh-CN")
        context.set_default_timeout(12000)
        context.route("**/*", handle)
        context.add_init_script(FAKE_STREAM)
        page = context.new_page()
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.goto(BASE, wait_until="networkidle")
        page.locator(".library-open").filter(has_text="生活灵感").click()
        expect(page.locator(".workspace-title")).to_have_text("生活灵感（隔离导航）")
        page.get_by_label("输入问题", exact=True).fill("隔离中途导航问题")
        page.get_by_role("button", name="发送问题", exact=True).click()
        expect(page.locator(".chat-room")).to_contain_text("隔离测试回答第一部分。")
        expect(page.locator(".library-open").first).to_be_disabled()
        page.locator(".assistant-sidebar-shortcuts .task-shortcut").filter(has_text="抖音助手").click()
        expect(page.locator(".workspace-title")).to_have_text("抖音助手")
        expect(page.locator(".assistant-control-card")).to_contain_text("工具处理出现问题，任务记录已保留")
        expect(page.locator(".assistant-control-card")).not_to_contain_text("正在收件")
        assert page.evaluate("window.__navigationTest.aborts") == 0
        page.get_by_role("button", name="返回知识库", exact=True).click()
        expect(page.locator(".workspace-title")).to_have_text("生活灵感（隔离导航）")
        expect(page.locator(".chat-room")).to_contain_text("隔离测试回答第一部分。")
        expect(page.locator(".chat-room").get_by_role("button", name="发送问题", exact=True)).to_be_disabled()
        page.evaluate("window.__navigationTest.finish(true)")
        expect(page.locator(".chat-room")).to_contain_text("切换助手后继续完成。")
        expect(page.locator(".library-open").first).to_be_enabled()
        assert page.evaluate("window.__navigationTest.cancellations") == 1
        page.get_by_label("输入问题", exact=True).fill("同一会话的第二个问题")
        page.get_by_role("button", name="发送问题", exact=True).click()
        page.evaluate("window.__navigationTest.finish()")
        expect(page.get_by_label("输入问题", exact=True)).to_be_enabled()
        actual = page.evaluate("({calls:window.__navigationTest.calls,aborts:window.__navigationTest.aborts,cancellations:window.__navigationTest.cancellations})")
        assert actual["aborts"] == 0
        assert len(actual["calls"]) == 2
        assert actual["calls"][0]["knowledge_base_id"] == actual["calls"][1]["knowledge_base_id"] == 8
        assert actual["calls"][1]["session_id"] == 501
        assert actual["calls"][0].get("session_id") is None
        expect(page.locator(".chat-room")).to_contain_text("隔离中途导航问题")
        expect(page.locator(".chat-room")).to_contain_text("同一会话的第二个问题")
        page.set_viewport_size({"width": 390, "height": 844})
        page.get_by_role("button", name="资料库", exact=True).click()
        expect(page.locator("aside")).to_be_visible()
        page.locator(".assistant-sidebar-shortcuts .task-shortcut").filter(has_text="抖音助手").click()
        expect(page.locator(".workspace-title")).to_have_text("抖音助手")
        expect(page.locator("aside")).not_to_be_visible()
        page.get_by_role("button", name="资料库", exact=True).click()
        expect(page.locator(".library-open").filter(has_text="生活灵感")).to_have_attribute("aria-pressed", "true")
        page.get_by_role("button", name="返回工作区", exact=True).click()
        expect(page.locator(".workspace-title")).to_have_text("抖音助手")
        page.get_by_role("button", name="返回知识库", exact=True).click()
        expect(page.locator(".workspace-title")).to_have_text("生活灵感（隔离导航）")
        expect(page.locator(".chat-room")).to_contain_text("同一会话的第二个问题")
        assert not page.evaluate("document.documentElement.scrollWidth > innerWidth")
        page.get_by_role("button", name="资料库", exact=True).click()
        page.locator(".library-open").filter(has_text="学习与工作").click()
        expect(page.locator(".workspace-title")).to_have_text("学习与工作（隔离导航）")
        expect(page.locator(".chat-room")).not_to_contain_text("同一会话的第二个问题")
        context.close(); browser.close()
    assert not errors, errors
    assert not blocked, blocked
    report = {"verified_at_utc": datetime.now(timezone.utc).isoformat(), "synthetic_only": True,
        "checks": ["assistant_navigation_during_stream_keeps_request_alive", "return_preserves_partial_answer_and_selected_library",
                   "completion_continues_same_request", "completed_protocol_releases_busy_without_http_eof", "followup_preserves_session_id_and_library", "worker_error_not_hidden_by_receiver_ready",
                   "mobile_sidebar_and_return_navigation", "mobile_no_horizontal_overflow", "explicit_library_change_still_resets_scope"],
        "stream_calls": len(actual["calls"]), "stream_aborts": actual["aborts"], "completed_reader_cancellations": actual["cancellations"], "runtime_errors": errors,
        "external_requests": blocked, "network_mutation_count": sum(c["method"] != "GET" for c in calls)}
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "navigation-verification.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"checks": len(report["checks"]), "stream_aborts": actual["aborts"], "runtime_errors": len(errors)}, ensure_ascii=True))


if __name__ == "__main__":
    run()
