"""Production bridge authority hooks, temporary ledgers and fake tools only."""
from app.assistant.legacy_bridge import LegacyBridge
from app.assistant.runtime import AssistantRuntime
from tests.test_assistant_integration_review import system, receive, QueryModel, QueryKnowledge, Adapter


def bridge_case(tmp_path, guard, *, model=None, adapter=None):
    binding, agent, env, registry, migrated, queue, history, control = system(tmp_path)
    model, adapter, knowledge = model or QueryModel(), adapter or Adapter(), QueryKnowledge()
    task = receive(queue, history, registry, {**env, 'comment_text': '@助手 请根据编程知识库说明Git。'})
    bridge = LegacyBridge(registry, queue, control, agent_path=tmp_path / 'agent.sqlite3',
        conversation=history, database=tmp_path / 'unused-business.db', model_factory=lambda: model,
        knowledge_factory=lambda _: knowledge, adapter=adapter, source_authorizer=guard)
    runtime = AssistantRuntime(queue, control, execute=bridge.execute, publish=bridge.publish,
                               reconcile=bridge.reconcile)
    return runtime, task, queue, agent, model, adapter, knowledge


def test_native_conflict_before_execution_blocks_model_tools_and_sender(tmp_path):
    runtime, task, queue, agent, model, adapter, knowledge = bridge_case(tmp_path, lambda _: False)
    result = runtime.execute_once()
    assert result['state'] == 'needs_review' and result['reason_code'] == 'binding_changed'
    assert model.requests == [] and knowledge.queries == [] and adapter.submits == 0
    assert runtime.publish_once() is None


def test_native_conflict_after_model_choice_blocks_the_selected_tool(tmp_path):
    allowed = [True]
    class ConflictAfterModel(QueryModel):
        def complete(self, messages, tools):
            turn = super().complete(messages, tools)
            allowed[0] = False
            return turn
    runtime, _, _, _, model, adapter, knowledge = bridge_case(tmp_path, lambda _: allowed[0], model=ConflictAfterModel())
    assert runtime.execute_once()['state'] == 'needs_review'
    assert len(model.requests) == 1 and knowledge.queries == [] and adapter.submits == 0


def test_native_guard_failure_is_closed_without_exception_text_in_result(tmp_path):
    def unavailable(_):
        raise RuntimeError('synthetic-private-credential-value')
    runtime, _, _, _, model, adapter, knowledge = bridge_case(tmp_path, unavailable)
    result = runtime.execute_once()
    assert result['state'] == 'needs_review'
    assert 'synthetic-private-credential-value' not in str(result)
    assert model.requests == [] and knowledge.queries == [] and adapter.submits == 0


def test_native_conflict_after_prepare_preserves_draft_without_send_reservation(tmp_path):
    allowed = [True]
    class ConflictAfterPrepare(Adapter):
        prepares = 0
        def prepare_reply(self, target, text):
            self.prepares += 1
            result = super().prepare_reply(target, text)
            allowed[0] = False
            return result
    runtime, _, _, agent, model, adapter, knowledge = bridge_case(tmp_path,
        lambda _: allowed[0], adapter=ConflictAfterPrepare())
    assert runtime.execute_once()['state'] == 'ready_to_publish'
    result = runtime.publish_once()
    assert result['state'] == 'needs_review'
    reply = agent.get_reply(result['agent_task_id'])
    assert reply['text'] and reply['send_token'] is None
    assert adapter.prepares == 1 and adapter.submits == 0
