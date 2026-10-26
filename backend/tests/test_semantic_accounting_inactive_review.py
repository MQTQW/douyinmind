"""A revoked Binding can account old usage, never authorize a fresh request."""
from dataclasses import replace

import pytest

from app.assistant.semantic_plan_store import SemanticPlanError
from tests.test_semantic_planning_store import case,run,ENV,BINDING


def linked(case):
    store,model,budget=case;receipt=run(store,model,budget)
    store.bind_agent_budget(receipt,'synthetic-agent',ENV,BINDING)
    return store,model,budget,receipt


def test_deactivated_binding_still_reconciles_only_the_exact_old_budget_scope(case):
    store,model,budget,receipt=linked(case);inactive=replace(BINDING,active=False)
    assert store.agent_budget_scope('synthetic-agent',envelope=ENV,binding=inactive,for_accounting=True)==receipt.budget_scope_id
    with pytest.raises(SemanticPlanError):store.agent_budget_scope('synthetic-agent',envelope=ENV,binding=inactive)
    assert model.calls==1 and len(budget.calls)==1


@pytest.mark.parametrize('change',[
    {'knowledge_base_id':7},{'public_reply_allowed':False},{'instance_id':'foreign-instance'},
    {'binding_id':'other-binding'},
])
def test_accounting_flag_cannot_hide_any_other_binding_difference(case,change):
    store,model,budget,receipt=linked(case)
    with pytest.raises(SemanticPlanError):
        store.agent_budget_scope('synthetic-agent',envelope=ENV,binding=replace(BINDING,active=False,**change),for_accounting=True)
    assert model.calls==1 and len(budget.calls)==1


def test_accounting_flag_cannot_retarget_source_or_accept_a_nonboolean_mode(case):
    store,model,budget,receipt=linked(case);inactive=replace(BINDING,active=False)
    with pytest.raises(SemanticPlanError):
        store.agent_budget_scope('synthetic-agent',envelope={**ENV,'reference_id':'other-reference'},binding=inactive,for_accounting=True)
    with pytest.raises(SemanticPlanError):
        store.agent_budget_scope('synthetic-agent',envelope=ENV,binding=inactive,for_accounting='yes')
    assert model.calls==1 and len(budget.calls)==1
