"""Connect a verified intake, isolated ledger, bounded model and local tools."""
from __future__ import annotations

import json
from pathlib import Path
from app.assistant.legacy_policy import legacy_testing_blocked

from .contracts import Binding, validate_envelope
from .engine import AgentEngine, ToolExecutionFailure
from .intake import make_envelope, should_receive_notice
from .knowledge import LocalKnowledgeTools
from .model import DeepSeekToolModel, IncompleteModelResponse
from .store import AgentStore, AgentStoreError
from app.assistant.reply_style import normalize_reply_prefix


def read_binding(path: Path) -> Binding:
    return Binding.from_dict(json.loads(Path(path).read_text(encoding="utf-8")))


def configured_database() -> Path:
    from app.core.config import settings
    from app.mention_receiver.catalog import configured_database as resolve
    return resolve(settings.database_url, Path(__file__).resolve().parents[2])


def open_store(path: Path, database: Path) -> AgentStore:
    from app.core.config import settings
    backend = Path(__file__).resolve().parents[2]
    return AgentStore(path, protected_paths=(database, backend / settings.chroma_persist_dir,
        backend / settings.playwright_user_data_dir, backend / "app/storage/mention_receiver"))


class AgentService:
    def __init__(self, store: AgentStore, binding: Binding, *, knowledge=None, model=None, database=None,
                 allow_import=False, import_factory=None, import_should_stop=None,
                 scope_authorizer=None, explicit_save_check=None, import_wait_seconds=None,
                 allowed_library_names=(), conversation_context=None, allow_private_execution=False,
                 automatic_import_receipt=False, usage_budget=None, trusted_save_selection=None,
                 budget_scope_id=None, budget_scope_linker=None, trusted_semantic_decision=None,
                 trusted_semantic_grant_id=None, trusted_semantic_owner_scope=None, reply_prefix=''):
        if type(allow_import) is not bool:
            raise ValueError("视频入库开关必须明确设置为布尔值")
        if binding.is_all_local and allow_import:
            raise ValueError('import_all_scope_forbidden')
        if import_factory is not None and not callable(import_factory):
            raise ValueError("视频入库工具工厂无效")
        if import_should_stop is not None and not callable(import_should_stop):
            raise ValueError("视频入库停止回调无效")
        self.store, self.binding = store, binding
        self.store.put_binding(binding.to_dict())
        scope_options={'query_scope':'all_local'} if binding.is_all_local else {}
        self.knowledge = knowledge or LocalKnowledgeTools(database or configured_database(), binding.knowledge_base_id, **scope_options)
        self.model = model or DeepSeekToolModel()
        self.allow_import = allow_import
        self.import_factory = import_factory
        self.import_should_stop = import_should_stop
        for checker in (scope_authorizer, explicit_save_check):
            if checker is not None and not callable(checker):
                raise ValueError("授权检查器无效")
        self.scope_authorizer = scope_authorizer
        self.explicit_save_check = explicit_save_check
        self.import_wait_seconds = import_wait_seconds
        self.allowed_library_names = allowed_library_names
        self.conversation_context = conversation_context
        if type(allow_private_execution) is not bool:
            raise ValueError("私有执行开关无效")
        self.allow_private_execution = allow_private_execution
        if type(automatic_import_receipt) is not bool:
            raise ValueError("自动回执开关无效")
        self.automatic_import_receipt = automatic_import_receipt
        self.reply_prefix = normalize_reply_prefix(reply_prefix)
        self.usage_budget = usage_budget
        self.trusted_save_selection = trusted_save_selection
        # Only the internal Bridge supplies this from a typed semantic receipt.
        # Envelope/HTTP fields never select a budget bucket. A durable trusted
        # link must succeed after obtaining the actual AgentTask, before any
        # provider reservation; a bare caller string is insufficient.
        if ((budget_scope_id is None) != (budget_scope_linker is None)
                or (budget_scope_id is not None and (type(budget_scope_id) is not str
                    or not 1 <= len(budget_scope_id) <= 240 or not callable(budget_scope_linker)))):
            raise ValueError('trusted_budget_scope_required')
        self.budget_scope_id, self.budget_scope_linker = budget_scope_id, budget_scope_linker
        self.trusted_semantic_decision = trusted_semantic_decision
        self.trusted_semantic_grant_id = trusted_semantic_grant_id
        self.trusted_semantic_owner_scope = trusted_semantic_owner_scope

    def _binding_now(self):
        value = self.store.get_binding(self.binding.binding_id)
        if not value:
            return None
        current = Binding.from_dict(value)
        if self.scope_authorizer is not None and self.scope_authorizer(current) is not True:
            return None
        return current

    def process(self, envelope: dict, *, video_title=None, query_retry_action_id=None) -> dict:
        envelope = validate_envelope(envelope, self.binding)
        if envelope.get('evidence', {}).get('source_kind') == 'conversation_followup' and self.allow_import:
            return {'task_id':None,'state':'needs_review','reason_code':'followup_query_only',
                    'created':False,'ran_model':False}
        if self._binding_now() != self.binding:
            return {"task_id": None, "state": "needs_review", "reason_code": "binding_changed",
                    "created": False, "ran_model": False}
        if query_retry_action_id is not None:
            if self.allow_import:
                raise ValueError('query_retry_import_forbidden')
            task = self.store.lookup_existing(self.binding.binding_id, envelope)
            if task is None:
                return {'task_id':None,'state':'needs_review','reason_code':'query_retry_not_safe',
                        'created':False,'ran_model':False}
            retry = self.store.get_query_retry(task['task_id'])
            if retry is None or retry['state']!='armed' or retry['action_id']!=query_retry_action_id:
                return self._result(task['task_id'],created=False,ran_model=False,reason_code='query_retry_not_safe')
            created = False
        else:
            before = {row["task_id"] for row in self.store.list_tasks(self.binding.binding_id)}
            task = self.store.enqueue(self.binding.binding_id, envelope)
            created = task["task_id"] not in before
        claim_options = {'query_retry_action_id':query_retry_action_id} if query_retry_action_id is not None else {}
        claim = self.store.claim(task["task_id"], lease_seconds=1200 if self.allow_import else 300, **claim_options)
        if claim is None:
            return self._result(task["task_id"], created=created, ran_model=False)
        token, task_id = claim["claim_token"], task["task_id"]
        model = self.model
        rounds = calls = request_attempts = responses = 0
        def record(kind, metadata):
            nonlocal request_attempts, responses, calls
            if kind in {'model_request', 'model_response'} and self.usage_budget is not None:
                metadata = {**metadata, 'budget_request_id': model.request_key}
            self.store.record_step(task_id, token, kind, metadata)
            if kind == "model_request":
                request_attempts += 1
            elif kind == "model_response":
                responses += 1
            elif kind == "tool_call":
                calls += 1
        try:
            if self.budget_scope_id is not None:
                if self.budget_scope_linker(task_id, envelope, self.binding, self.budget_scope_id) is not True:
                    raise ValueError('trusted_budget_scope_required')
            if self.usage_budget is not None:
                from app.assistant.usage_model import BudgetedModel
                model = BudgetedModel(model, self.usage_budget, self.budget_scope_id or task_id)
            engine_options = {'reply_prefix': self.reply_prefix}
            if self.trusted_semantic_decision is not None:
                engine_options.update(trusted_semantic_decision=self.trusted_semantic_decision,
                    trusted_semantic_grant_id=self.trusted_semantic_grant_id,
                    trusted_semantic_owner_scope=self.trusted_semantic_owner_scope,
                    trusted_semantic_agent_task_id=task_id)
            if self.trusted_save_selection is not None:
                engine_options['trusted_save_selection']=self.trusted_save_selection
            if self.conversation_context is not None:
                engine_options["conversation_context"] = self.conversation_context
            if self.allowed_library_names:
                engine_options["allowed_library_names"] = self.allowed_library_names
            if self.allow_private_execution:
                engine_options["allow_private_execution"] = True
            if self.automatic_import_receipt:
                engine_options["automatic_import_receipt"] = True
            if isinstance(video_title,str) and video_title.strip():
                engine_options["current_video"] = {"id":envelope["video_id"],"title":video_title.strip()[:512]}
            if self.allow_import:
                description = self.knowledge.describe()
                if (not isinstance(description, dict) or type(description.get("id")) is not int
                        or description["id"] != self.binding.knowledge_base_id
                        or not isinstance(description.get("name"), str) or not description["name"].strip()
                        or len(description["name"]) > 128):
                    raise ValueError("library_scope_mismatch")
                factory = self.import_factory
                if factory is None:
                    from .imports import ImportTools
                    factory = ImportTools
                import_options = {"should_stop": self.import_should_stop or (lambda: False)}
                if self.explicit_save_check is not None:
                    import_options["explicit_save_check"] = self.explicit_save_check
                if self.import_wait_seconds is not None:
                    import_options["wait_seconds"] = self.import_wait_seconds
                if self.scope_authorizer is not None:
                    import_options["scope_authorizer"] = self.scope_authorizer
                if isinstance(video_title, str) and video_title.strip():
                    import_options["video_title"] = video_title.strip()[:512]
                engine_options["ingestion"] = factory(
                    self.store, self.binding, task_id, token, envelope, description["name"],
                    **import_options,
                )
            result = AgentEngine(model, self.knowledge, **engine_options).run(
                self.binding, envelope,
                record=record,
                current_binding=self._binding_now,
            )
            rounds, calls = result.model_rounds, result.tool_calls
            if result.proposal is None:
                self.store.needs_review(task_id, token, result.review_code or "no_reply_proposal")
            else:
                self.store.complete(task_id, token, result.proposal, reply_prefix=self.reply_prefix)
        except Exception as error:
            # Never persist provider error strings: they can contain URLs,
            # headers or model input. An uncertain request is not retried.
            code = ("knowledge_query_failed" if isinstance(error, ToolExecutionFailure)
                    else "model_response_incomplete" if isinstance(error, IncompleteModelResponse)
                    else "model_or_tool_failed")
            try:
                from app.assistant.runtime_diagnostics import safe_failure_chain
                status = getattr(error, "status_code", None)
                self.store.record_step(task_id, token, "error", {"code": code, "error_type": type(error).__name__,
                    "http_status": status if type(status) is int else None,
                    "failure_chain": safe_failure_chain(error)})
                self.store.needs_review(task_id, token, code)
            except AgentStoreError:
                # Revocation or a lease fence has already quarantined the task.
                pass
        return self._result(task_id, created=created, processing_attempted=True, ran_model=request_attempts > 0,
                            model_rounds=rounds, tool_calls=calls, model_request_attempts=request_attempts,
                            completed_model_responses=responses)

    def _result(self, task_id, **metadata):
        task = self.store.get_task(task_id)
        reply = self.store.get_reply(task_id)
        receipt = ({"import_job_id": reply["import_job_id"], "import_state": reply["import_state"]}
                   if reply and reply["kind"] == "import_status" else {})
        return {"task_id": task_id, "state": task["state"], "reason_code": task.get("reason_code"),
                **metadata, "reply": ({"state": reply["state"], "text": reply["text"],
                                       "source_ids": reply["source_ids"], "kind": reply["kind"],
                                       "sources": reply["sources"], **receipt} if reply else None)}


class ReceiverHandler:
    def __init__(self, service: AgentService, max_tasks: int = 1, required_marker: str | None = None):
        if type(max_tasks) is not int or not 1 <= max_tasks <= 20:
            raise ValueError("单次模型任务数量应为1至20")
        self.service, self.max_tasks = service, max_tasks
        self.processed_count = 0
        if required_marker is not None and (not isinstance(required_marker, str) or not 1 <= len(required_marker.strip()) <= 80):
            raise ValueError("测试标记应为1至80个字符")
        self.required_marker = required_marker.strip() if required_marker is not None else None

    @property
    def exhausted(self):
        return self.processed_count >= self.max_tasks

    def accepts_notice(self, notice):
        return (not self.exhausted and should_receive_notice(notice, self.service.binding)
                and (self.required_marker is None or self.required_marker in notice["exact_text"]))

    def handle_detail(self, notice, detail):
        if self.exhausted:
            return {"created": False, "task_id": None, "state": "model_task_limit"}
        if not self.accepts_notice(notice):
            return {"created": False, "task_id": None, "state": "notice_not_selected"}
        envelope = make_envelope(notice, detail, self.service.binding)
        result = (self.service.process(envelope, video_title=detail.get("video_title"))
                  if getattr(self.service, "allow_import", False) else self.service.process(envelope))
        if result.get("processing_attempted", False):
            self.processed_count += 1
        return result


def build_receiver_handler(*, binding_path, state_path, max_tasks=1, required_marker=None, allow_import=False):
    if not isinstance(required_marker, str) or not 1 <= len(required_marker.strip()) <= 80:
        raise ValueError("当前智能收件测试必须明确指定新指令标记")
    if type(allow_import) is not bool or (allow_import and (type(max_tasks) is not int or max_tasks != 1)):
        raise ValueError("视频入库测试需要明确开关与单任务上限")
    database = configured_database()
    binding = read_binding(Path(binding_path))
    store = open_store(Path(state_path), database)
    options = {"allow_import": True} if allow_import else {}
    return ReceiverHandler(AgentService(store, binding, database=database,
        scope_authorizer=lambda _:not legacy_testing_blocked(), **options), max_tasks, required_marker)
