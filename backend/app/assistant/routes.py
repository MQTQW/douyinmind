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
    "assistant_setup_required": "首次使用还需完成助手设置：填写助手 B 的公开抖音号与主页，并在账号与设备中确认设备与资料范围。",
    "assistant_receiver_configuration_required": "本机已有助手记录，但接收器配置缺失；请核对原有设置，不会自动覆盖。",
    "assistant_receiver_configuration_invalid": "接收器配置无效，请核对原有设置。",
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

SETUP_FAILURE_MESSAGES = {
    "invalid_receiver_public_number": "请输入 5–30 位数字的助手 B 公开抖音号。",
    "invalid_receiver_profile_url": "请填写固定的抖音公开主页链接（https://www.douyin.com/user/…），不能使用“我”的动态主页。",
    "receiver_configuration_locked": "已有助手 B 接收配置，当前不会覆盖；如需更换请先核对原有记录。",
    "assistant_receiver_configuration_required": "本机已有助手记录但接收器配置缺失，请核对原有设置；不会自动覆盖。",
    "assistant_receiver_configuration_invalid": "接收器配置无效，请核对原有设置后重试。",
    "setup_path_redirected": "助手数据目录存在重定向，为了安全没有写入配置。",
    "setup_state_dir_invalid": "助手数据目录不可用，没有写入配置。",
}


def public_action(row):
    """Publish only fixed status fields; never arbitrary exception text or tokens."""
    if row is None:
        return None
    state = row.get("state")
    action = row.get("action")
    if state not in {"queued", "processing", "done", "failed"} or action not in {"reconcile", "resume", "retry_query", "retry_unsent", "dismiss_unsent", "readback_unsent", "retry_after_readback"}:
        state, action = "failed", "reconcile"
        reason = "action_unavailable"
    else:
        reason = row.get("reason_code") if state == "failed" else None
    if state == "failed":
        reason = reason if reason in ACTION_FAILURE_MESSAGES else "action_unavailable"
        message = ACTION_FAILURE_MESSAGES[reason]
    else:
        message = {"queued": "请求已登记，等待助手处理。", "processing": "正在核对这项请求，原任务记录保留。",
                   "done": ("问答重试请求已处理，请以任务状态为准。" if action == "retry_query" else "核查请求已处理，请以任务状态为准。" if action == "reconcile" else "重新处理请求已处理，请以任务状态为准。" if action == "retry_unsent" else "只读核对已完成，请以任务状态为准。" if action == "readback_unsent" else "已记录为未提交。" if action == "dismiss_unsent" else "继续请求已处理，后续阶段仍以任务状态为准。")}[state]
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
    def __init__(self, queue, control=None, *, runtime=None, binding_provider=None, start_guard=None,
                 setup_provider=None, receiver_writer=None, login_flow=None, native_flow=None):
        self.queue = queue
        self.control = control or AssistantControl(queue)
        self.runtime = runtime
        self.binding_provider = binding_provider
        self.start_guard = start_guard
        self.setup_provider = setup_provider
        self.receiver_writer = receiver_writer
        self.login_flow = login_flow
        self.native_flow = native_flow

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
                if reason == "assistant_setup_required":
                    configuration = {"state": "setup_required", "reason_code": reason,
                                     "message": CONFIGURATION_MESSAGES[reason]}
                else:
                    reason = reason if reason in CONFIGURATION_MESSAGES else "assistant_configuration_unavailable"
                    configuration = {"state": "unavailable", "reason_code": reason, "message": CONFIGURATION_MESSAGES[reason]}
        current_control, counts = self.control.snapshot(), self.queue.counts()
        native_intake = None
        if current_control.get('native_intake') is not None:
            from .native_intake_visibility import project_native_intake
            try:
                native_intake = project_native_intake(current_control['native_intake'])
            except Exception:
                native_intake = {'available': False, 'message': '收件明细暂时无法读取，已有记录保留。'}
            current_control = {**current_control, 'native_intake': native_intake}
        setup = None
        if self.setup_provider is not None:
            try:
                setup = self.setup_provider()
            except Exception:
                setup = {"ready": False, "state": "unknown", "next_step": None, "steps": [],
                         "message": "首次设置状态暂时无法读取，请刷新后核对。"}
        start_allowed = configuration["state"] not in {"unavailable", "setup_required"}
        if setup is not None and setup.get("ready") is False:
            start_allowed = False
        return {"available": True, "control": current_control, "counts": counts, "binding": binding,
                "diagnostics": diagnostics(current_control, counts,
                    pending_rechecks=self.queue.notice_deferral_summary()['pending_rechecks']),
                "configuration": configuration, "setup": setup,
                "login_window": self.login_flow.status() if self.login_flow is not None else None,
                "native_provision": self.native_flow.status() if self.native_flow is not None else None,
                "native_channel": self._native_channel(),
                "intake_issues": self.queue.intake_issue_summary(),
                "native_intake": native_intake,
                "capabilities": {"start": start_allowed,
                    "pause": True, "stop": True, "multi_user": False,
                    "public_visibility": "b_view_only", "reconcile": True, "automatic_unknown_retry": False}}

    @staticmethod
    def _native_channel():
        """Local opt-in state; read-only facts, never credentials."""
        try:
            from .native_provision import status
            return status()
        except Exception:
            return {"configured": False, "enabled": False, "uid_present": False}

    def dismiss_intake_issue(self, issue_id):
        return self.queue.dismiss_intake_issue(issue_id)

    def explain_task(self, task_id):
        """Display-only DeepSeek failure triage; never changes task state."""
        task = self.queue.get_task(task_id)
        if task is None:
            raise QueueError("task_not_found")
        from . import semantic_triage
        if not semantic_triage.applicable(task):
            raise QueueError("triage_not_applicable")
        details = task.get("details") if isinstance(task.get("details"), dict) else {}
        cached = details.get("semantic_triage")
        if isinstance(cached, str) and cached.strip():
            return {"success": True, "text": cached, "cached": True}
        try:
            from .usage_budget import UsageBudget
            from .semantic_intent_model import default_semantic_model
            budget = UsageBudget.prepare(self.queue.path.parent / "usage-budget.sqlite3")
            result = semantic_triage.explain(task, model=default_semantic_model(), budget=budget)
        except Exception:
            result = {"text": semantic_triage.fallback_for(task), "fallback": True}
        self.queue.set_semantic_triage(task_id, result["text"])
        return {"success": True, "text": result["text"], "fallback": result.get("fallback", False), "cached": False}

    def propose_plan(self, task_id):
        """Display-only multi-step plan proposal; never executes or sends."""
        task = self.queue.get_task(task_id)
        if task is None:
            raise QueueError("task_not_found")
        comment = task.get("comment_text")
        if not isinstance(comment, str) or not comment.strip():
            envelope = task.get("envelope") if isinstance(task.get("envelope"), dict) else {}
            comment = envelope.get("comment_text")
        if not isinstance(comment, str) or not comment.strip():
            raise QueueError("plan_proposal_not_applicable")
        subject = {**task, "comment_text": comment}
        details = task.get("details") if isinstance(task.get("details"), dict) else {}
        cached = details.get("semantic_plan_proposal")
        if isinstance(cached, dict) and cached.get("summary"):
            return {"success": True, "proposal": cached, "cached": True}
        from . import semantic_plan_proposal
        try:
            from .usage_budget import UsageBudget
            from .semantic_intent_model import default_semantic_model
            budget = UsageBudget.prepare(self.queue.path.parent / "usage-budget.sqlite3")
            result = semantic_plan_proposal.propose(subject, model=default_semantic_model(), budget=budget)
        except Exception:
            result = semantic_plan_proposal.fallback_proposal()
        if not result.get("fallback"):
            self.queue.set_semantic_plan_proposal(task_id, result)
        return {"success": True, "proposal": result, "cached": False}

    def action(self, task_id, action):
        if action not in {"reconcile", "resume", "retry_query", "retry_unsent", "dismiss_unsent", "readback_unsent", "retry_after_readback"}:
            raise QueueError("invalid_action")
        task = self.queue.get_task(task_id)
        if task is None:
            raise QueueError("task_not_found")
        if self.runtime is not None:
            if action == "retry_query":
                task = self.runtime.retry_query_task(task_id)
            elif action == "retry_unsent":
                task = self.runtime.retry_unsent_task(task_id)
            elif action == "dismiss_unsent":
                task = self.runtime.dismiss_unsent_task(task_id)
            elif action == "readback_unsent":
                task = self.runtime.readback_unsent_task(task_id)
            elif action == "retry_after_readback":
                task = self.runtime.retry_after_readback_task(task_id)
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
                    "retry_unsent_not_safe": "这条任务不能确认未发出，已保留原记录，不能重复发送。",
                    "retry_unsent_requires_running": "请先开启助手，再重新处理这条回复。",
                    "dismiss_unsent_not_safe": "只有已确认未发出的任务才能标记为未提交。",
                    "dismiss_unsent_requires_running": "请先开启助手，再处理这条未提交记录。",
                    "readback_unsent_not_safe": "只有已确认未发出的任务才能做只读核对。",
                    "readback_requires_running": "请先开启助手，再做只读核对。",
                    "readback_unavailable": "只读核对暂时不可用；原任务记录已保留。",
                    "readback_result_invalid": "只读核对结果无效；原任务记录已保留。",
                    "retry_after_readback_not_safe": "只读核对未确认这条回复未发出，不能重新发表。",
                    "retry_after_readback_requires_readback": "请先做一次只读核对，确认原评论线程没有这条回复。",
                    "retry_after_readback_requires_running": "请先开启助手，再重新发表这条回复。",
                    "retry_after_readback_unavailable": "重新发表暂时不可用；原任务记录已保留。",
                    "retry_after_readback_exhausted": "这条任务已经重新发表过一次；如仍被平台验证拦截，请标记为未提交并结束，再重新发一条评论指令。",
                    "task_busy": "任务仍在处理中，请稍后核对。", "launch_pending_requires_review": "上次启动结果待核对，请勿重复启动。",
                    "assistant_setup_required": "请先在「账号与设备」完成首次设置，再开启助手。",
                    "assistant_receiver_configuration_required": "本机已有助手记录但接收器配置缺失，请核对原有设置。",
                    "assistant_receiver_configuration_invalid": "接收器配置无效，请核对原有设置。",
                    "verified_local_binding_required": "尚无可用的本机授权，请确认账号、设备和知识库范围。",
                    "native_intake_unavailable": "接口收件功能暂不可用；原网页收件不受影响。",
                    "invalid_intake_issue_id": "这条历史提醒编号无效，已保留原记录。",
                    "triage_not_applicable": "这条任务还在处理中或不需要失败解释。",
                    "plan_proposal_not_applicable": "这条任务没有可规划的原评论内容。",
                    "invalid_semantic_triage": "解释文本无效，原任务记录已保留。",
                    "intake_issue_or_task_missing": "这条历史提醒不存在，可能已处理。"}
        return HTTPException(status, detail={"code": name, "message": messages.get(name, "当前操作未完成，状态记录已保留。")})
    return HTTPException(503, detail={"code": "assistant_unavailable", "message": "助手状态暂时无法读取，请稍后重试。"})


class EmptyBody(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ControlBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: str


class ReceiverSetupBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    receiver_public_number: str
    receiver_profile_url: str


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


@router.get("/setup")
def setup(service=Depends(get_service)):
    """First-run progress. Reading it never starts the assistant or intake."""
    if service.setup_provider is None:
        raise HTTPException(503, detail={"code": "assistant_unavailable", "message": "首次设置状态暂时无法读取，请稍后重试。"})
    try:
        return service.setup_provider()
    except Exception as error:
        raise failure(error) from None


@router.post("/setup/receiver")
def save_receiver(body: ReceiverSetupBody, service=Depends(get_service)):
    """Save only the public assistant identity for a fresh installation."""
    if service.receiver_writer is None or service.setup_provider is None:
        raise HTTPException(503, detail={"code": "assistant_unavailable", "message": "助手设置服务尚未就绪，请稍后重试。"})
    try:
        service.receiver_writer(body.receiver_public_number, body.receiver_profile_url)
        return {"success": True, "setup": service.setup_provider()}
    except QueueError as error:
        code = str(error)
        message = SETUP_FAILURE_MESSAGES.get(code, "助手 B 的公开信息未能保存，请核对后重试。")
        raise HTTPException(400 if code.startswith("invalid_") else 409,
                            detail={"code": code, "message": message}) from None
    except Exception as error:
        raise failure(error) from None


@router.post("/login-window")
def login_window(body: EmptyBody, service=Depends(get_service)):
    """Pause the headless owner, open the visible login window, then restart it."""
    flow = service.login_flow
    if flow is None:
        raise HTTPException(503, detail={"code": "assistant_unavailable",
                                         "message": "可见登录窗口尚未就绪，请稍后重试。"})
    if not flow.start():
        raise HTTPException(409, detail={"code": "login_window_busy",
                                         "message": "登录窗口正在处理中，请稍候；完成后助手会自动开启。"})
    return {"success": True, "login_window": flow.status(),
            "message": "正在停止助手并打开可见登录窗口；登录完成后助手会自动重新开启。"}


@router.post("/native-intake/enable")
def native_intake_enable(body: EmptyBody, service=Depends(get_service)):
    """Enable the opt-in native (API) intake channel for the configured B."""
    flow = service.native_flow
    if flow is None:
        raise HTTPException(503, detail={"code": "native_intake_unavailable",
                                         "message": "接口收件功能暂不可用；原网页收件不受影响。"})
    if not flow.start_enable():
        raise HTTPException(409, detail={"code": "native_intake_busy",
                                         "message": "接口收件正在切换中，请稍候；完成后助手会自动重新开启。"})
    return {"success": True, "native_provision": flow.status(),
            "message": "正在核对 B 会话并启用接口收件；助手会短暂重启，完成后自动开启。"}


@router.post("/native-intake/disable")
def native_intake_disable(body: EmptyBody, service=Depends(get_service)):
    """Close the native channel and fall back to the normal-page intake lane."""
    flow = service.native_flow
    if flow is None:
        raise HTTPException(503, detail={"code": "native_intake_unavailable",
                                         "message": "接口收件功能暂不可用；原网页收件不受影响。"})
    if not flow.start_disable():
        raise HTTPException(409, detail={"code": "native_intake_busy",
                                         "message": "接口收件正在切换中，请稍候；完成后助手会自动重新开启。"})
    return {"success": True, "native_provision": flow.status(),
            "message": "正在关闭接口收件并恢复网页收件；助手会短暂重启，完成后自动开启。"}


@router.post("/intake-issues/{issue_id}/dismiss")
def dismiss_intake_issue(issue_id: str, body: EmptyBody, service=Depends(get_service)):
    """Acknowledged historical notice; kept, never executed or treated as resolved."""
    try:
        return {"success": True, "issue": service.dismiss_intake_issue(issue_id)}
    except Exception as error:
        raise failure(error) from None


@router.post("/tasks/{task_id}/plan-proposal")
def plan_proposal(task_id: str, body: EmptyBody, service=Depends(get_service)):
    """Propose a fixed multi-step plan for the operator; no step is executed."""
    try:
        return service.propose_plan(task_id)
    except Exception as error:
        raise failure(error) from None


@router.post("/tasks/{task_id}/triage")
def triage(task_id: str, body: EmptyBody, service=Depends(get_service)):
    """Explain an already-stopped task to the user; no state or send change."""
    try:
        return service.explain_task(task_id)
    except Exception as error:
        raise failure(error) from None


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


@router.post("/tasks/{task_id}/retry-unsent")
def retry_unsent(task_id: str, body: EmptyBody, service=Depends(get_service)):
    """Retry a reply that was never sent; never offered for unknown deliveries."""
    try:
        return service.action(task_id, "retry_unsent")
    except Exception as error:
        raise failure(error) from None


@router.post("/tasks/{task_id}/retry-after-readback")
def retry_after_readback(task_id: str, body: EmptyBody, service=Depends(get_service)):
    """Reopen a never-sent draft once after a fresh read-only B-view readback."""
    try:
        return service.action(task_id, "retry_after_readback")
    except Exception as error:
        raise failure(error) from None


@router.post("/tasks/{task_id}/readback-unsent")
def readback_unsent(task_id: str, body: EmptyBody, service=Depends(get_service)):
    """Read-only B-view check for a never-sent reply; never sends or resets."""
    try:
        return service.action(task_id, "readback_unsent")
    except Exception as error:
        raise failure(error) from None


@router.post("/tasks/{task_id}/dismiss-unsent")
def dismiss_unsent(task_id: str, body: EmptyBody, service=Depends(get_service)):
    """Close a confirmed-unsent task without sending or re-running anything."""
    try:
        return service.action(task_id, "dismiss_unsent")
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
