"""Scoped import tools: the existing backend owns all media/model/index work."""
from __future__ import annotations

import json
import re
import time
from urllib.error import HTTPError
from urllib.request import Request, ProxyHandler, HTTPRedirectHandler, build_opener

from .contracts import Binding


def is_save_command(text: str, library_name: str) -> bool:
    """Small explicit command grammar; questions/negation/extra targets fail closed."""
    if not isinstance(text, str) or not isinstance(library_name, str) or not library_name.strip():
        return False
    if re.search(r"https?://|v\.douyin\.com|www\.", text, re.I):
        return False
    text = re.sub(r"^\s*@\S+\s+", "", text, count=1).strip()
    name = re.escape(library_name)
    quoted = rf'(?:“{name}”|"{name}"|「{name}」|‘{name}’)'
    command = rf'(?:请)?(?:帮我)?(?:把|将)(?:这个|当前|本条)视频(?:保存到|加入|导入到|上传到){quoted}(?:知识库)?'
    marker = r'(?:[，。；;\s]*测试编号\s*[A-Za-z0-9_-]{1,80})?'
    return re.fullmatch(command + marker + r'[。！!\s]*', text) is not None


def import_status_proposal(job, library_name):
    """Render only a reconciled receipt; this helper cannot submit or publish."""
    state = job["state"]
    if state == "succeeded":
        text = f"这个视频已加入“{library_name}”知识库，文字稿和检索资料已就绪，现在可以根据它提问。"
    elif state == "queued":
        text = "还没有确认提交这个视频的入库任务；请明确指定当前绑定知识库后再发送保存指令。"
    elif state in {"accepted", "processing"}:
        text = "这个视频的入库任务仍在处理中，尚未确认完成。可以在本视频下再问我入库进度。"
    elif state in {"failed", "failed_no_submit"}:
        text = "这次视频入库没有完成，失败记录已保留，请先核对本机处理状态。"
    else:
        text = "这次入库提交结果暂未确认，记录已保留，暂不重复提交，请先核对本机处理状态。"
    return {"kind": "import_status", "text": text, "source_ids": [], "sources": [],
            "import_job_id": job["job_id"], "import_state": state}


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class BackendImportClient:
    """Pinned loopback JSON API, no proxies, redirects, credentials or POST retries."""
    ROOT = "http://127.0.0.1:8000/api/mention-ingestion"

    def __init__(self):
        self.opener = build_opener(ProxyHandler({}), _NoRedirect())

    def _request(self, path, body=None):
        data = json.dumps(body, ensure_ascii=False).encode() if body is not None else None
        request = Request(self.ROOT + path, data=data,
            headers={"Content-Type": "application/json"}, method="POST" if data is not None else "GET")
        try:
            with self.opener.open(request, timeout=8) as response:
                raw = response.read(20001)
        except HTTPError as error:
            if body is None and error.code == 404:
                return None
            raise RuntimeError("backend_import_request_failed") from None
        if len(raw) > 20000:
            raise ValueError("backend_response_too_large")
        value = json.loads(raw)
        if not isinstance(value, dict):
            raise ValueError("invalid_backend_import_response")
        return value

    def submit(self, body):
        return self._request("", body)

    def status(self, request_id):
        if not isinstance(request_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{16,128}", request_id):
            raise ValueError("invalid_import_receipt")
        return self._request("/" + request_id)


class ImportTools:
    MAX_WAIT_SECONDS = 600

    def __init__(self, store, binding: Binding, task_id, claim_token, envelope, library_name,
                 *, should_stop=None, client=None, wait_seconds=None, sleep=None, video_title=None,
                 explicit_save_check=None, scope_authorizer=None):
        self.store, self.binding, self.task_id, self.claim_token = store, binding, task_id, claim_token
        self.envelope, self.library_name = envelope, library_name
        self.video_title = video_title.strip()[:512] if isinstance(video_title, str) else ""
        self.should_stop = should_stop or (lambda: False)
        self.client = client or BackendImportClient()
        self.wait_seconds = self.MAX_WAIT_SECONDS if wait_seconds is None else wait_seconds
        if type(self.wait_seconds) not in (int, float) or not 0 <= self.wait_seconds <= self.MAX_WAIT_SECONDS:
            raise ValueError("invalid_import_wait_budget")
        self.sleep = sleep or time.sleep
        self.observed = False
        self.confirmed_succeeded = False
        self.wait_deadline = None
        if explicit_save_check is not None and not callable(explicit_save_check):
            raise ValueError("invalid_explicit_save_check")
        self.explicit_save_check = explicit_save_check
        if scope_authorizer is not None and not callable(scope_authorizer):
            raise ValueError("invalid_scope_authorizer")
        self.scope_authorizer = scope_authorizer

    def _active(self):
        current = self.store.get_binding(self.binding.binding_id)
        if not current or Binding.from_dict(current) != self.binding or not self.binding.active:
            raise ValueError("binding_changed")
        if self.scope_authorizer is not None and self.scope_authorizer(self.binding) is not True:
            raise ValueError("binding_changed")

    def _job(self):
        self._active()
        return self.store.ensure_ingestion(self.task_id, self.claim_token)

    def _observe(self, response, job):
        if response is None:
            return job
        expected = {"request_id": job["job_id"], "binding_id": self.binding.binding_id,
                    "instance_id": self.binding.instance_id, "video_id": self.envelope["video_id"],
                    "knowledge_base_id": self.binding.knowledge_base_id}
        if (not isinstance(response, dict) or any(type(response.get(key)) is not type(value)
                or response.get(key) != value for key, value in expected.items())):
            raise ValueError("import_response_scope_mismatch")
        state = response.get("state")
        if state not in {"pending", "accepted", "processing", "succeeded", "failed", "unknown"}:
            raise ValueError("invalid_import_state")
        # The store performs strict boolean/integer and three-part success checks.
        observed = self.store.observe_ingestion(job["job_id"], {
            "backend_request_id": job["job_id"], "binding_id": self.binding.binding_id,
            "instance_id": self.binding.instance_id, "knowledge_base_id": self.binding.knowledge_base_id,
            "video_id": self.envelope["video_id"], "backend_state": state,
            "membership_exists": response.get("membership_exists"), "cache_status": response.get("cache_status"),
            "indexed_content_chunks": response.get("indexed_content_chunks")})
        self.confirmed_succeeded = state == "succeeded" and observed["state"] == "succeeded"
        return observed

    def _refresh(self, job):
        self.confirmed_succeeded = False
        try:
            return self._observe(self.client.status(job["job_id"]), job)
        except Exception:
            # A GET failure is not proof that a prior POST never executed.
            return self.store.get_ingestion(job["job_id"])

    def _wait(self, job):
        if self.wait_deadline is None:
            self.wait_deadline = time.monotonic() + self.wait_seconds
        deadline = self.wait_deadline
        while job["state"] in {"reserved", "accepted", "processing", "submission_unknown"}:
            if self.should_stop() or time.monotonic() >= deadline:
                break
            self._active()
            job = self._refresh(job)
            if job["state"] in {"succeeded", "failed", "failed_no_submit", "needs_review"}:
                break
            self.sleep(min(2, max(0, deadline - time.monotonic())))
        self.observed = True
        return self._result(job)

    def save_current_video(self, knowledge_base_id):
        if type(knowledge_base_id) is not int or knowledge_base_id != self.binding.knowledge_base_id:
            raise ValueError("library_not_allowed")
        explicit = (self.explicit_save_check(self.envelope, self.binding) is True
                    if self.explicit_save_check is not None else
                    is_save_command(self.envelope["comment_text"], self.library_name))
        if self.envelope["source_mode"] != "live_ui" or not explicit:
            raise ValueError("explicit_save_command_required")
        if self.should_stop():
            raise ValueError("import_run_stopped")
        job = self._job()
        reservation = self.store.reserve_ingestion(self.task_id, self.claim_token)
        if reservation is not None:
            job = reservation
            try:
                self._active()
                if (self.explicit_save_check is not None
                        and self.explicit_save_check(self.envelope, self.binding) is not True):
                    self.store.finish_ingestion_submission(job["job_id"], job["dispatch_token"], "failed_no_submit",
                        {"backend_request_id": job["job_id"], "reason_code": "save_authorization_changed"})
                    self.observed = True
                    return self._result(self.store.get_ingestion(job["job_id"]))
                if self.should_stop():
                    self.store.finish_ingestion_submission(job["job_id"], job["dispatch_token"], "failed_no_submit",
                        {"backend_request_id": job["job_id"], "reason_code": "run_stopped_before_dispatch"})
                    return self._result(self.store.get_ingestion(job["job_id"]))
                # The body never contains a model-supplied URL, title or target.
                body = {"request_id": job["job_id"],
                    "binding_id": self.binding.binding_id, "instance_id": self.binding.instance_id,
                    "video_id": self.envelope["video_id"], "knowledge_base_id": self.binding.knowledge_base_id}
                if self.video_title:
                    body["title"] = self.video_title
                response = self.client.submit(body)
            except Exception:
                self.store.finish_ingestion_submission(job["job_id"], job["dispatch_token"], "submission_unknown",
                    {"backend_request_id": job["job_id"], "reason_code": "backend_submit_result_unknown"})
                job = self.store.get_ingestion(job["job_id"])
            else:
                self.store.finish_ingestion_submission(job["job_id"], job["dispatch_token"], "accepted",
                    {"backend_request_id": job["job_id"], "http_status": 200})
                job = self._observe(response, self.store.get_ingestion(job["job_id"]))
        else:
            # Reusing a receipt must re-check its current library/index state.
            job = self._refresh(job)
        return self._wait(job)

    def get_import_status(self):
        job = self._job()
        if job["state"] != "queued":
            job = self._refresh(job)
        self.observed = True
        return self._result(job)

    def _result(self, job):
        # A historical success remains durable, but a failed current GET must
        # not tell the model it has just reverified that success. The final
        # proposal gate still performs its own fresh read before publication.
        state = 'unknown' if job['state'] == 'succeeded' and not self.confirmed_succeeded else job['state']
        return {"status": state, "import_job_id": job["job_id"],
                "video_id": job["video_id"], "knowledge_base_id": job["knowledge_base_id"]}

    def proposal(self):
        if not self.observed:
            raise ValueError("import_status_required")
        job = self._job()
        state = job["state"]
        if state == "succeeded":
            job = self._refresh(job)
            if not self.confirmed_succeeded:
                raise ValueError("import_status_unverified")
            state = job["state"]
        return import_status_proposal(job, self.library_name)
