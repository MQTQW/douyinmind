"""Only a real same-thread private parent can share the original command cap."""
from copy import deepcopy

import pytest

from app.assistant.semantic_plan_store import SemanticPlanStore,SemanticPlanError,decision_metadata
from app.assistant.semantic_intent_model import INTENT_TOOLS,intent_messages
from app.assistant.usage_budget import UsageBudget
from tests.test_semantic_planning_store import case,run,OWNER,CATALOG,TEXT,Model,Budget
from tests.test_native_semantic_inbox_and_binding import (
    case as native_case, message,understand,bind,scope_for,
)


def choice_run(store,model,budget,owner,parent=None,source='native:choice-budget',**changes):
    options=dict(source_key=source,frozen_ui_digest='a'*64,owner_scope=owner,catalog_snapshot=deepcopy(CATALOG),
        mode='choice',original_text=TEXT,model=model,budget=budget,scope_authorizer=lambda:True,should_stop=lambda:False,
        budget_parent_receipt=parent)
    options.update(changes)
    return store.run_once(**options)


def test_native_root_parent_keeps_same_scope_for_ordinary_reply_child(native_case):
    inbox,store,clock,allowed,model,budget=native_case;clock[0]=103;value=message()
    origin=understand(native_case,value);parent=bind(native_case,origin,value)
    owner={**scope_for(value,actual_ui=True),'reference_id':'reference:ordinary-choice'}
    child=choice_run(store,model,budget,owner,parent)
    assert child.budget_scope_id==origin.budget_scope_id==parent.budget_scope_id
    assert child.plan_id!=parent.plan_id
    with store._connection() as db:
        row=db.execute('SELECT * FROM semantic_budget_parents WHERE plan_id=?',(child.plan_id,)).fetchone()
    assert row['parent_plan_id']==parent.plan_id and row['budget_scope_id']==parent.budget_scope_id
    assert child.decision=={'action':'save','target_state':'one','grant_id':'grant-one'}


def test_parent_consumed_fee_is_counted_against_child_admission_cap(native_case,tmp_path):
    inbox,store,clock,allowed,model,_budget=native_case;clock[0]=103;value=message()
    budget=UsageBudget.prepare(tmp_path/'usage-budget.sqlite3')
    origin=understand(native_case,value,budget=budget);parent=bind(native_case,origin,value)
    owner={**scope_for(value,actual_ui=True),'reference_id':'reference:ordinary-choice'}
    import json
    payload={'messages':intent_messages('choice',TEXT,CATALOG),'tools':INTENT_TOOLS}
    estimate=len(json.dumps(payload,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode())+1500+2048
    budget.set_limits(per_task_tokens=estimate)
    child_model=Model()
    with pytest.raises(SemanticPlanError,match='budget_exceeded'):choice_run(store,child_model,budget,owner,parent)
    assert child_model.calls==0
    totals=budget.summary(parent.budget_scope_id)['task']
    assert totals['actual_tokens']==20 and totals['request_count']==1


@pytest.mark.parametrize('field,value',[
    ('user_id','foreign-user'),('device_id','foreign-device'),('instance_id','foreign-instance'),
    ('video_id','7999999000000000002'),('root_reference_id','reference:wrong-root'),
])
def test_wrong_owner_or_root_parent_rejected_before_paid_request(case,field,value):
    store,model,budget=case;parent=run(store,model,budget)
    owner={**OWNER,'reference_id':'reference:choice',field:value}
    with pytest.raises(SemanticPlanError,match='budget_parent_invalid'):choice_run(store,model,budget,owner,parent)
    assert model.calls==1 and len(budget.calls)==1


def test_dict_or_native_origin_cannot_be_used_as_ui_parent(native_case):
    inbox,store,clock,allowed,model,budget=native_case;clock[0]=103;value=message();origin=understand(native_case,value)
    owner={**scope_for(value,actual_ui=True),'reference_id':'reference:choice'}
    with pytest.raises(SemanticPlanError):choice_run(store,model,budget,owner,decision_metadata(origin))
    with pytest.raises(SemanticPlanError):choice_run(store,model,budget,owner,origin)
    assert model.calls==1


def test_nonchoice_cannot_inherit_and_none_legacy_path_still_works(case):
    store,model,budget=case;parent=run(store,model,budget)
    with pytest.raises(SemanticPlanError):run(store,model,budget,source_key='native:other-root',budget_parent_receipt=parent)
    owner={**OWNER,'reference_id':'reference:legacy-choice'}
    child=choice_run(store,model,budget,owner,None)
    assert child.budget_scope_id==parent.budget_scope_id
    assert choice_run(store,model,budget,owner,None).plan_id==child.plan_id
    assert model.calls==2


def test_adding_or_changing_parent_of_existing_choice_is_scope_conflict(case):
    store,model,budget=case;parent=run(store,model,budget)
    other=run(store,model,budget,source_key='native:second-parent-same-ui')
    owner={**OWNER,'reference_id':'reference:choice'}
    choice_run(store,model,budget,owner,parent)
    with pytest.raises(SemanticPlanError,match='scope_conflict'):choice_run(store,model,budget,owner,other)
    with pytest.raises(SemanticPlanError,match='scope_conflict'):choice_run(store,model,budget,owner,None)
    assert model.calls==3
