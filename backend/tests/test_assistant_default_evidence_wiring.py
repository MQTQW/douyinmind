"""Default daily-bridge transport wiring; fake model/evidence and temp ledgers only."""
import re
import json

import pytest

from app.assistant import legacy_bridge
from app.assistant.runtime import AssistantRuntime
from tests.test_assistant_product_bridge import system, receive
from tests.test_assistant_legacy_extensions import QueryKnowledge, QueryModel


@pytest.mark.parametrize('focus', [False, True])
def test_default_bridge_captures_original_task_claim_and_keeps_current_video_focus(tmp_path, monkeypatch, focus):
    binding, agent, env, registry, migrated, queue, history, control = system(tmp_path)
    observed = []

    class Backend:
        def __init__(self, database, knowledge_base_id, task_id, focus_video_id=None, *, execution_token,
                     query_scope='library'):
            current = queue.get_task(task_id)
            assert current['state'] == 'executing'
            assert re.fullmatch(r'[a-f0-9]{48}', execution_token)
            assert execution_token == current['execution_token']
            assert query_scope == 'library' and knowledge_base_id == 7
            observed.append({'task_id': task_id, 'knowledge_base_id': knowledge_base_id,
                'focus_video_id': focus_video_id, 'claim_matches_original': True})
            self.local_fake = QueryKnowledge()

        def describe(self):
            return self.local_fake.describe()

        def query(self, question):
            return self.local_fake.query(question)

    monkeypatch.setattr(legacy_bridge, 'BackendKnowledgeTools', Backend)
    # If a future bridge silently falls back to local retrieval, the test fails
    # before a real embedding/index can be initialized.
    from app.mention_agent.knowledge import LocalKnowledgeTools
    monkeypatch.setattr(LocalKnowledgeTools, 'query', lambda *a, **k: pytest.fail('helper must not query its own index'))
    body = '@助手 请根据编程知识库说明这条视频中的Git。' if focus else '@助手 请根据编程知识库说明Git。'
    task = receive(queue, history, registry, {**env, 'comment_text': body})
    bridge = legacy_bridge.LegacyBridge(registry, queue, control, agent_path=tmp_path/'agent.sqlite3',
        conversation=history, database=tmp_path/'unused-business.db', model_factory=QueryModel)
    runtime = AssistantRuntime(queue, control, execute=bridge.execute, reconcile=bridge.reconcile)
    result = runtime.execute_once()
    assert result['state'] == 'ready_to_publish'
    assert observed == [{'task_id': task['id'], 'knowledge_base_id': 7,
        'focus_video_id': env['video_id'] if focus else None, 'claim_matches_original': True}]
    assert not (tmp_path/'unused-business.db').exists()


def test_default_bridge_uses_explicit_all_scope_and_original_claim_for_unspecified_query(tmp_path, monkeypatch):
    binding, agent, env, registry, migrated, queue, history, control = system(tmp_path)
    registry.set_local_owner_access(migrated['user_id'], migrated['device_id'], query_all_local=True,
        catalog_read_allowed=False, public_reply_allowed=True, verified_by='synthetic-owner')
    history.prepare_scope_v2()
    observed = []

    class Backend:
        def __init__(self, database, knowledge_base_id, task_id, focus_video_id=None, *, execution_token,
                     query_scope='library'):
            current = queue.get_task(task_id)
            assert current['state'] == 'executing' and execution_token == current['execution_token']
            assert knowledge_base_id is None and query_scope == 'all_local'
            assert current['authorization']['query_scope'] == 'all_local'
            observed.append(task_id)
        def describe(self):
            return {'id':None, 'name':'全部入库资料', 'total_count':1, 'ready_count':1, 'query_scope':'all_local'}
        def query(self, question):
            result = QueryKnowledge().query(question)
            return {**result, 'knowledge_base_id':None, 'knowledge_base_name':'全部入库资料', 'query_scope':'all_local'}

    class AllModel(QueryModel):
        def complete(self, messages, tools):
            query_tool = next(item['function'] for item in tools if item['function']['name'] == 'query_knowledge_base')
            assert set(query_tool['parameters']['properties']) == {'question'}
            turn = super().complete(messages, tools)
            if len(self.requests) == 1:
                call = turn.message['tool_calls'][0]['function']
                args = json.loads(call['arguments'])
                args.pop('knowledge_base_id')
                call['arguments'] = json.dumps(args)
            return turn

    monkeypatch.setattr(legacy_bridge, 'BackendKnowledgeTools', Backend)
    from app.mention_agent.knowledge import LocalKnowledgeTools
    monkeypatch.setattr(LocalKnowledgeTools, 'query', lambda *a, **k: pytest.fail('helper must not open index'))
    task = receive(queue, history, registry, {**env, 'comment_text':'@助手 Git的工作区是什么？'})
    assert task['binding']['knowledge_base_id'] is None and task['binding']['query_scope'] == 'all_local'
    model = AllModel()
    bridge = legacy_bridge.LegacyBridge(registry, queue, control, agent_path=tmp_path/'agent.sqlite3',
        conversation=history, database=tmp_path/'unused-business.db', model_factory=lambda:model)
    result = AssistantRuntime(queue, control, execute=bridge.execute, reconcile=bridge.reconcile).execute_once()
    assert result['state'] == 'ready_to_publish' and observed == [task['id']]
    assert len(model.requests) == 2 and result['details']['library_name'] == '全部入库资料'
    assert not (tmp_path/'unused-business.db').exists()
