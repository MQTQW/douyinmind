"""Isolated UI verification: fresh Edge, synthetic API only, no account/model calls.

Run with the backend venv Python while the frontend serves on 127.0.0.1:5187.
Every /api request is fulfilled here; all non-local requests are aborted.
Screenshots use the explicit assistantDemo=1 branch and retain its visible label.
"""
from __future__ import annotations

import copy
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from playwright.sync_api import sync_playwright, expect


BASE = "http://127.0.0.1:5187"
ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "backend/app/storage/ui_review/2026-10-21/assistant-product-demo-v2"
NOW = "2026-10-21T09:00:00Z"
TOKEN = "synthetic-memory-only-admin-token"
USER = "fixture-user"
DEVICE = "fixture-device"


def task(identity, state, title, **extra):
    return {"id": identity, "kind": "qa", "state": state, "video_title": title,
            "comment_text": "@演示助手 请根据我的学习库回答这个问题。", "created_at": NOW,
            "updated_at": NOW, "knowledge_base_id": 7,
            "actions": {"can_reconcile": state == "delivery_unknown", "can_resume": False}, **extra}


def run():
    OUT.mkdir(parents=True, exist_ok=True)
    requests, blocked, errors, checks = [], [], [], []
    registry = {"configured": True, "local_instance_id": "fixture-local", "local_catalog": [
        {"id": 7, "name": "学习与工作（隔离测试）"}, {"id": 8, "name": "生活灵感（隔离测试）"}],
        "cross_device_status": "local_protocol_only_not_remote_verified", "users": [{
            "user_id": USER, "label": "隔离测试账号", "active": True, "verification_status": "verified",
            "sender_profile_url": "https://www.douyin.com/user/fixture-person",
            "receiver_profile_url": "https://www.douyin.com/user/fixture-assistant",
            "default_device_id": DEVICE, "devices": [{"device_id": DEVICE, "label": "隔离本机",
                "instance_id": "fixture-local", "is_local": True, "enabled": True, "revoked": False,
                "verification_status": "verified", "online": False, "libraries": [{
                    "grant_id": "fixture-grant", "knowledge_base_id": 7, "name": "学习与工作（隔离测试）",
                    "aliases": ["学习库"], "is_default": True, "can_query": True, "can_import": True,
                    "public_reply_allowed": True, "library_exists": True}]}, {
                "device_id": "fixture-remote", "label": "隔离远端", "instance_id": "fixture-remote-instance",
                "is_local": False, "enabled": True, "revoked": False, "verification_status": "unverified",
                "online": False, "libraries": []}]}]}
    overview = {"available": True, "control": {"state": "running", "receiver_state": "login_required", "heartbeat_at": datetime.now(timezone.utc).isoformat()},
        "counts": {"completed_private": 1, "delivery_unknown": 1, "needs_review": 1, "queued": 12},
        "capabilities": {"start": False, "pause": True, "stop": True}, "binding": {
            "binding_id": "fixture-binding", "sender_display_name": "隔离账号 A", "receiver_display_name": "隔离助手 B",
            "receiver_profile_url": "https://www.douyin.com/user/fixture-assistant",
            "knowledge_base_id": 7, "knowledge_base_name": "学习与工作（隔离测试）"}}
    tasks = [task("fixture-private", "completed_private", "本机结果（隔离测试）", reply_state="ready",
                  stages={"intake": "accepted", "processing": "done", "reply": "not_public"}),
             task("fixture-unknown", "delivery_unknown", "结果不明（隔离测试）", reply_state="delivery_unknown"),
             task("fixture-preflight", "needs_review", "发布前核验（隔离测试）", reply_state="needs_review",
                  reason_code="preflight_verification_required", actions={"can_reconcile": True, "can_resume": True})]
    tasks += [task(f"fixture-queued-{n}", "queued", f"排队任务 {n}（隔离测试）") for n in range(12)]
    flags = {"deny_admin": False, "catalog_error": False, "assistant_unavailable": False, "registry_empty": False, "registry_failed": False,
             "configuration_error": False, "heartbeat_stale": False, "receiver_ready": False, "action_state": None, "missing_usage": False, "null_usage": False, "experience": False}

    def fulfill(route, status=200, value=None):
        route.fulfill(status=status, content_type="application/json; charset=utf-8", body=json.dumps(value, ensure_ascii=False))

    def handle(route):
        request = route.request
        parsed = urlparse(request.url)
        if parsed.netloc != "127.0.0.1:5187":
            blocked.append(request.url); route.abort(); return
        if not parsed.path.startswith("/api/"):
            route.continue_(); return
        path, method = parsed.path, request.method
        body = request.post_data_json if request.post_data else None
        requests.append({"path": path, "method": method, "body": body,
                         "has_admin_header": request.headers.get("authorization") == f"Bearer {TOKEN}"})
        if path.startswith("/api/assistant") and flags["assistant_unavailable"]:
            fulfill(route, 404, {"detail": "synthetic old backend"}); return
        if path.startswith("/api/assistant/registry"):
            suffix = path[len("/api/assistant/registry"):]
            if method == "GET" and not suffix:
                if flags["registry_failed"]:
                    fulfill(route, 409, {"detail": {"code": "registry_required_marker_missing", "message": "DO_NOT_ECHO_RAW_STATE"}}); return
                data = copy.deepcopy(registry)
                if flags["registry_empty"]:
                    data.update(configured=False, users=[])
                if flags["catalog_error"]:
                    data.update(local_catalog=[], catalog_error="fixture_catalog_unavailable")
                fulfill(route, value=data); return
            if suffix == "/admin-session":
                assert body == {"confirm_local_admin": True}
                fulfill(route, value={"admin_token": TOKEN, "expires_at_utc": (datetime.now(timezone.utc)+timedelta(minutes=15)).isoformat(), "admin_id": "fixture-admin"}); return
            assert request.headers.get("authorization") == f"Bearer {TOKEN}"
            if suffix == "/migrate-legacy":
                assert body.get("confirm") is True and type(body.get("allow_import")) is bool
                fulfill(route, 409, {"detail": {"code": "invalid_legacy_binding"}}); return
            if flags["deny_admin"]:
                fulfill(route, 401, {"detail": {"code": "admin_expired", "message": "PROVIDER_SECRET_BODY_MUST_NOT_RENDER"}}); return
            if suffix == "/users" and method == "POST":
                assert set(body) == {"label", "sender_profile_url", "receiver_profile_url"}
                registry["users"].append({**body, "user_id": "fixture-new-user", "active": True,
                                           "verification_status": "unverified", "devices": []})
            elif suffix == "/users/fixture-new-user/verify":
                assert body == {"confirm": True}
                registry["users"][-1]["verification_status"] = "verified"
            elif suffix.endswith("/libraries/8"):
                assert method == "PUT" and body == {"aliases": ["生活库", "灵感"], "can_query": True,
                    "can_import": True, "public_reply_allowed": False, "is_default": False}
                registry["users"][0]["devices"][0]["libraries"].append({**body, "knowledge_base_id": 8,
                    "name": "生活灵感（隔离测试）", "grant_id": "fixture-grant-8"})
            elif suffix == "/grants/fixture-grant-8":
                assert method == "DELETE" and body == {"confirm": True}
                registry["users"][0]["devices"][0]["libraries"][-1]["revoked"] = True
            else:
                raise AssertionError(f"Unexpected synthetic mutation: {method} {suffix}")
            fulfill(route, value={"success": True}); return
        if path == "/api/assistant/overview":
            value = copy.deepcopy(overview)
            if flags["registry_empty"]: value["binding"] = None
            if flags["receiver_ready"]: value["control"]["receiver_state"] = "ready"
            if flags["heartbeat_stale"]: value["control"]["heartbeat_at"] = "2026-10-20T00:00:00Z"
            if flags["configuration_error"]:
                value.update(binding=None, configuration={"state": "unavailable", "reason_code": "registry_required_marker_missing", "message": "PRIVATE_CONFIGURATION_DO_NOT_RENDER"})
                value["capabilities"]["start"] = False
            fulfill(route, value=value); return
        if path == "/api/assistant/pause":
            assert method == "POST" and body == {}
            fulfill(route, 503, {"detail": {"code": "assistant_unavailable", "message": "PRIVATE_UPSTREAM_FAILURE_DO_NOT_RENDER"}}); return
        if path == "/api/assistant/tasks":
            query = parse_qs(parsed.query)
            state, q = query.get("state", [""])[0], query.get("q", [""])[0]
            selected = [t for t in tasks if (not state or t["state"] == state) and (not q or q in t["video_title"])]
            p, size = int(query.get("page", ["1"])[0]), int(query.get("page_size", ["10"])[0])
            fulfill(route, value={"items": selected[(p-1)*size:p*size], "total": len(selected), "page": p, "page_size": size}); return
        if path.endswith("/reconcile"):
            assert method == "POST" and body == {}
            flags["action_state"] = "queued"
            fulfill(route, value={"success": True, "accepted": True, "action_id": "fixture-action"}); return
        if path.startswith("/api/assistant/tasks/"):
            data = copy.deepcopy(next(t for t in tasks if t["id"] == path.rsplit("/", 1)[-1]))
            data.update(details={"library_name": "学习与工作（隔离测试）", "reply_text": "仅本机可见的隔离测试回答。",
                "sources": [{"source_id": "资料1", "title": "测试来源", "chunk_id": "fixture:3", "url": "https://www.douyin.com/video/1234567890123456789?discard=yes"}],
                "steps": [{"kind": "tool", "created_at": 1792573200, "metadata": {"tool_name": "query_knowledge"}}]},
                timeline=[{"kind": "finished_execute", "at": NOW}], usage={"total_tokens": 123, "model_responses": 2, "elapsed_ms": 5432})
            if data["id"] == "fixture-unknown" and flags["action_state"]:
                data["latest_action"] = {"id": "fixture-action", "action": "reconcile", "state": flags["action_state"],
                    "reason_code": "reconciliation_unavailable" if flags["action_state"] == "failed" else None,
                    "message": "PRIVATE_ACTION_DO_NOT_RENDER", "created_at": NOW, "updated_at": NOW}
            if flags["missing_usage"]: data["usage"].pop("total_tokens")
            if flags["null_usage"]: data["usage"]["total_tokens"] = None
            fulfill(route, value=data); return
        if path == "/api/auth/douyin/login/status": fulfill(route, value={"status": "logged_out" if flags["experience"] else "logged_in", "message": "隔离测试"}); return
        if path == "/api/knowledge-bases": fulfill(route, value={"success": True, "items": registry["local_catalog"], "total": 2}); return
        if path == "/api/knowledge/stats": fulfill(route, value={"success": True, "video_cache": {"done": 5, "pending": 0, "failed": 0}}); return
        if path in {"/api/favorites/collections", "/api/chat/sessions"} or "/videos" in path:
            fulfill(route, value={"success": True, "items": [], "total": 0}); return
        fulfill(route, 404, {"code": "synthetic_endpoint_not_defined"})

    with sync_playwright() as p:
        browser = p.chromium.launch(channel="msedge", headless=True)
        # A failed read is unknown, never an empty queue or a first-use registry.
        first = browser.new_context(viewport={"width": 1440, "height": 1000}, locale="zh-CN")
        first.set_default_timeout(12000); first.route("**/*", handle)
        first_page = first.new_page()
        first_page.on("pageerror", lambda error: errors.append(str(error)))
        flags["assistant_unavailable"] = True
        first_page.goto(BASE + "/?panel=assistant", wait_until="networkidle")
        expect(first_page.locator(".assistant-empty")).to_contain_text("任务记录尚未读取")
        expect(first_page.locator(".assistant-pagination")).to_contain_text("共 — 项")
        expect(first_page.locator(".assistant-empty")).not_to_contain_text("还没有收到指令")
        first_page.get_by_role("button", name="查看演示数据", exact=True).click()
        expect(first_page.locator(".assistant-demo-banner")).to_be_visible()
        expect(first_page.locator(".assistant-task-row")).to_have_count(6)
        first_page.get_by_role("button", name="返回本机状态", exact=True).click()
        expect(first_page.locator(".assistant-empty")).to_contain_text("任务记录尚未读取")
        expect(first_page.locator(".assistant-task-row")).to_have_count(0)
        expect(first_page.locator(".assistant-demo-banner")).to_have_count(0)
        flags["assistant_unavailable"] = False
        flags["registry_empty"] = True
        first_page.get_by_role("button", name="账号与设备", exact=True).click()
        first_page.get_by_label("我确认由本人管理这台电脑上的助手账号和资料授权", exact=True).check()
        first_page.get_by_role("button", name="确认本机管理", exact=True).click()
        legacy = first_page.get_by_role("button", name="登记原有本机绑定（如果已有）", exact=True)
        expect(legacy).to_be_visible()
        expect(first_page.get_by_label("允许向原知识库保存视频", exact=True)).not_to_be_checked()
        legacy.click()
        expect(first_page.locator(".assistant-registry")).to_contain_text("未找到可用的原本机绑定")
        assert [r for r in requests if r["path"].endswith("migrate-legacy")][-1]["body"] == {"confirm": True, "allow_import": False}
        first_page.get_by_role("button", name="刷新账号设置", exact=True).click()
        expect(legacy).to_be_visible()
        first_page.get_by_label("允许向原知识库保存视频", exact=True).check()
        legacy.click()
        expect(first_page.locator(".assistant-registry")).to_contain_text("未找到可用的原本机绑定")
        assert [r for r in requests if r["path"].endswith("migrate-legacy")][-1]["body"] == {"confirm": True, "allow_import": True}
        first_page.get_by_role("button", name="刷新账号设置", exact=True).click()
        expect(legacy).to_be_visible()
        first_page.get_by_role("button", name="结束管理", exact=True).click()
        first_page.get_by_label("我确认由本人管理这台电脑上的助手账号和资料授权", exact=True).check()
        first_page.get_by_role("button", name="确认本机管理", exact=True).click()
        expect(first_page.get_by_label("允许向原知识库保存视频", exact=True)).not_to_be_checked()
        flags["registry_failed"] = True
        first_page.get_by_role("button", name="刷新账号设置", exact=True).click()
        expect(first_page.locator(".assistant-registry")).to_contain_text("授权保护标记缺失，已停止执行")
        expect(legacy).to_have_count(0)
        expect(first_page.get_by_role("button", name="添加账号", exact=True)).to_be_disabled()
        expect(first_page.locator("body")).not_to_contain_text("DO_NOT_ECHO_RAW_STATE")
        first_page.get_by_role("button", name="结束管理", exact=True).click()
        first_page.get_by_label("我确认由本人管理这台电脑上的助手账号和资料授权", exact=True).check()
        first_page.get_by_role("button", name="确认本机管理", exact=True).click()
        expect(legacy).to_have_count(0)
        expect(first_page.get_by_role("button", name="添加账号", exact=True)).to_be_disabled()
        expect(first_page.locator(".assistant-registry")).to_contain_text("授权保护标记缺失，已停止执行")
        first.close()
        flags["registry_failed"] = False; flags["registry_empty"] = False
        checks.extend(["failed_read_unknown_not_empty", "demo_exit_discards_all_synthetic_rows", "empty_registry_bootstrap_without_overview_binding", "migration_import_explicit_false_then_optin", "new_management_session_does_not_inherit_migration_optin", "missing_marker_not_first_use_bootstrap"])
        health = browser.new_context(viewport={"width": 1440, "height": 1000}, locale="zh-CN")
        health.set_default_timeout(12000); health.route("**/*", handle)
        health_page = health.new_page()
        health_page.on("pageerror", lambda error: errors.append(str(error)))
        flags.update(configuration_error=True, heartbeat_stale=True, receiver_ready=True)
        health_page.goto(BASE + "/?panel=assistant", wait_until="networkidle")
        expect(health_page.locator(".assistant-configuration-error")).to_contain_text("授权保护标记缺失")
        expect(health_page.locator(".assistant-binding-strip")).not_to_contain_text("尚未设置")
        expect(health_page.get_by_role("button", name="开启助手", exact=True)).to_be_disabled()
        expect(health_page.locator(".assistant-control-card")).to_contain_text("最近报告已过期")
        expect(health_page.locator(".assistant-control-card")).not_to_contain_text("正在收件")
        expect(health_page.locator(".assistant-control-card")).to_contain_text("不代表已经断线，不会自动重启")
        expect(health_page.locator("body")).not_to_contain_text("PRIVATE_CONFIGURATION_DO_NOT_RENDER")
        health_page.evaluate("""() => { const label=document.createElement('div'); label.textContent='隔离验证示例 · 所有账号和请求均为假数据'; label.style='position:fixed;bottom:10px;left:50%;transform:translateX(-50%);z-index:9999;padding:8px 14px;background:#fff3cf;border:1px solid #b88b35;border-radius:8px;color:#6e521e;font-size:12px;';document.body.append(label); }""")
        health_page.screenshot(path=str(OUT / "assistant-configuration-and-stale-report-isolated.png"))
        health.close()
        flags.update(configuration_error=False, heartbeat_stale=False, receiver_ready=False)
        experience = browser.new_context(viewport={"width": 1440, "height": 1000}, locale="zh-CN")
        experience.set_default_timeout(12000); experience.route("**/*", handle)
        experience_page = experience.new_page(); flags["experience"] = True
        experience_page.on("pageerror", lambda error: errors.append(str(error)))
        experience_page.goto(BASE, wait_until="networkidle")
        experience_page.get_by_role("button", name="先体验 AI 对话", exact=True).click()
        expect(experience_page.locator(".library-open").first).to_be_disabled()
        expect(experience_page.locator(".library-open").first).to_have_attribute("title", "体验模式尚未连接抖音；登录后可选择和管理知识库。")
        expect(experience_page.get_by_role("button", name="新对话", exact=True)).to_be_enabled()
        experience_page.evaluate("""() => { const label=document.createElement('div'); label.textContent='隔离验证示例 · 未连接真实抖音或模型'; label.style='position:fixed;bottom:10px;left:50%;transform:translateX(-50%);z-index:9999;padding:8px 14px;background:#fff3cf;border:1px solid #b88b35;border-radius:8px;color:#6e521e;font-size:12px;';document.body.append(label); }""")
        experience_page.screenshot(path=str(OUT / "assistant-experience-mode-isolated.png"))
        experience.close(); flags["experience"] = False
        checks.extend(["configuration_failure_not_first_use_and_start_disabled", "stale_runtime_report_not_refreshed_by_web_read", "experience_disabled_reason_is_not_chat_busy"])
        context = browser.new_context(viewport={"width": 1440, "height": 1000}, locale="zh-CN")
        context.set_default_timeout(12000)
        context.route("**/*", handle)
        page = context.new_page()
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.goto(BASE + "/?panel=assistant", wait_until="networkidle")
        expect(page.locator(".assistant-control-card")).to_contain_text("等待助手登录")
        expect(page.locator(".assistant-control-card")).not_to_contain_text("正在收件")
        page.get_by_role("button", name="暂停处理", exact=True).click()
        expect(page.locator(".assistant-notice.is-error")).to_have_attribute("role", "alert")
        expect(page.locator("body")).not_to_contain_text("PRIVATE_UPSTREAM_FAILURE_DO_NOT_RENDER")
        expect(page.locator(".assistant-task-row")).to_have_count(10)
        page.locator(".assistant-filter select").select_option("completed_private")
        expect(page.locator(".assistant-task-row")).to_have_count(1)
        expect(page.locator(".assistant-task-row")).to_contain_text("本机结果")
        page.locator(".assistant-filter select").select_option("")
        expect(page.locator(".assistant-task-row")).to_have_count(10)
        page.get_by_role("button", name="下一页", exact=True).click()
        expect(page.locator(".assistant-task-row")).to_have_count(5)
        page.get_by_role("button", name="上一页", exact=True).click()
        page.get_by_label("搜索任务", exact=True).fill("结果不明")
        page.get_by_role("button", name="搜索", exact=True).click()
        expect(page.locator(".assistant-task-row")).to_have_count(1)
        page.locator(".assistant-task-row").click()
        detail = page.locator(".assistant-detail-dialog")
        expect(detail.get_by_role("button", name="继续未开始任务")).to_have_count(0)
        detail.get_by_role("button", name="核查任务状态").click()
        expect(detail).to_contain_text("请求已登记")
        expect(detail.get_by_role("button", name="核查任务状态")).to_be_disabled()
        expect(detail).not_to_contain_text("已发送成功")
        flags["action_state"] = "failed"
        detail.get_by_role("button", name="刷新记录", exact=True).click()
        expect(detail.locator(".assistant-action-status")).to_contain_text("核查请求未完成")
        expect(detail.locator(".assistant-action-status")).to_contain_text("暂时无法核对原任务")
        expect(detail).not_to_contain_text("PRIVATE_ACTION_DO_NOT_RENDER")
        detail.locator(".assistant-detail-inner").evaluate("""node => { const label=document.createElement('p');label.textContent='隔离验证示例 · 假任务与假请求，没有真实发送';label.style='padding:9px 12px;background:#fff3cf;border:1px solid #d4b06a;border-radius:8px;color:#6e521e;font-size:12px;';node.prepend(label); }""")
        page.screenshot(path=str(OUT / "assistant-action-failure-isolated.png"))
        checks.append("accepted_async_action_failure_reaches_detail_without_false_task_success")
        page.get_by_role("button", name="关闭任务详情").click()
        page.get_by_label("搜索任务", exact=True).fill("本机结果")
        page.get_by_role("button", name="搜索", exact=True).click()
        expect(page.locator(".assistant-task-row")).to_have_count(1)
        page.locator(".assistant-task-row").click()
        expect(detail).to_contain_text("本机处理结果")
        expect(detail).to_contain_text("没有提交到抖音")
        expect(detail).to_contain_text("指令与回答用量")
        expect(detail).to_contain_text("不含声音识别、向量检索等服务，不代表全部费用")
        expect(detail.get_by_role("button", name="继续未开始任务")).to_have_count(0)
        detail.get_by_text("查看模型与工具步骤", exact=True).click()
        expect(detail).not_to_contain_text("1970")
        expect(detail.get_by_role("link", name="打开来源视频")).to_have_attribute("href", "https://www.douyin.com/video/1234567890123456789")
        flags["missing_usage"] = True
        detail.get_by_role("button", name="刷新记录", exact=True).click()
        expect(detail.locator(".assistant-usage")).to_contain_text("未记录")
        expect(detail.locator(".assistant-usage")).not_to_contain_text("0 tokens")
        flags["missing_usage"] = False; flags["null_usage"] = True
        detail.get_by_role("button", name="刷新记录", exact=True).click()
        expect(detail.locator(".assistant-usage")).to_contain_text("未记录")
        expect(detail.locator(".assistant-usage")).not_to_contain_text("0 tokens")
        flags["null_usage"] = False
        checks.append("missing_and_null_usage_remain_unrecorded")
        checks.extend(["receiver_readiness_not_process_only", "control_failure_alert_no_false_success", "server_filter_pagination_and_search", "unknown_only_reconcile_accepted_not_success", "private_result_no_publish_or_resume", "nested_details_unix_steps_and_safe_source_url"])
        page.get_by_role("button", name="关闭任务详情").click()
        page.get_by_label("搜索任务", exact=True).fill("发布前核验")
        page.get_by_role("button", name="搜索", exact=True).click()
        expect(page.locator(".assistant-task-row")).to_have_count(1)
        page.locator(".assistant-task-row").click()
        expect(detail).to_contain_text("这条回复尚未提交")
        expect(detail).to_contain_text("完成核验后，先开启助手，再核查原任务")
        expect(detail).not_to_contain_text("这项回复已经进入发送核对")
        expect(detail.get_by_role("button", name="继续未开始任务", exact=True)).to_be_visible()
        assert not any(r["path"].endswith("/resume") for r in requests)
        checks.extend(["preflight_verification_is_unsent_and_requires_explicit_action", "usage_excludes_other_service_costs"])
        page.get_by_role("button", name="关闭任务详情").click()
        page.get_by_role("button", name="账号与设备", exact=True).click()
        expect(page.locator(".assistant-registry")).to_contain_text("隔离测试账号")
        expect(page.get_by_role("button", name="添加账号", exact=True)).to_be_disabled()
        page.get_by_label("我确认由本人管理这台电脑上的助手账号和资料授权", exact=True).check()
        page.get_by_role("button", name="确认本机管理", exact=True).click()
        expect(page.get_by_role("button", name="结束管理", exact=True)).to_be_visible()
        page.get_by_role("button", name="添加账号", exact=True).click()
        form = page.locator(".assistant-form-dialog")
        form.get_by_label("账号名称", exact=True).fill("新增隔离账号")
        form.get_by_label("提问账号 A 的公开主页", exact=True).fill("https://douyin.com/user/new-fixture-person?strip=yes")
        form.get_by_role("button", name="保存设置", exact=True).click()
        expect(page.locator(".assistant-user-card").filter(has_text="新增隔离账号")).to_contain_text("待本人在本机确认")
        assert requests[-1]["method"] == "GET"
        page.locator(".assistant-user-card").filter(has_text="新增隔离账号").get_by_role("button", name="在本机确认账号", exact=True).click()
        expect(page.locator(".assistant-user-card").filter(has_text="新增隔离账号")).to_contain_text("已在本机确认")
        local = page.locator(".assistant-registry-device").filter(has_text="隔离本机")
        remote = page.locator(".assistant-registry-device").filter(has_text="隔离远端")
        expect(remote.get_by_role("button", name="允许使用另一个知识库")).to_have_count(0)
        local.get_by_role("button", name="允许使用另一个知识库", exact=True).click()
        public_checkbox = form.get_by_label("允许把此库的回答公开回复到原评论区", exact=True)
        expect(public_checkbox).not_to_be_checked()
        form.locator("select").select_option("8")
        form.get_by_label("这个库的别名", exact=True).fill("生活库，灵感，生活库")
        form.get_by_label("把当前视频导入此知识库", exact=True).check()
        form.get_by_role("button", name="保存设置", exact=True).click()
        grant = local.locator(".assistant-grant").filter(has_text="生活灵感")
        expect(grant).to_contain_text("不公开回复")
        grant.get_by_role("button", name="移除授权", exact=True).click()
        page.get_by_role("button", name="确认撤销", exact=True).click()
        expect(grant).to_have_count(0)
        saved = page.evaluate("({local:{...localStorage},session:{...sessionStorage},body:document.body.innerText,url:location.href})")
        assert TOKEN not in json.dumps(saved)
        flags["catalog_error"] = True
        page.get_by_role("button", name="刷新账号设置", exact=True).click()
        expect(page.locator(".assistant-registry")).to_contain_text("未使用空清单覆盖已有授权")
        expect(local.get_by_role("button", name="允许使用另一个知识库", exact=True)).to_be_disabled()
        flags["deny_admin"] = True
        page.get_by_role("button", name="添加账号", exact=True).click()
        form.get_by_label("账号名称", exact=True).fill("失效会话测试")
        form.get_by_label("提问账号 A 的公开主页", exact=True).fill("https://www.douyin.com/user/expired-fixture-person")
        form.get_by_role("button", name="保存设置", exact=True).click()
        expect(form).to_contain_text("本机管理确认已过期")
        expect(page.locator("body")).not_to_contain_text("PROVIDER_SECRET_BODY_MUST_NOT_RENDER")
        checks.extend(["new_user_unverified_no_inherited_public_fields", "explicit_verify_confirm_payload", "memory_only_admin_header", "new_grant_public_default_false", "deduplicated_aliases_exact_permission_payload", "remote_catalog_not_local", "revoke_confirm_json", "catalog_error_keeps_existing_grants", "expired_admin_cleared_and_error_redacted"])
        context.close()

        # Separate explicit demo context; screenshots cannot be mistaken for live UI.
        demo = browser.new_context(viewport={"width": 1440, "height": 1000}, locale="zh-CN")
        demo.set_default_timeout(12000)
        demo.route("**/*", handle)
        page = demo.new_page()
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.goto(BASE + "/?panel=assistant&assistantDemo=1", wait_until="networkidle")
        expect(page.locator(".assistant-demo-banner")).to_be_visible()
        expect(page.locator(".assistant-control-card")).to_contain_text("处理已暂停，新指令继续排队")
        expect(page.locator(".assistant-control-card")).to_contain_text("停止后不再收件")
        expect(page.locator(".assistant-control-card")).not_to_contain_text("暂停收件")
        checks.append("pause_processing_keeps_intake_until_stop")
        page.screenshot(path=str(OUT / "assistant-desktop-demo.png"))
        page.locator(".assistant-task-row").filter(has_text="工作区和暂存区有什么区别").click()
        expect(page.locator(".assistant-detail-dialog")).to_contain_text("演示任务")
        page.screenshot(path=str(OUT / "assistant-unknown-detail-demo.png"))
        page.get_by_role("button", name="关闭任务详情").click()
        page.locator(".assistant-task-row").filter(has_text="只在本机查看的一次问答").click()
        expect(page.locator(".assistant-detail-dialog")).to_contain_text("本机处理结果")
        page.screenshot(path=str(OUT / "assistant-private-result-demo.png"))
        page.get_by_role("button", name="关闭任务详情").click()
        page.get_by_role("button", name="账号与设备", exact=True).click()
        page.get_by_label("我确认由本人管理这台电脑上的助手账号和资料授权", exact=True).check()
        page.get_by_role("button", name="确认本机管理", exact=True).click()
        page.screenshot(path=str(OUT / "assistant-settings-demo.png"))
        page.get_by_role("button", name="允许使用另一个知识库", exact=True).click()
        expect(page.locator(".assistant-form-dialog")).to_contain_text("演示设置")
        expect(page.get_by_label("允许把此库的回答公开回复到原评论区", exact=True)).not_to_be_checked()
        page.locator(".assistant-form-dialog select").select_option("8")
        page.screenshot(path=str(OUT / "assistant-new-grant-public-off-demo.png"))
        page.get_by_role("button", name="关闭设置表单").click()
        page.get_by_role("button", name="任务队列", exact=True).click()
        page.set_viewport_size({"width": 390, "height": 844})
        page.locator(".assistant-panel").evaluate("node => {let p=node; while(p){p.scrollTop=0;p=p.parentElement}}")
        page.screenshot(path=str(OUT / "assistant-mobile-demo.png"))
        page.locator(".assistant-queue").scroll_into_view_if_needed()
        expect(page.locator(".assistant-demo-banner")).to_be_visible()
        page.screenshot(path=str(OUT / "assistant-mobile-queue-demo.png"))
        assert not page.evaluate("document.documentElement.scrollWidth > innerWidth")
        checks.append("explicit_demo_screenshots_desktop_mobile_no_horizontal_overflow")
        demo.close(); browser.close()
    assert not errors, errors
    assert not blocked, blocked
    report = {"verified_at_utc": datetime.now(timezone.utc).isoformat(), "synthetic_only": True,
              "live_accounts_models_comments_used": False, "checks": checks, "check_count": len(checks),
              "runtime_errors": errors, "blocked_external_requests": blocked,
              "synthetic_mutations": [{k: v for k, v in r.items() if k != "body"} for r in requests if r["method"] != "GET"],
              "screenshots": [path.name for path in sorted(OUT.glob("*.png"))]}
    (OUT / "preview-verification.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"checks": len(checks), "runtime_errors": len(errors), "output": str(OUT)}, ensure_ascii=True))


if __name__ == "__main__":
    run()
