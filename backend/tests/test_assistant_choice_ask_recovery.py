"""Read-only ask reconciliation must survive restarts without another send."""
from hashlib import sha256

import pytest

from app.assistant.library_choice import LibraryChoiceStore, LibraryChoiceError
from tests.library_choice_fixture import choice_fixture, visible, reserve


@pytest.mark.parametrize('outcome',[None,'delivery_unknown','verification_required','failed_no_submit'])
def test_ask_observation_lists_unselected_intent_without_replaying_public_action(tmp_path,outcome):
    case=choice_fixture(tmp_path)
    if outcome:case.store.record_ask_outcome(case.created['choice_id'],ask_ref=case.ask_ref,outcome=outcome)
    before=case.store.get(case.created['choice_id']);digest=sha256(case.store.path.read_bytes()).hexdigest()
    values=case.store.list_asks_to_observe(case.root_binding.receiver_profile_url,instance_id=case.root_binding.instance_id)
    assert len(values)==1 and values[0]['choice_id']==before['choice_id']
    assert values[0]['state']==before['state'] and values[0]['ask_ref']==before['ask_ref']
    assert values[0]['ask_reply_ref'] is None and values[0]['selection_ref'] is None
    assert sha256(case.store.path.read_bytes()).hexdigest()==digest
    assert case.store.get(case.created['choice_id'])==before
    # Only a separate trusted B-side exact-body readback may move the same ask.
    waiting=visible(case)
    assert waiting['state']=='waiting_library' and waiting['ask_ref']==before['ask_ref']
    assert case.store.list_asks_to_observe(case.root_binding.receiver_profile_url)==[]


def test_ask_reconciliation_round_robin_deferral_survives_reopen_and_expires(tmp_path):
    clock=[1900000000.0]
    first=choice_fixture(tmp_path,clock=lambda:clock[0]);second=choice_fixture(tmp_path,store=first.store,root_number=2,clock=lambda:clock[0])
    first.store.record_ask_outcome(first.created['choice_id'],ask_ref=first.ask_ref,outcome='delivery_unknown')
    receiver=first.root_binding.receiver_profile_url
    initial=first.store.list_asks_to_observe(receiver);assert len(initial)==2
    skipped=initial[0];old=first.store.get(skipped['choice_id'])
    assert first.store.defer_waiting_recheck(skipped['choice_id'],seconds=120)
    reopened=LibraryChoiceStore(first.store.path,clock=lambda:clock[0])
    assert [row['choice_id'] for row in reopened.list_asks_to_observe(receiver)]==[initial[1]['choice_id']]
    assert reopened.get(skipped['choice_id'])==old
    clock[0]+=121
    assert {row['choice_id'] for row in reopened.list_asks_to_observe(receiver)}=={row['choice_id'] for row in initial}


def test_ask_observation_scope_and_selected_or_held_terminal_boundaries(tmp_path):
    case=choice_fixture(tmp_path);receiver=case.root_binding.receiver_profile_url
    assert case.store.list_asks_to_observe('https://www.douyin.com/user/foreign')==[]
    assert case.store.list_asks_to_observe(receiver,instance_id='different-computer')==[]
    for limit in (0,129,True):
        with pytest.raises(LibraryChoiceError):case.store.list_asks_to_observe(receiver,limit=limit)
    visible(case);selected=reserve(case)
    assert case.store.list_asks_to_observe(receiver)==[]
    assert case.store.defer_waiting_recheck(selected['choice_id'],seconds=45) is False
    other=choice_fixture(tmp_path,store=case.store,root_number=2)
    case.store.hold_waiting(other.created['choice_id'],'binding_changed')
    assert case.store.list_asks_to_observe(receiver)==[]
    assert case.store.defer_waiting_recheck(other.created['choice_id']) is False
