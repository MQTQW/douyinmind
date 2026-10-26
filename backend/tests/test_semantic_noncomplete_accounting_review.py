"""Accounting-only failure reproduction; temporary stores and a fake planner."""
import json

import pytest

from app.assistant.commands import CommandResolver
from app.assistant.legacy_bridge import LegacyBridge
from app.assistant.semantic_plan_store import SemanticPlanStore, SemanticPlanError, ui_semantic_digest
from app.assistant.semantic_resolution import (catalog_snapshot_from_grants, owner_scope_from_envelope,
                                               semantic_source_key)
from app.assistant.usage_budget import UsageBudget
from app.mention_agent.model import ModelTurn
from tests.test_assistant_product_bridge import system


def test_post_provider_scope_loss_keeps_settled_cost_visible_without_decision(tmp_path):
    """A failure must lose execution permission, not already measured billing."""
    binding,agent,env,registry,migrated,queue,history,control=system(tmp_path)
    env['comment_text']='@助手 根据编程知识库解释Git。'
    # This is a display-only task fixture, not a real native/UI admission.
    resolution=CommandResolver(registry).resolve(env)
    task=queue.enqueue(resolution.binding,env,kind=resolution.action,user_id=resolution.user_id,
        device_id=resolution.device_id,authorization=resolution.authorization)
    history.bind_task(task['id'],resolution.binding,env,env['reference_id'],
        {'quality':'current_comment','root_reference_id':env['reference_id']})
    snapshot=registry.snapshot();user=next(u for u in snapshot['users'] if u['user_id']==migrated['user_id'])
    device=next(d for d in user['devices'] if d['device_id']==migrated['device_id'])
    grants=[g for g in device['libraries'] if g.get('active') and g.get('library_exists')]
    budget=UsageBudget.prepare(tmp_path/'usage-budget.sqlite3');semantic=SemanticPlanStore.prepare(tmp_path/'semantic.sqlite3')
    active=[True]
    class Planner:
        calls=0
        def complete(self,*_):
            self.calls+=1;active[0]=False
            return ModelTurn({'role':'assistant','content':None,'tool_calls':[{'id':'failure-cost','type':'function',
                'function':{'name':'submit_intent','arguments':json.dumps({'action':'query','target_state':'one',
                    'grant_id':migrated['grant_id']})}}]}, {'prompt_tokens':12,'completion_tokens':8,'total_tokens':20},'tool_calls')
    planner=Planner();source=semantic_source_key(env)
    with pytest.raises(SemanticPlanError,match='scope_unavailable'):
        semantic.run_once(source_key=source,frozen_ui_digest=ui_semantic_digest(env),
            owner_scope=owner_scope_from_envelope(env,user,device),catalog_snapshot=catalog_snapshot_from_grants(grants),
            mode='root',original_text=env['comment_text'],model=planner,budget=budget,
            scope_authorizer=lambda:active[0],should_stop=lambda:False)
    assert planner.calls==1 and budget.summary()['all_time']['actual_tokens']==20
    assert semantic.lookup_receipt(source)['state']=='unknown'
    assert semantic.get_recorded_decision(env,binding=resolution.binding) is None
    bridge=LegacyBridge(registry,queue,control,agent_path=tmp_path/'agent.sqlite3',conversation=history,
        database=tmp_path/'unused-business.db',usage_budget=budget,semantic_store=semantic,
        model_factory=lambda:pytest.fail('accounting observation must not construct a final model'))
    observed=bridge._snapshot(task)
    # Desired invariant; currently exposes the non-complete accounting gap.
    assert observed.get('usage',{}).get('total_tokens')==20
    assert observed.get('safe_to_execute') is False
