"""Injectable product scheduler. Browser adapters stay on their owner lane.

The execution lane may run in a worker thread. Intake and publication methods
are explicitly owner-thread operations and never called by that worker.
"""
from __future__ import annotations

import threading

from .queue import AssistantQueue, QueueError


class AssistantRuntime:
    def __init__(self, queue: AssistantQueue, control, *, execute=None, publish=None, reconcile=None, intake=None,
                 run_id=None, owner_thread_id=None, resume_adapter=None, retry_adapter=None,
                 delivery_readback_adapter=None, recorded_readback_adapter=None,
                 operator_readback_adapter=None):
        self.queue, self.control = queue, control
        self.execute_adapter, self.publish_adapter, self.reconcile_adapter = execute, publish, reconcile
        self.intake_adapter = intake
        self.resume_adapter = resume_adapter
        self.retry_adapter = retry_adapter
        self.delivery_readback_adapter = delivery_readback_adapter
        self.recorded_readback_adapter = recorded_readback_adapter
        self.operator_readback_adapter = operator_readback_adapter
        self._delivery_readback_cursor = None
        self.run_id = run_id or control.snapshot().get("run_id") or "local"
        self.owner_thread_id = threading.get_ident() if owner_thread_id is None else owner_thread_id

    def _running(self):
        value = self.control.snapshot()
        return value.get("desired_state") == "running" and value.get("run_id") in {None, self.run_id}

    def _owner(self):
        if threading.get_ident() != self.owner_thread_id:
            raise QueueError("browser_lane_wrong_thread")

    def recover(self):
        """Call only after the controller established that the prior owner exited."""
        return self.queue.recover_interrupted(current_run_id=self.run_id)

    def reconcile_delivery_once(self):
        """Observe at most one already-attempted reply, never execute or publish.

        A verified credential stays in-process from the legacy store to the
        queue. JSON-shaped HTTP/client claims cannot substitute for it.
        """
        self._owner()
        if not self._running() or self.delivery_readback_adapter is None:
            return None
        if self.recorded_readback_adapter is not None:
            # A crash after a durable positive observation does not authorize
            # a fourth browser attempt. Rebuild this local projection instead.
            candidates = self.queue.list_delivery_readback_candidates(limit=3,
                before=self._delivery_readback_cursor)
            if not candidates and self._delivery_readback_cursor is not None:
                self._delivery_readback_cursor = None
                candidates = self.queue.list_delivery_readback_candidates(limit=3)
            for candidate in candidates:
                if not self._running():
                    return None
                self._delivery_readback_cursor = (candidate['created_at'], candidate['id'])
                try:
                    recorded = self.recorded_readback_adapter(candidate)
                    if recorded is not None:
                        return self.queue.project_recorded_delivery_readback(candidate['id'], recorded,
                            run_id=self.run_id)
                except (ValueError, QueueError):
                    # A stale or corrupt historical proof remains unknown. It
                    # must not block another reply or lower proof requirements.
                    continue
        task = self.queue.claim_delivery_readback(run_id=self.run_id)
        if task is None:
            return None
        token = task['_delivery_readback_claim_token']
        if not self._running():
            return self.queue.finish_delivery_readback(task['id'], token,
                reason_code='readback_control_paused')
        try:
            confirmation = self.delivery_readback_adapter(task)
        except Exception as error:
            from app.mention_receiver.browser import ReceiverInterventionRequired
            try:
                self.queue.finish_delivery_readback(task['id'], token,
                    reason_code='readback_verification_required' if isinstance(error, ReceiverInterventionRequired)
                    else 'readback_unconfirmed')
            finally:
                # A local evidence-write failure must not hide a CAPTCHA/login
                # challenge and allow the owner loop to navigate afterwards.
                if isinstance(error, ReceiverInterventionRequired):
                    raise error
            return self.queue.get_task(task['id'])
        return self.queue.finish_delivery_readback(task['id'], token,
            confirmation=confirmation,
            reason_code=None if confirmation is not None else 'readback_not_visible')

    def receive_once(self):
        self._owner()
        state = self.control.snapshot()
        if self.intake_adapter is None or state.get("desired_state") not in {"running", "paused"}:
            return []
        rows = self.intake_adapter()
        if not isinstance(rows, (list, tuple)) or len(rows) > 100:
            raise QueueError("invalid_intake_batch")
        results = []
        for item in rows:
            if not isinstance(item, dict) or set(item) - {"binding", "envelope", "video_title", "kind", "notice_fingerprint", "user_id", "device_id", "authorization", "initial_state"}:
                raise QueueError("invalid_intake_item")
            results.append(self.queue.enqueue(**item))
        return results

    def execute_once(self):
        if not self._running() or self.execute_adapter is None:
            return None
        task = self.queue.claim_next("execute", run_id=self.run_id)
        if task is None:
            return None
        # Stop/pause after claim is reconciled, never converted to an implicit
        # paid retry. The adapter also receives current control via its closure.
        if not self._running():
            return self.queue.finish(task["id"], task["execution_token"], "execute",
                                     {"state": "needs_reconcile", "reason_code": "stopped_before_execution"})
        try:
            result = self.execute_adapter(task)
            return self.queue.finish(task["id"], task["execution_token"], "execute", result)
        except Exception:
            # Raw exceptions may contain prompts, credentials, SQL or file paths.
            return self.queue.finish(task["id"], task["execution_token"], "execute",
                                     {"state": "needs_reconcile", "reason_code": "execution_result_unconfirmed"})

    def publish_once(self):
        self._owner()
        if not self._running() or self.publish_adapter is None:
            return None
        task = self.queue.claim_next("publish", run_id=self.run_id)
        if task is None:
            return None
        if not self._running():
            return self.queue.finish(task["id"], task["publication_token"], "publish",
                                     {"state": "needs_reconcile", "reason_code": "stopped_before_publication"})
        try:
            result = self.publish_adapter(task)
            return self.queue.finish(task["id"], task["publication_token"], "publish", result)
        except Exception:
            # The old publisher's own send token is the authoritative boundary.
            # Query it next; never guess that an exception means no comment.
            return self.queue.finish(task["id"], task["publication_token"], "publish",
                                     {"state": "needs_reconcile", "reason_code": "publication_result_unconfirmed"})

    def reconcile_task(self, task_id, *, operator_readback=False):
        if self.reconcile_adapter is None:
            raise QueueError("reconciler_unavailable")
        task = self.queue.get_task(task_id)
        if task is None:
            raise QueueError("task_not_found")
        if task["state"] in {"executing", "publishing"}:
            raise QueueError("task_busy")
        if operator_readback and task['state']=='delivery_unknown' and self.operator_readback_adapter is not None:
            # A deliberate local "check" is a fresh bounded observation, not
            # an automatic fourth attempt or a reset of the original send.
            self._owner()
            if not self._running():
                raise QueueError('readback_requires_running')
            try:
                confirmation=self.operator_readback_adapter(task)
                if confirmation is not None:
                    return self.queue.project_recorded_delivery_readback(task_id,confirmation,run_id=self.run_id)
            except Exception as error:
                from app.mention_receiver.browser import ReceiverInterventionRequired
                if isinstance(error,ReceiverInterventionRequired):
                    raise
                raise QueueError('reconciliation_unavailable') from None
        try:
            result = self.reconcile_adapter(task)
        except Exception:
            raise QueueError("reconciliation_unavailable") from None
        return self.queue.apply_reconciliation(task_id, result)

    def resume_task(self, task_id):
        # Fresh trusted receipt and authorization are required for every resume;
        # HTTP callers cannot supply these proof fields themselves.
        task = self.queue.get_task(task_id)
        if task is None:
            raise QueueError("task_not_found")
        if task["state"] not in {"needs_review", "needs_reconcile"}:
            raise QueueError("resume_not_safe")
        if self.resume_adapter is not None:
            try:
                if self.resume_adapter(task) is False:
                    raise QueueError("resume_not_safe")
            except QueueError:
                raise
            except Exception:
                raise QueueError("operator_resume_unavailable") from None
        self.reconcile_task(task_id)
        return self.queue.resume(task_id)

    def retry_query_task(self, task_id, *, _action=None):
        """Arm one explicit query-only attempt; the worker runs the model later.

        A replay/partial crash observes the durable intent, never re-arms it.
        This is separate from ordinary safe-unstarted resume.
        """
        if self.retry_adapter is None:
            if _action is not None:
                self.queue.hold_query_retry(task_id,_action['id'],'query_retry_unavailable')
            raise QueueError('query_retry_unavailable')
        if _action is None:
            prior = self.queue.get_query_retry(task_id)
            if prior is not None:
                return self.queue.get_task(task_id)
            if not self._running():
                raise QueueError('query_retry_requires_running')
            self.reconcile_task(task_id)
            requested = self.queue.request_action(task_id,'retry_query')
            action = self.queue.take_query_retry_action(task_id,requested['id'])
            if action is None:
                return self.queue.get_task(task_id)
        else:
            action = _action
        if not self.queue.query_retry_action_current(task_id,action.get('id'),action.get('_query_retry_token')):
            raise QueueError('query_retry_action_not_current')
        try:
            if not self._running():
                raise QueueError('query_retry_requires_running')
            task = self.queue.get_task(task_id)
            if task is None or action.get('action')!='retry_query' or action.get('task_id')!=task_id:
                raise QueueError('query_retry_not_safe')
            result = self.retry_adapter(task,action['id'])
            return self.queue.activate_query_retry(task_id,action['id'],action.get('_query_retry_token'),result)
        except BaseException:
            # Even if the agent arm committed, never automatically repeat the
            # cross-ledger handoff or any possibly claimed paid attempt.
            self.queue.hold_query_retry(task_id,action['id'])
            self.queue.finish_action(action['id'],'query_retry_unconfirmed')
            raise

    def poll_waiting_once(self):
        if not self._running() or self.reconcile_adapter is None:
            return None
        task = self.queue.claim_waiting()
        if task is None:
            return None
        try:
            result = self.reconcile_adapter(task)
        except Exception:
            result = {"state": "waiting_ingestion", "reason_code": "ingestion_status_unavailable"}
        return self.queue.apply_reconciliation(task["id"], result, poll_token=task["poll_token"])

    def process_action_once(self):
        action = self.queue.take_action()
        if action is None:
            return None
        if action.get('action')=='retry_query' and action.get('state')!='processing':
            return {'action_id':action['id'],'state':'failed','reason_code':action.get('reason_code')}
        try:
            if action['action']=='retry_query':
                task=self.retry_query_task(action['task_id'],_action=action)
            else:
                task = self.reconcile_task(action["task_id"],operator_readback=True) if action["action"] == "reconcile" else self.resume_task(action["task_id"])
        except QueueError as error:
            self.queue.finish_action(action["id"], str(error))
            return {"action_id": action["id"], "state": "failed", "reason_code": str(error)}
        except Exception as error:
            from app.mention_receiver.browser import ReceiverInterventionRequired
            if isinstance(error,ReceiverInterventionRequired):
                try:
                    self.queue.finish_action(action['id'],'readback_verification_required')
                finally:
                    # A ledger-write error cannot hide a live challenge and
                    # allow the owner loop to navigate afterwards.
                    raise error
            self.queue.finish_action(action["id"], "action_unavailable")
            return {"action_id": action["id"], "state": "failed", "reason_code": "action_unavailable"}
        self.queue.finish_action(action["id"])
        return {"action_id": action["id"], "state": "done", "task": self.queue.public_task(task)}
