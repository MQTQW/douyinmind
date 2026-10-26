"""Local product API. Mount this router from the existing backend explicitly."""
from __future__ import annotations

import ipaddress
from pathlib import Path
import threading

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict

from .control import AssistantControl
from .queue import AssistantQueue, QueueError, utc
from .runtime_diagnostics import diagnostics


DEFAULT_STATE = Path(__file__).resolve().parents[1] / "storage" / "assistant"
ALLOWED_ORIGINS = frozenset({"http://127.0.0.1:5173", "http://localhost:5173", "http://127.0.0.1:8000",
    "http://localhost:8000", "http://[::1]:5173", "http://[::1]:8000"})

CONFIGURATION_MESSAGES = {
    "registry_required_but_missing": "原授权登记库不可用，已停止执行。请恢复原授权状态，不会回退旧绑定。",
    "registry_missing_after_initialization": "原授权登记库缺失，已停止执行。请恢复原授权状态。",
    "registry_required_marker_missing": "授权保护标记缺失，已停止执行。请恢复原授权状态。",
    "registry_required_marker_write_failed": "授权保护标记未可靠保存，本次设置尚未完成。",
    "invalid_registry_required_marker": "授权保护标记无效，已停止执行。请核对原授权状态。",
    "unsafe_registry_required_marker": "授权保护标记路径异常，已停止执行。请核对原授权状态。",
    "registry_missing_or_empty": "原授权登记库缺失或为空，已停止执行，不会自动重建。",
    "foreign_registry_database": "授权登记库格式不符，已停止执行。请核对原授权状态。",
    "assistant_receiver_not_configured": "助手收件配置尚不可用，请核对原有助手设置。",
    "assistant_configuration_unavailable": "助手设置暂时无法读取，已保留原记录。请核对原有授权和助手设置。",
}

ACTION_FAILURE_MESSAGES = {
    "task_not_found": "原任务已不存在。",
    "task_busy": "原任务仍在处理中，请稍后核对。",
    "reconciler_unavailable": "助手运行时尚未就绪，暂不能核查。",
    "reconciliation_unavailable": "暂时无法核对原任务，请检查助手运行状态。",
    "operator_resume_unavailable": "暂时不能继续原任务，请核对助手窗口和本人核验。",
    "resume_not_safe": "当前证据不足，不能继续原任务。",
    "query_retry_not_safe": "这项问答不满足单次重试条件，原失败记录保留。",
    "query_retry_already_used": "单次问答重试已经登记，请核查原任务。",
    "query_retry_unavailable": "当前无法登记问答重试，请核对助手运行状态。",
    "query_retry_requires_running": "请先开启助手，再核查并重试本条问答。",
    "query_retry_scope_changed": "账号或知识库授权已变化，本次不重试。",
    "query_retry_unconfirmed": "上次重试登记尚未核清，已保留记录，不会再次调用模型。",
    "query_retry_interrupted": "重试处理中断，原记录保留，需要先核查结果。",
    "query_retry_action_not_current": "这次重试请求已经失效，请核查原任务。",
    "resume_requires_operator_adapter": "原任务需要本人确认，当前不能继续。",
    "interrupted_action": "上次请求处理中断，结果未确认。原任务记录保留，不会自动重试。",
    "action_unavailable": "本次请求未完成，原任务记录已保留。",
}


def public_action(row):
    """Publish only fixed status fields; never arbitrary exception text or tokens."""
    if row is None:
        return None
    state = row.get("state")
    action = row.get("action")
    if state not in {"queued", "processing", "done", "failed"} or action not in {"reconcile", "resume", "retry_query"}:
        state, action = "failed", "reconcile"
        reason = "action_unavailable"
    else:
        reason = row.get("reason_code") if state == "failed" else None
    if state == "failed":
        reason = reason if reason in ACTION_FAILURE_MESSAGES else "action_unavailable"
        message = ACTION_FAILURE_MESSAGES[reason]
    else:
        message = {"queued": "请求已登记，等待助手处理。", "processing": "正在核对这项请求，原任务记录保留。",
                   "done": "问答重试请求已处理，请以任务状态为准。" if action == "retry_query" else "核查请求已处理，请以任务状态为准。" if action == "reconcile" else "继续请求已处理，后续阶段仍以任务状态为准。"}[state]
    def timestamp(value):
        return value if isinstance(value, str) else utc(value) if type(value) in (int, float) else None
    return {"id": row.get("id"), "action": action, "state": state, "reason_code": reason,
            "message": message, "created_at": timestamp(row.get("created_at")), "updated_at": timestamp(row.get("updated_at"))}


def local_request(request: Request):
    try:
        address = ipaddress.ip_address(request.client.host)
        local = address.is_loopback or bool(getattr(address, "ipv4_mapped", None) and address.ipv4_mapped.is_loopback)
    except (ValueError, AttributeError):
        local = False
    if not local or request.url.hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise HTTPException(403, detail={"code": "loopback_required", "message": "请从本机页面操作助手。"})
    origin = request.headers.get("origin")
    if origin is not None and origin not in ALLOWED_ORIGINS:
        raise HTTPException(403, detail={"code": "local_origin_required", "message": "这个页面来源不允许控制助手。"})
    if request.method in {"POST", "PUT", "PATCH", "DELETE"} and request.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "application/json":
        raise HTTPException(415, detail={"code": "json_required", "message": "请使用本机助手页面提交操作。"})


class AssistantAPI:
    def __init__(self, queue, control=None, *, runtime=None, binding_provider=None, start_guard=None):
        self.queue = queue
        self.control = control or AssistantControl(queue)
        self.runtime = runtime
        self.binding_provider = binding_provider
        self.start_guard = start_guard

    def overview(self):
        binding = None
        configuration = {"state": "not_configured", "reason_code": None, "message": None}
        if self.binding_provider is not None:
            try:
                raw = self.binding_provider()
                if isinstance(raw, dict):
                    allowed = {"sender_profile_url", "sender_public_number", "sender_display_name", "receiver_profile_url",
                        "receiver_public_number", "receiver_display_name", "knowledge_base_id", "knowledge_base_name",
                        "public_reply_allowed", "allow_import", "binding_id", "instance_id"}
                    binding = {key: value for key, value in raw.items() if key in allowed and type(value) in (str, int, bool, type(None))}
                    if binding:
                        configuration["state"] = "ready"
            except Exception as error:
                reason = str(error)
                reason = reason if reason in CONFIGURATION_MESSAGES else "assistant_configuration_unavailable"
                configuration = {"state": "unavailable", "reason_code": reason, "message": CONFIGURATION_MESSAGES[reason]}
        current_control, counts = self.control.snapshot(), self.queue.counts()
        return {"available": True, "control": current_control, "counts": counts, "binding": binding,
                "diagnostics": diagnostics(current_control, counts,
                    pending_rechecks=self.queue.notice_deferral_summary()['pending_rechecks']),
                "configuration": configuration,
                "intake_issues": self.queue.intake_issue_summary(),
                "capabilities": {"start": configuration["state"] != "unavailable", "pause": True, "stop": True, "multi_user": False,
                    "public_visibility": "b_view_only", "reconcile": True, "automatic_unknown_retry": False}}

    def action(self, task_id, action):
        if action not in {"reconcile", "resume", "retry_query"}:
            raise QueueError("invalid_action")
        task = self.queue.get_task(task_id)
        if task is None:
            raise QueueError("task_not_found")
        if self.runtime is not None:
            if action == "retry_query":
                task = self.runtime.retry_query_task(task_id)
            else:
                task = self.runtime.reconcile_task(task_id) if action == "reconcile" else self.runtime.resume_task(task_id)
            return {"success": True, "accepted": False, "task": self.queue.public_task(task, detail=True)}
        command = self.queue.request_action(task_id, action)
        return {"success": True, "accepted": True, "action_id": command["id"],
                "message": "请求已登记；助手运行时会核对原有记录，不会直接重复提交。", "task": self.queue.public_task(task, detail=True)}


_service = None
_service_lock = threading.Lock()


def configure_service(service):
    """Root integration may inject a fixed, trusted local service before use."""
    global _service
    with _service_lock:
        _service = service


def get_service():
    global _service
    with _service_lock:
        if _service is None:
            from .integration import build_web_service
            _service = build_web_service()
        return _service


def failure(error):
    if isinstance(error, QueueError):
        name = str(error)
        status = 404 if name == "task_not_found" else 409
        messages = {"task_not_found": "这个任务不存在。", "resume_not_safe": "当前证据不足，不能恢复或重复发送。",
                    "query_retry_not_safe": "当前证据不足，不能重试这项问答。",
                    "query_retry_already_used": "单次问答重试已登记，请核查原任务。",
                    "task_busy": "任务仍在处理中，请稍后核对。", "launch_pending_requires_review": "上次启动结果待核对，请勿重复启动。"}
        return HTTPException(status, detail={"code": name, "message": messages.get(name, "当前操作未完成，状态记录已保留。")})
    return HTTPException(503, detail={"code": "assistant_unavailable", "message": "助手状态暂时无法读取，请稍后重试。"})


class EmptyBody(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ControlBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: str


router = APIRouter(prefix="/assistant", tags=["抖音助手"], dependencies=[Depends(local_request)])


@router.get("/overview")
@router.get("/status", include_in_schema=False)
def overview(service=Depends(get_service)):
    try:
        return service.overview()
    except Exception as error:
        raise failure(error) from None


@router.get("/tasks")
def tasks(state: str | None = None, q: str | None = None, page: int = Query(1, ge=1),
          page_size: int = Query(20, ge=1, le=100), service=Depends(get_service)):
    try:
        return service.queue.list_tasks(state=state, q=q, page=page, page_size=page_size)
    except Exception as error:
        raise failure(error) from None


@router.get("/tasks/{task_id}")
def task_detail(task_id: str, service=Depends(get_service)):
    try:
        task = service.queue.get_task(task_id)
        if task is None:
            raise QueueError("task_not_found")
        value = service.queue.public_task(task, detail=True)
        value["latest_action"] = public_action(service.queue.latest_action(task_id))
        return value
    except Exception as error:
        raise failure(error) from None


def perform_control(action, service):
    try:
        if action not in {"start", "pause", "stop"}:
            raise QueueError("invalid_control_action")
        if action == "start" and service.start_guard is not None and service.start_guard() is False:
            raise QueueError("assistant_not_configured")
        getattr(service.control, action)()
        return {"success": True, "overview": service.overview()}
    except Exception as error:
        raise failure(error) from None


@router.post("/start")
def start(body: EmptyBody, service=Depends(get_service)):
    return perform_control("start", service)


@router.post("/pause")
def pause(body: EmptyBody, service=Depends(get_service)):
    return perform_control("pause", service)


@router.post("/stop")
def stop(body: EmptyBody, service=Depends(get_service)):
    return perform_control("stop", service)


@router.post("/control", include_in_schema=False)
def control(body: ControlBody, service=Depends(get_service)):
    return perform_control(body.action, service)


@router.post("/tasks/{task_id}/reconcile")
def reconcile(task_id: str, body: EmptyBody, service=Depends(get_service)):
    try:
        return service.action(task_id, "reconcile")
    except Exception as error:
        raise failure(error) from None


@router.post("/tasks/{task_id}/resume")
def resume(task_id: str, body: EmptyBody, service=Depends(get_service)):
    try:
        return service.action(task_id, "resume")
    except Exception as error:
        raise failure(error) from None


@router.post("/tasks/{task_id}/retry-query")
def retry_query(task_id: str, body: EmptyBody, service=Depends(get_service)):
    try:
        return service.action(task_id, "retry_query")
    except QueueError as error:
        # This new recovery endpoint exposes only its fixed public reasons.
        # Never echo an arbitrary low-level exception or private proof string.
        reason = str(error)
        allowed = {"task_not_found", "task_busy", "query_retry_not_safe", "query_retry_already_used",
            "query_retry_unavailable", "query_retry_requires_running", "query_retry_scope_changed",
            "query_retry_unconfirmed", "query_retry_interrupted", "query_retry_action_not_current"}
        if reason not in allowed:
            reason = "query_retry_unavailable"
        raise HTTPException(404 if reason == "task_not_found" else 409,
            detail={"code": reason, "message": ACTION_FAILURE_MESSAGES[reason]}) from None
    except Exception as error:
        raise failure(error) from None
