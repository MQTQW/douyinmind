"""An exact UI fee record cannot silently look like legacy under another owner."""
import pytest

from app.assistant.semantic_plan_store import SemanticPlanError
from tests.test_semantic_planning_store import case,run,ENV,BINDING,OWNER


@pytest.mark.parametrize('field,value',[
    ('user_id','another-valid-user'),('device_id','another-valid-device'),
    ('root_reference_id','reference:another-root'),
])
def test_existing_accounting_scope_mismatch_is_not_legacy_none(case,field,value):
    store,model,budget=case;active=[True];model.hook=lambda:active.__setitem__(0,False)
    with pytest.raises(SemanticPlanError):run(store,model,budget,scope_authorizer=lambda:active[0])
    with pytest.raises(SemanticPlanError,match='accounting_scope_changed'):
        store.get_recorded_accounting(ENV,binding=BINDING,owner_scope={**OWNER,field:value})
    assert model.calls==1 and store.get_recorded_decision(ENV,binding=BINDING) is None


def test_unrelated_legacy_envelope_has_no_accounting_even_with_missing_owner_metadata(case):
    store,model,budget=case
    assert store.get_recorded_accounting({**ENV,'reference_id':'reference:legacy'},owner_scope={'user_id':None}) is None
