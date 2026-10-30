"""Evidence retrieval runs inside the existing backend's Chroma owner process."""
from __future__ import annotations

import hmac
import json
import math
import re
import time
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from starlette.concurrency import run_in_threadpool

from app.mention_agent.contracts import Binding, validate_envelope
from app.mention_agent.knowledge import LocalKnowledgeTools
from .backend_knowledge import (BackendEvidenceError, MAX_QUESTION_CHARS, MAX_REQUEST_BYTES,
    MAX_RESPONSE_BYTES, EXECUTION_CLAIM_HEADER, decode_json, normalized_evidence_result, valid_execution_claim)
from .routes import local_request


class EvidenceError(ValueError):
    def __init__(self, code, status_code=409):
        self.code, self.status_code = code, status_code
        super().__init__(code)


class EvidenceBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    task_id: str = Field(strict=True, pattern=r"^[a-f0-9]{32}$")
    question: str = Field(strict=True, min_length=1, max_length=MAX_QUESTION_CHARS)
    focus_video_id: str | None = Field(default=None, strict=True, pattern=r"^[0-9]{5,30}$")
    focus: Literal['auto', 'current_video', 'authorized_scope'] = 'auto'


def original_comment_focus(envelope):
    from .evidence_focus import default_current_video
    return default_current_video(envelope)


class EvidenceService:
    def __init__(self, queue, registry, database, *, knowledge_factory=None, control=None):
        self.queue, self.registry, self.database = queue, registry, database
        self.knowledge_factory = knowledge_factory or LocalKnowledgeTools
        self.control = control

    def _scope(self, task_id, execution_token):
        if not valid_execution_claim(execution_token):
            raise EvidenceError("evidence_execution_claim_required", 403)
        task = self.queue.get_task(task_id)
        if not task:
            raise EvidenceError("evidence_task_not_found", 404)
        if (task.get("kind") != "query" or task.get("state") != "executing"
                or not isinstance(task.get("execution_token"), str) or not task["execution_token"]
                or type(task.get("lease_until")) not in (int, float)
                or not math.isfinite(task["lease_until"]) or task["lease_until"] <= time.time()):
            raise EvidenceError("evidence_task_not_executing")
        if not hmac.compare_digest(task["execution_token"], execution_token):
            raise EvidenceError("evidence_execution_claim_stale", 403)
        authorization = task.get("authorization")
        fresh = self.registry.authorize_snapshot(authorization, action="query")
        try:
            binding = Binding.from_dict(task["binding"])
            validate_envelope(task["envelope"], binding)
        except (ValueError, TypeError, KeyError):
            raise EvidenceError("evidence_task_scope_invalid") from None
        if (fresh is None or fresh != binding or task.get("knowledge_base_id") != binding.knowledge_base_id
                or not isinstance(authorization, dict) or task.get("user_id") != authorization.get("user_id")
                or task.get("device_id") != authorization.get("device_id")):
            raise EvidenceError("evidence_scope_not_authorized", 403)
        if self.control is not None:
            current = self.control.snapshot()
            if (current.get("run_id") != task.get("owner_run_id")
                    or current.get("desired_state") not in {"running", "paused"}):
                raise EvidenceError("evidence_owner_stopped")
        return task, binding

    def query(self, body, *, execution_token):
        if not isinstance(body, EvidenceBody):
            body = EvidenceBody.model_validate(body)
        if not body.question.strip():
            raise EvidenceError("evidence_question_empty", 422)
        task, binding = self._scope(body.task_id, execution_token)
        if body.focus_video_id is not None and body.focus_video_id != task["envelope"]["video_id"]:
            raise EvidenceError("evidence_focus_scope_mismatch", 403)
        # Focus only selects an intersection inside the independently verified
        # Binding. The model cannot choose a user, library or arbitrary video.
        # Explicit intent may search that authorized scope for cross-video
        # questions; old callers retain the original-comment auto heuristic.
        if body.focus == 'authorized_scope':
            if body.focus_video_id is not None:
                raise EvidenceError('evidence_focus_conflict', 422)
            focus = None
        elif body.focus == 'current_video':
            focus = task['envelope']['video_id']
        else:
            focus = original_comment_focus(task["envelope"]) or body.focus_video_id
        scope_options = {'query_scope':'all_local'} if binding.is_all_local else {}
        try:
            tools = self.knowledge_factory(self.database, binding.knowledge_base_id, focus_video_id=focus, **scope_options)
            result = tools.query(body.question.strip())
        except Exception:
            raise EvidenceError("evidence_query_failed", 503) from None
        after, after_binding = self._scope(body.task_id, execution_token)
        if (after_binding != binding or after.get("authorization") != task.get("authorization")
                or after.get("immutable") != task.get("immutable")
                or not hmac.compare_digest(after["execution_token"], task["execution_token"])):
            raise EvidenceError("evidence_task_changed_during_query", 403)
        try:
            # Re-read only SQL membership after retrieval, so removed/unready
            # material cannot escape even through an injected/stale retriever.
            sql_tools = LocalKnowledgeTools(self.database, binding.knowledge_base_id, **scope_options)
            with sql_tools._read_session() as db:
                description, allowed = sql_tools._snapshot(db)
            if not isinstance(result, dict) or result.get("knowledge_base_name") != description["name"]:
                raise BackendEvidenceError("backend_evidence_invalid_response")
            if focus is not None:
                allowed.intersection_update({focus})
            result = normalized_evidence_result(result, binding.knowledge_base_id, allowed_video_ids=allowed,
                query_scope=binding.query_scope)
            encoded = json.dumps(result, ensure_ascii=False, allow_nan=False).encode("utf-8")
            if len(encoded) > MAX_RESPONSE_BYTES:
                raise BackendEvidenceError("backend_evidence_response_too_large")
        except Exception:
            raise EvidenceError("evidence_result_invalid", 503) from None
        # Membership projection also took time: one final authorization fence.
        final, final_binding = self._scope(body.task_id, execution_token)
        if (final_binding != binding or final.get("authorization") != task.get("authorization")
                or final.get("immutable") != task.get("immutable")
                or not hmac.compare_digest(final["execution_token"], task["execution_token"])):
            raise EvidenceError("evidence_task_changed_during_query", 403)
        return result


_service = None


def configure_service(service):
    global _service
    _service = service


def get_service():
    global _service
    if _service is None:
        from .routes import get_service as get_assistant_service
        from .integration import STATE, configured_database, registry_if_configured
        backend = get_assistant_service()
        database = configured_database()
        registry = registry_if_configured(STATE, database)
        if registry is None:
            raise HTTPException(409, detail={"code": "evidence_registry_required"})
        _service = EvidenceService(backend.queue, registry, database, control=backend.control)
    return _service


router = APIRouter(prefix="/assistant", tags=["助手原文证据"], dependencies=[Depends(local_request)])


@router.post("/evidence/query")
async def query(request: Request, service=Depends(get_service)):
    execution_token = request.headers.get(EXECUTION_CLAIM_HEADER)
    if not valid_execution_claim(execution_token):
        raise HTTPException(403, detail={"code": "evidence_execution_claim_required"})
    raw = bytearray()
    async for chunk in request.stream():
        raw.extend(chunk)
        if len(raw) > MAX_REQUEST_BYTES:
            raise HTTPException(413, detail={"code": "evidence_request_too_large"})
    try:
        body = EvidenceBody.model_validate(decode_json(bytes(raw)))
    except (ValueError, TypeError, UnicodeError, ValidationError):
        raise HTTPException(422, detail={"code": "invalid_evidence_request"}) from None
    try:
        return await run_in_threadpool(service.query, body, execution_token=execution_token)
    except EvidenceError as error:
        raise HTTPException(error.status_code, detail={"code": error.code}) from None
    except Exception:
        raise HTTPException(503, detail={"code": "evidence_query_unavailable"}) from None


_operator_draft_service = None


def configure_operator_draft_service(service):
    global _operator_draft_service
    _operator_draft_service = service


def get_operator_draft_service():
    global _operator_draft_service
    if _operator_draft_service is None:
        from .routes import get_service as get_assistant_service
        from .integration import STATE, AGENT_PATH, configured_database, registry_if_configured, receiver_configuration
        from .operator_draft_service import OperatorDraftService, existing_agent_readonly
        try:
            backend = get_assistant_service()
            database = configured_database()
            registry = registry_if_configured(STATE, database)
            if registry is None:
                raise ValueError()
            _operator_draft_service = OperatorDraftService(backend.queue, registry, database,
                existing_agent_readonly(AGENT_PATH), STATE, control=backend.control,
                receiver_provider=receiver_configuration)
        except Exception:
            raise HTTPException(409, detail={'code': 'operator_draft_service_unavailable'}) from None
    return _operator_draft_service


async def _operator_draft_call(action, *args):
    from .operator_draft_service import OperatorDraftError
    try:
        return await run_in_threadpool(action, *args)
    except OperatorDraftError as error:
        raise HTTPException(error.status_code, detail={'code': error.code}) from None
    except Exception:
        raise HTTPException(503, detail={'code': 'operator_draft_unavailable'}) from None


@router.get('/operator-query-draft/{task_id}/evidence')
async def operator_draft_evidence(task_id: str, service=Depends(get_operator_draft_service)):
    return await _operator_draft_call(service.evidence, task_id)


@router.post('/operator-query-draft/{task_id}')
async def operator_draft_register(task_id: str, request: Request, service=Depends(get_operator_draft_service)):
    raw = bytearray()
    async for chunk in request.stream():
        raw.extend(chunk)
        if len(raw) > MAX_REQUEST_BYTES:
            raise HTTPException(413, detail={'code': 'operator_draft_request_too_large'})
    try:
        body = decode_json(bytes(raw))
    except (ValueError, TypeError, UnicodeError):
        raise HTTPException(422, detail={'code': 'operator_draft_request_invalid'}) from None
    return await _operator_draft_call(service.register, task_id, body)


@router.get('/operator-query-draft/{task_id}/bundle')
async def operator_draft_bundle(task_id: str, service=Depends(get_operator_draft_service)):
    return await _operator_draft_call(service.bundle, task_id)
