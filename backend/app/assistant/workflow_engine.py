"""Single-step workflow coordinator with trusted, injected business adapters.

This module never opens a browser/model, imports a video, or publishes by itself.
The tool adapters must use existing guarded operations and return real receipts.
The authorizer verifies fresh source/UI/Registry facts, not model-supplied flags.
No integration into the live assistant is enabled by importing this component.
"""
from __future__ import annotations
from copy import deepcopy
import hashlib

from .workflow_store import (WorkflowError, WorkflowStore, TOOLS, _integer, _json,
    _key, _text, validate_amendment, validate_plan, validate_scope, validate_source)


class WorkflowEngine:
    def __init__(self, store, *, tools, authorize, estimate_tokens,
                 validate_proposal, read_publication, should_stop=None,
                 poll_seconds=5, lease_seconds=120, reply_char_limit=320):
        if not isinstance(store, WorkflowStore) or type(tools) is not dict or set(tools) != TOOLS:
            raise WorkflowError("workflow_engine_invalid")
        if any(not callable(value) for value in (*tools.values(), authorize,
                estimate_tokens, validate_proposal, read_publication)):
            raise WorkflowError("workflow_engine_invalid")
        if should_stop is not None and not callable(should_stop):
            raise WorkflowError("workflow_engine_invalid")
        if type(poll_seconds) is not int or not 1 <= poll_seconds <= 300:
            raise WorkflowError("workflow_poll_interval_invalid")
        if type(lease_seconds) is not int or not 1 <= lease_seconds <= 1200:
            raise WorkflowError("workflow_lease_invalid")
        if type(reply_char_limit) is not int or not 1 <= reply_char_limit <= 8000:
            raise WorkflowError("workflow_reply_limit_invalid")
        self.store, self.tools = store, dict(tools)
        self.authorize, self.estimate_tokens = authorize, estimate_tokens
        self.validate_proposal, self.read_publication = validate_proposal, read_publication
        self.should_stop = should_stop or (lambda: False)
        self.poll_seconds, self.lease_seconds = poll_seconds, lease_seconds
        self.reply_char_limit = reply_char_limit

    def _active(self, workflow, phase):
        try:
            if self.should_stop() is not False or self.authorize(deepcopy(workflow), phase) is not True:
                raise ValueError()
        except Exception:
            raise WorkflowError("workflow_authorization_changed") from None

    def _finish(self, claim, **result):
        try:
            return self.store.finish(claim, **result)
        except WorkflowError as error:
            # Another owner may already have fenced this claim as unknown.
            # A late response never settles a new reservation or unblocks it.
            if error.code in {"workflow_revision_changed", "workflow_claim_changed"}:
                return self.store.get(claim.workflow_id)
            raise

    def start(self, *, source, scope, plan, budget_limit, initial_tokens=0):
        source, scope = validate_source(source), validate_scope(scope)
        plan = validate_plan(plan, scope)
        self._active({"source": source, "scope": scope, "plan": plan}, "create")
        return self.store.create(source=source, scope=scope, plan=plan,
            budget_limit=budget_limit, initial_tokens=initial_tokens)

    def revise(self, workflow_id, *, expected_revision, plan, amendment):
        workflow = self.store.get(workflow_id)
        if workflow is None:
            raise WorkflowError("workflow_missing")
        plan = validate_plan(plan, workflow["scope"])
        amendment = validate_amendment(amendment)
        candidate = {**deepcopy(workflow), "previous_plan": deepcopy(workflow["plan"]),
            "plan": plan, "amendment": amendment}
        self._active(candidate, "revise")
        return self.store.revise(workflow_id, expected_revision=expected_revision,
            plan=plan, amendment=amendment)

    def cancel(self, workflow_id, *, expected_revision):
        workflow = self.store.get(workflow_id)
        if workflow is None:
            raise WorkflowError("workflow_missing")
        self._active(workflow, "cancel")
        return self.store.cancel(workflow_id, expected_revision=expected_revision)

    @staticmethod
    def _usage(result, tool):
        if type(result) is not dict:
            return None
        usage = result.get("usage")
        if tool == "await_import" and usage is None:
            return 0  # Contract: a receipt GET, never a model/embedding request.
        if type(usage) is not dict or "total_tokens" not in usage:
            return None
        try:
            if set(usage) - {"total_tokens", "input_tokens", "output_tokens", "elapsed_ms", "model_responses"}:
                return None
            for value in usage.values():
                _integer(value)
            if "input_tokens" in usage and "output_tokens" in usage and (
                    usage["input_tokens"] + usage["output_tokens"] != usage["total_tokens"]):
                return None
            if tool == "await_import" and usage["total_tokens"] != 0:
                return None
            return usage["total_tokens"]
        except WorkflowError:
            return None

    @staticmethod
    def _prior(workflow, tool):
        rows = [step for step in workflow["steps"] if step["state"] == "succeeded"
                and step["spec"]["tool"] == tool]
        if len(rows) != 1 or type(rows[0]["result"]) is not dict:
            raise WorkflowError("workflow_precondition_missing")
        return deepcopy(rows[0]["result"])

    @staticmethod
    def _correlated(result, workflow, step):
        if type(result) is not dict:
            return False
        scope = workflow["scope"]
        return ({"operation_key", "video_id", "knowledge_base_id"}.issubset(result)
            and type(result["knowledge_base_id"]) is type(scope["knowledge_base_id"])
            and result["operation_key"] == step["request_key"]
            and result["video_id"] == scope["video_id"]
            and result["knowledge_base_id"] == scope["knowledge_base_id"])

    def _context(self, workflow, step):
        context = {"workflow_id": workflow["workflow_id"], "origin_task_id": workflow["origin_task_id"],
            "source": deepcopy(workflow["source"]), "scope": deepcopy(workflow["scope"]),
            "goal": workflow["plan"]["goal"], "request_key": step["request_key"],
            "args": deepcopy(step["spec"]["args"])}
        tool = step["spec"]["tool"]
        if tool == "await_import":
            context["import_receipt"] = self._prior(workflow, "import_current_video")
        elif tool == "query_current_video" and workflow["plan"]["goal"] == "save_then_query":
            ready = self._prior(workflow, "await_import")
            if ready.get("status") != "succeeded" or ready.get("readable") is not True:
                raise WorkflowError("workflow_import_not_ready")
            context["import_receipt"] = ready
        elif tool == "propose_reply":
            name = "await_import" if step["spec"]["args"]["purpose"] == "import_status" else "query_current_video"
            context["evidence"] = self._prior(workflow, name)
        return context

    def _checked_result(self, result, workflow, step, context):
        if type(result) is not dict:
            raise WorkflowError("workflow_result_invalid")
        _json(result)  # Reject credentials/raw messages and unbounded payloads.
        scope, tool = workflow["scope"], step["spec"]["tool"]
        if not self._correlated(result, workflow, step) or "status" not in result:
            raise WorkflowError("workflow_result_scope_mismatch")
        status = result.get("status")
        if status in {"unknown", "submission_unknown", "delivery_unknown"}:
            return "unknown", "workflow_effect_unknown"
        if status in {"failed", "failed_no_submit", "needs_review", "no_evidence"}:
            return "failed", "workflow_tool_failed"
        if tool in {"import_current_video", "await_import"}:
            _key(result.get("receipt_id"))
            if tool == "import_current_video":
                if status not in {"accepted", "succeeded"}:
                    raise WorkflowError("workflow_import_result_invalid")
            else:
                if result["receipt_id"] != context["import_receipt"].get("receipt_id"):
                    raise WorkflowError("workflow_import_receipt_changed")
                if status in {"queued", "processing"}:
                    return "waiting", None
                if status != "succeeded" or result.get("readable") is not True:
                    raise WorkflowError("workflow_import_not_ready")
        elif tool == "query_current_video":
            sources = result.get("sources")
            if status != "ok" or type(sources) is not list or not 1 <= len(sources) <= 8:
                raise WorkflowError("workflow_evidence_missing")
            ids = set()
            for source in sources:
                if type(source) is not dict or source.get("platform_item_id") != scope["video_id"]:
                    raise WorkflowError("workflow_evidence_scope_mismatch")
                key = _text(source.get("source_id"), 128)
                _text(source.get("chunk_id"), 128)
                _text(source.get("text"), 20000)
                if key in ids:
                    raise WorkflowError("workflow_evidence_duplicate")
                ids.add(key)
        else:
            proposal = result.get("proposal")
            if status != "ready" or type(proposal) is not dict:
                raise WorkflowError("workflow_proposal_invalid")
            purpose = step["spec"]["args"]["purpose"]
            if proposal.get("kind") != purpose:
                raise WorkflowError("workflow_proposal_invalid")
            _text(proposal.get("text"), self.reply_char_limit)
            source_ids = proposal.get("source_ids")
            if purpose == "answer":
                known = {source["source_id"] for source in context["evidence"]["sources"]}
                if (type(source_ids) is not list or not 1 <= len(source_ids) <= 8
                        or any(type(key) is not str or key not in known for key in source_ids)
                        or len(set(source_ids)) != len(source_ids)):
                    raise WorkflowError("workflow_proposal_sources_invalid")
            elif source_ids not in (None, []):
                raise WorkflowError("workflow_proposal_sources_invalid")
            if self.validate_proposal(deepcopy(workflow), deepcopy(proposal),
                    deepcopy(context["evidence"])) is not True:
                raise WorkflowError("workflow_proposal_validation_failed")
        return "succeeded", None

    def tick(self, workflow_id):
        workflow = self.store.get(workflow_id)
        if workflow is None:
            raise WorkflowError("workflow_missing")
        if workflow["state"] in {"completed", "needs_review", "cancelled"}:
            return workflow
        if self.should_stop() is not False:
            return workflow
        if workflow["state"] == "proposal_ready":
            try:
                self._active(workflow, "read_publication")
                receipt = self.read_publication(deepcopy(workflow))
                self._active(workflow, "record_publication")
                if receipt is None:
                    return workflow
                return self.store.record_publication(workflow_id,
                    expected_revision=workflow["revision"], receipt=receipt)
            except WorkflowError as error:
                return self.store.hold(workflow_id, expected_revision=workflow["revision"], reason_code=error.code)
            except Exception:
                return self.store.hold(workflow_id, expected_revision=workflow["revision"],
                    reason_code="workflow_publication_observation_unavailable")
        active = [step for step in workflow["steps"][:len(workflow["plan"]["steps"])]
                  if step["state"] != "succeeded"]
        if not active:
            raise WorkflowError("workflow_state_invalid")
        step, now = active[0], self.store._now()
        if step["state"] == "reserved":
            if step["lease_until"] is not None and now >= step["lease_until"]:
                return self.store.expire_reserved(workflow_id, expected_revision=workflow["revision"])
            return workflow
        if step["state"] == "waiting" and (step["next_poll_at"] or 0) > now:
            return workflow
        tool = step["spec"]["tool"]
        try:
            self._active(workflow, "before:" + tool)
            context = self._context(workflow, step)
            estimate = 0 if tool == "await_import" else _integer(self.estimate_tokens(deepcopy(workflow), tool))
            claim = self.store.reserve_next(workflow_id, expected_revision=workflow["revision"],
                estimated_tokens=estimate, lease_seconds=self.lease_seconds)
        except WorkflowError as error:
            return self.store.hold(workflow_id, expected_revision=workflow["revision"], reason_code=error.code)
        except Exception:
            return self.store.hold(workflow_id, expected_revision=workflow["revision"],
                reason_code="workflow_preparation_unavailable")
        if claim is None:
            return self.store.get(workflow_id)
        reserved = self.store.get(workflow_id)
        def checkpoint():
            try:
                self.store.assert_claim_current(claim)
                self._active(reserved, "checkpoint:" + tool)
                return self.store.assert_claim_current(claim)
            except Exception:
                return False
        try:
            self._active(reserved, "dispatch:" + tool)
        except WorkflowError as error:
            try:
                self.store.assert_claim_current(claim)
            except WorkflowError:
                return self.store.get(workflow_id)
            return self._finish(claim, state="failed", actual_tokens=0, reason_code=error.code)
        if not checkpoint():
            current = self.store.get(workflow_id)
            if current["revision"] == claim.revision:
                try:
                    self.store.assert_claim_current(claim)
                except WorkflowError:
                    return self.store.expire_reserved(workflow_id, expected_revision=claim.revision)
                return self._finish(claim, state="failed", actual_tokens=0,
                    reason_code="workflow_authorization_changed")
            return current
        try:
            # Trusted adapters must pass this checkpoint into their existing
            # final import/query/publication gates immediately before effects.
            result = self.tools[tool](deepcopy(context), checkpoint=checkpoint)
        except Exception:
            return self._finish(claim, state="unknown", actual_tokens=None,
                reason_code="workflow_tool_result_unknown")
        # Usage from another request/video/library cannot settle this claim,
        # even when the mismatched response reports a tempting zero cost.
        actual = self._usage(result, tool) if self._correlated(result, workflow, step) else None
        checked = False
        try:
            state, reason = self._checked_result(result, workflow, step, context)
            checked = True
            self._active(reserved, "after:" + tool)
        except WorkflowError as error:
            return self._finish(claim, state="unknown", result=result if checked else None,
                actual_tokens=actual, reason_code=error.code)
        except Exception:
            return self._finish(claim, state="unknown", actual_tokens=actual,
                reason_code="workflow_result_validation_unavailable")
        return self._finish(claim, state=state, result=result, actual_tokens=actual,
            reason_code=reason, poll_seconds=self.poll_seconds)
