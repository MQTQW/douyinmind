"""Product bridge prerequisites, with isolated ledgers and fake providers."""
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timezone
import json

import pytest

from app.assistant.conversation import ConversationStore
from app.mention_agent.engine import AgentEngine
from app.mention_agent.imports import ImportTools, import_status_proposal
from app.mention_agent.model import ModelTurn
from app.mention_agent.publisher import ReplyPublisher
from app.mention_agent.service import AgentService
from app.mention_agent.store import AgentStoreError
from tests.test_mention_agent_import_end_to_end import scope, Backend, Model, Knowledge, Adapter


class QueryKnowledge:
    def __init__(self):
        self.queries = []

    def describe(self):
        return {"id": 7, "name": "测试库", "total_count": 1, "ready_count": 1}

    def query(self, question):
        self.queries.append(question)
        return {"status": "ok", "knowledge_base_id": 7, "knowledge_base_name": "测试库", "evidence": [{
            "source_id": "资料1", "platform_item_id": "7999999000000000011", "chunk_id": "source:1",
            "title": "虚构转写", "url": "https://www.douyin.com/video/7999999000000000011",
            "text": "[00:17-00:55] 工作区编辑文件，暂存区选择要提交的文件。", "time_range": "00:17–00:55"}]}


class QueryModel:
    def __init__(self):
        self.requests = []

    def complete(self, messages, tools):
        self.requests.append(deepcopy(messages))
        name, args = (("query_knowledge_base", {"knowledge_base_id": 7, "question": "第二点有什么区别？"})
                      if len(self.requests) == 1 else
                      ("propose_reply", {"text": "工作区编辑文件，暂存区选择要提交的文件。", "source_ids": ["资料1"]}))
        return ModelTurn({"role": "assistant", "content": None, "tool_calls": [{"id": "q" + str(len(self.requests)),
            "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}]}, {}, "tool_calls")


def test_revoked_scope_blocks_before_enqueue_or_model(tmp_path):
    binding, store, env = scope(tmp_path)
    model = QueryModel()
    result = AgentService(store, binding, knowledge=QueryKnowledge(), model=model,
                          scope_authorizer=lambda _: False).process(env)
    assert result["reason_code"] == "binding_changed"
    assert not store.list_tasks() and not model.requests


def test_thread_reference_is_not_used_as_factual_evidence(tmp_path):
    binding, store, env = scope(tmp_path)
    env["comment_text"] = '@助手 根据“编程”知识库，第二点有什么区别？'
    knowledge, model = QueryKnowledge(), QueryModel()
    context = {"prior_questions": ["请说明Git的四个区域"], "previous_answer": "第二点是暂存区；历史内容仅用于确定指代。"}
    result = AgentService(store, binding, knowledge=knowledge, model=model,
        allowed_library_names=("编程",), conversation_context=context).process(env)
    assert result["state"] == "answer_ready"
    assert "Git的四个区域" in knowledge.queries[0]
    assert "不是本轮事实依据" in model.requests[0][0]["content"]
    assert result["reply"]["sources"][0]["time_range"] == "00:17–00:55"
    assert result["reply"]["source_ids"] == ["资料1"]


def test_alias_permission_is_explicit_not_any_named_library(tmp_path):
    binding, store, env = scope(tmp_path)
    env["comment_text"] = '@助手 根据“别人的库”知识库，第二点是什么？'
    decision = AgentEngine(QueryModel(), QueryKnowledge(), allowed_library_names=("编程",)).run(binding, env)
    assert decision.proposal is None


def prepared_import(tmp_path, *, text=None, checker=None, backend=None):
    binding, store, env = scope(tmp_path)
    if text:
        env["comment_text"] = text
    backend = backend or Backend()
    model = Model([("save_current_video", {"knowledge_base_id": 7}), ("propose_import_status", {})])
    service = AgentService(store, binding, knowledge=Knowledge(), model=model, allow_import=True,
        import_wait_seconds=0, explicit_save_check=checker,
        import_factory=lambda *a, **k: ImportTools(*a, **k, client=backend))
    result = service.process(env)
    return binding, store, env, backend, model, result


def test_natural_save_requires_program_authorization_and_keeps_original_text(tmp_path):
    original = "@助手 存到编程库"
    binding, store, env, backend, model, result = prepared_import(tmp_path, text=original, checker=lambda e, b: e["comment_text"] == original)
    assert result["state"] == "answer_ready" and backend.posts == 1
    assert store.get_reply(result["task_id"])["target"]["comment_text"] == original
    assert backend.body["video_id"] == env["video_id"]


def test_changed_explicit_save_authorization_never_dispatches(tmp_path):
    checks = []
    def check(*_):
        checks.append(True)
        return len(checks) == 1
    binding, store, env, backend, model, result = prepared_import(tmp_path, text="@助手 存到编程库", checker=check)
    assert backend.posts == 0
    assert store.get_task_ingestion(result["task_id"])["state"] == "failed_no_submit"
    assert result["reply"]["kind"] == "import_status"


def test_processing_receipt_can_be_refreshed_without_model_or_second_import(tmp_path):
    class PendingBackend(Backend):
        state = "processing"
        def _response(self):
            return dict(self.body, state=self.state, membership_exists=True,
                cache_status="done" if self.state == "succeeded" else "transcribing",
                indexed_content_chunks=3 if self.state == "succeeded" else 0)
    backend = PendingBackend()
    binding, store, env, backend, model, result = prepared_import(tmp_path, backend=backend)
    task_id = result["task_id"]
    assert result["reply"]["import_state"] == "processing"
    assert backend.posts == 1 and model.count == 2
    job = store.get_task_ingestion(task_id)
    backend.state = "succeeded"
    response = backend.status(job["job_id"])
    observation = {("backend_request_id" if k == "request_id" else "backend_state" if k == "state" else k):v for k,v in response.items()}
    observed = store.observe_ingestion(job["job_id"], observation)
    prior = store.get_reply(task_id)
    reply = store.refresh_unsent_import_reply(task_id, import_status_proposal(observed, "测试库"))
    assert reply["text"] != prior["text"] and reply["target"] == prior["target"]
    assert reply["created_at"] == prior["created_at"] and not reply["send_token"]
    assert len(store.list_import_reply_updates(task_id)) == 1
    assert backend.posts == 1 and model.count == 2
    store.refresh_unsent_import_reply(task_id, import_status_proposal(observed, "测试库"))
    assert len(store.list_import_reply_updates(task_id)) == 1
    assert ReplyPublisher(store, binding, Adapter(), tmp_path/'reply-evidence').publish(task_id)["state"] == "b_view_visible"
    with pytest.raises(AgentStoreError, match="reply_not_unsent_ready"):
        store.refresh_unsent_import_reply(task_id, import_status_proposal(observed, "测试库"))


def test_revoke_before_publication_blocks_browser_access(tmp_path):
    binding, store, env, backend, model, result = prepared_import(tmp_path)
    class NoBrowser:
        def __getattr__(self, name):
            raise AssertionError("must not access browser after scope revocation")
    outcome = ReplyPublisher(store, binding, NoBrowser(), tmp_path/'reply-evidence', scope_authorizer=lambda _:False).publish(result["task_id"])
    assert outcome["state"] == "preflight_blocked"
    assert not store.get_reply(result["task_id"])["send_token"]


def test_context_isolated_by_binding_device_library_video_and_thread(tmp_path):
    binding, store, env = scope(tmp_path)
    history = ConversationStore(tmp_path/'contexts.sqlite3')
    history.bind_task("old", binding, env, env["reference_id"], {"quality":"current_comment", "root_reference_id":env["reference_id"]})
    reply = {"state":"b_view_visible", "kind":"answer", "text":"第一点工作区；第二点暂存区。", "sources":[]}
    assert history.record_published("old", binding, reply)
    assert not history.record_published("old", binding, reply)
    followup = {**env, "command_id":"followup", "reference_id":"child", "comment_text":"@助手 第二点呢？"}
    history.bind_task("child", binding, followup, env["reference_id"], {"quality":"verified_ancestor", "root_reference_id":env["reference_id"]})
    assert history.context_for("child", binding)["previous_answer"] == reply["text"]
    for alternate in (replace(binding,binding_id="other"), replace(binding,instance_id="other"),
                      replace(binding,knowledge_base_id=8), replace(binding,sender_profile_url="https://www.douyin.com/user/other")):
        assert history.context_for("child", alternate) is None
    unrelated = {**followup, "command_id":"other-root", "reference_id":"other-root"}
    history.bind_task("other-root", binding, unrelated, unrelated["reference_id"], {"quality":"current_comment", "root_reference_id":unrelated["reference_id"]})
    assert history.context_for("other-root", binding) is None
    other_video = {**followup, "command_id":"other-video", "reference_id":"other-video", "video_id":"7999999000000000022"}
    history.bind_task("other-video", binding, other_video, env["reference_id"], {"quality":"independent", "root_reference_id":env["reference_id"]})
    assert history.context_for("other-video", binding) is None


def test_unsent_or_uncertain_answer_is_not_thread_history(tmp_path):
    binding, store, env = scope(tmp_path)
    history = ConversationStore(tmp_path/'contexts.sqlite3')
    history.bind_task("one", binding, env, env["reference_id"], {"quality":"current_comment", "root_reference_id":env["reference_id"]})
    for state in ("ready", "delivery_unknown", "verification_required"):
        assert not history.record_published("one", binding, {"state":state,"kind":"answer","text":"不能当已送达历史","sources":[]})
    assert history.roots(binding, env["video_id"]) == []


def test_foreign_history_database_is_never_migrated(tmp_path):
    path = tmp_path/'foreign.sqlite3'
    path.write_bytes(b"foreign private data")
    before = path.read_bytes()
    with pytest.raises(ValueError, match="foreign_conversation_database"):
        ConversationStore(path)
    assert path.read_bytes() == before
