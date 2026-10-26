"""One reserved public reply through a bounded normal-page adapter.

The model never obtains this object or a send capability. Reservations survive
crashes; a returned/raised uncertain submission is never retried here.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import uuid

from .contracts import Binding
from .store import AgentStoreError
from app.mention_receiver.browser import ReceiverInterventionRequired


_LOOKUP_REASONS=frozenset({'invalid_original_target','native_correlation_requires_source_id',
    'receiver_binding_mismatch','original_lookup_stopped','original_identity_not_ready',
    'original_notifications_unavailable','original_notification_not_found',
    'original_notification_candidates_exceed_limit','original_notification_not_unique',
    'original_notification_changed','original_candidate_unreadable','original_target_not_unique',
    'original_target_changed','original_native_semantic_ambiguous','original_lookup_unavailable',
    'matched','intervention_required'})


def _fresh_lookup_failure(adapter,previous):
    lookup=getattr(adapter,'last_original_lookup',None)
    # The shared opener replaces this dict at the start of each lookup. A
    # different preparation path must never inherit an earlier task's result.
    if not isinstance(lookup,dict) or lookup is previous:return {},None
    counts={key:lookup[key] for key in ('candidate_limit','candidate_count','checked_count',
        'match_count','unreadable_count','final_rechecks','final_verified','snapshot_changes','page_open_count')
        if type(lookup.get(key)) is int and 0<=lookup[key]<=100000}
    stage=lookup.get('lookup_stage_index')
    if type(stage) is int and 0<=stage<=10:counts['lookup_stage_index']=stage
    reason=lookup.get('reason_code')
    return counts,reason if type(reason) is str and reason in _LOOKUP_REASONS else None


class ReplyPreflightError(RuntimeError):
    def __init__(self, code: str):
        if not re.fullmatch(r"[a-z][a-z0-9_]{0,127}", code):
            code = "preflight_failed"
        self.code = code
        super().__init__(code)


class ReplyPublisher:
    def __init__(self, store, binding: Binding, adapter, evidence_root: Path, *, should_stop=None,
                 scope_authorizer=None, publication_allowed=None):
        self.store, self.binding, self.adapter = store, binding, adapter
        self.evidence_root = Path(evidence_root).absolute()
        self.should_stop = should_stop or (lambda: False)
        if scope_authorizer is not None and not callable(scope_authorizer):
            raise ValueError("invalid_scope_authorizer")
        self.scope_authorizer = scope_authorizer
        if publication_allowed is not None and not callable(publication_allowed):
            raise ValueError("invalid_publication_gate")
        self.publication_allowed = (lambda: True) if publication_allowed is None else publication_allowed

    def _authorized(self):
        current = self.store.get_binding(self.binding.binding_id)
        if not current:
            return False
        value = Binding.from_dict(current)
        return (value == self.binding and value.active and value.public_reply_allowed
                and (self.scope_authorizer is None or self.scope_authorizer(value) is True))

    def _directory(self, task_id):
        if not re.fullmatch(r"[a-f0-9]{32,64}", task_id):
            raise ReplyPreflightError("invalid_task_id")
        root = self.evidence_root
        if root.resolve() != root or root.is_symlink() or (root.exists() and not root.is_dir()):
            raise ReplyPreflightError("evidence_path_redirected")
        # No arbitrary CLI output path is accepted; the integration chooses a
        # child of the already protected independent ledger directory.
        store_path = Path(self.store.path).absolute()
        if not root.is_relative_to(store_path.parent) or root == store_path.parent:
            raise ReplyPreflightError("evidence_outside_agent_state")
        task_root = root / task_id
        if task_root.resolve() != task_root or task_root.is_symlink():
            raise ReplyPreflightError("evidence_path_redirected")
        path = task_root / uuid.uuid4().hex
        path.mkdir(parents=True, exist_ok=False)
        return path

    @staticmethod
    def _write(path, name, value):
        with (path / name).open("x", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.write("\n")

    def _hold(self, task_id, reason):
        try:
            self.store.hold_reply(task_id, reason)
        except AgentStoreError:
            pass  # A competing reservation/hold must never be reset.

    def publish(self, task_id):
        task, reply = self.store.get_task(task_id), self.store.get_reply(task_id)
        if (not task or not reply or task["binding_id"] != self.binding.binding_id
                or task["state"] != "answer_ready" or reply["state"] != "ready"):
            return {"state": "not_sendable", "task_id": task_id, "attempted": False}
        target, text = reply["target"], reply["text"]
        if target["source_mode"] != "live_ui" or not self._authorized():
            self._hold(task_id, "public_send_not_authorized")
            return {"state": "preflight_blocked", "task_id": task_id, "attempted": False,
                    "reason_code": "public_send_not_authorized"}
        if self.should_stop():
            return {"state": "preflight_blocked", "task_id": task_id, "attempted": False,
                    "reason_code": "run_stopped_before_prepare"}

        def pause_before_reservation(*, prepared):
            if self.publication_allowed() is True:
                return None
            if prepared:
                # This is still an unreserved draft. The normal adapter clears
                # only this exact body under its freshly verified parent/actor.
                self.adapter.clear_prepared_reply(target, text)
            return {"state": "preflight_blocked", "task_id": task_id,
                    "attempted": prepared, "submitted": False, "reason_code": "publication_paused"}

        directory = None
        previous_lookup=getattr(self.adapter,'last_original_lookup',None)
        try:
            paused = pause_before_reservation(prepared=False)
            if paused is not None:
                return paused
            directory = self._directory(task_id)
            verification = self.adapter.prepare_reply(target, text)
            if self.should_stop() or not self._authorized():
                raise ReplyPreflightError("run_stopped_or_binding_changed")
            paused = pause_before_reservation(prepared=True)
            if paused is not None:
                return paused
            self._write(directory, "preflight.json", {
                "task_id": task_id, "observed_at_utc": datetime.now(timezone.utc).isoformat(),
                "verification": verification, "reply_sha256": hashlib.sha256(text.encode()).hexdigest()})
            if self.should_stop() or not self._authorized():
                raise ReplyPreflightError("run_stopped_or_binding_changed")
            paused = pause_before_reservation(prepared=True)
            if paused is not None:
                return paused
            reservation = self.store.reserve_send(task_id, verification)
            if reservation is None:
                raise ReplyPreflightError("reservation_not_granted")
        except ReceiverInterventionRequired:
            self._hold(task_id, "preflight_verification_required")
            return {"state": "verification_required", "task_id": task_id, "attempted": True,
                    "submitted": False, "reason_code": "preflight_verification_required"}
        except Exception as error:
            code = error.code if isinstance(error, ReplyPreflightError) else "preflight_failed"
            self._hold(task_id, code)
            # Keep only bounded scalar diagnostics. Never persist a page,
            # notification body, signed media URL or arbitrary exception text.
            safe_counts,lookup_reason=_fresh_lookup_failure(self.adapter,previous_lookup)
            if directory is not None and (safe_counts or lookup_reason is not None):
                try:
                    self._write(directory, 'preflight_failure.json', {'reason_code': code,
                        'original_lookup': safe_counts,
                        **({'lookup_reason_code':lookup_reason} if lookup_reason is not None else {})})
                except Exception:
                    pass
            return {"state": "preflight_blocked", "task_id": task_id, "attempted": True,
                    "submitted": False, "reason_code": code}

        # This capability remains only in memory/the independent store, and is
        # deliberately absent from diagnostics, screenshots and returned data.
        token = reservation["send_token"]
        gate_passed = False

        def before_submit():
            nonlocal gate_passed
            if gate_passed or self.should_stop() or not self._authorized():
                raise ReplyPreflightError("submission_gate_closed")
            now = self.store.get_reply(task_id)
            if not now or now["state"] != "reserved" or now["send_token"] != token:
                raise ReplyPreflightError("reservation_changed")
            # The nonce read can block. Re-read both trusted scope and product
            # publication control after it, before granting this one click.
            # An already granted/clicked operation is not revoked retroactively.
            if self.should_stop() or not self._authorized() or self.publication_allowed() is not True:
                raise ReplyPreflightError("submission_gate_closed")
            gate_passed = True

        try:
            observation = self.adapter.submit_reply(target, text, before_submit=before_submit)
            if not isinstance(observation, dict):
                raise ValueError("invalid adapter observation")
            outcome = observation.get("outcome")
            # A success report requires a gate invocation and a readback of the
            # exact body under the locked parent by the expected B author.
            if outcome == "b_view_visible":
                if not gate_passed or observation.get("exact_body_in_original_thread") is not True:
                    raise ValueError("unverified reply observation")
            elif outcome == "failed_no_submit":
                if gate_passed:
                    outcome = "delivery_unknown"
            elif outcome not in {"delivery_unknown", "verification_required"}:
                outcome = "delivery_unknown"
            evidence = {"observed_at_utc": datetime.now(timezone.utc).isoformat(),
                        "adapter_outcome": outcome, "submit_gate_passed": gate_passed,
                        "exact_body_in_original_thread": observation.get("exact_body_in_original_thread") is True,
                        "reply_sha256": hashlib.sha256(text.encode()).hexdigest(),
                        "scope": "B view only; no claim of A or public visibility"}
            quality = observation.get('reply_readback_quality')
            if isinstance(quality, str) and quality in {'direct_selected_parent','weak_unique_root'}:
                evidence['reply_readback_quality'] = quality
        except ReceiverInterventionRequired:
            outcome = "verification_required"
            evidence = {"adapter_outcome": outcome, "submit_gate_passed": gate_passed,
                        "reason_code": "human_verification_required"}
        except Exception as error:
            # Once the gate allowed a click, even a timeout may mean a posted
            # reply. Never turn that into a retryable no-submit failure.
            outcome = "delivery_unknown" if gate_passed else "failed_no_submit"
            evidence = {"adapter_outcome": outcome, "submit_gate_passed": gate_passed,
                        "error_type": type(error).__name__}
        # Record the durable terminal result before optional evidence I/O.
        # If this write fails, the durable reservation remains non-retryable.
        try:
            self.store.finish_send(task_id, token, outcome, evidence)
        except Exception:
            return {"state": "delivery_unknown", "task_id": task_id, "attempted": True,
                    "reason_code": "result_record_failed_reservation_retained"}
        try:
            self._write(directory, "result.json", evidence)
            # Never screenshot an authentication/verification dialog.
            if outcome == "b_view_visible":
                self.adapter.save_reply_evidence(target, text, directory / "b-view-original-thread.png")
        except Exception:
            pass  # A screenshot failure cannot cause a second submission.
        return {"state": outcome, "task_id": task_id, "attempted": True,
                "evidence_directory": str(directory), "submit_gate_passed": gate_passed}
