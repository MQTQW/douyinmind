"""Owner-lane observation cannot turn into model/import/publication work."""
import threading
from types import SimpleNamespace

import pytest

from app.assistant.runtime import AssistantRuntime
from app.assistant.queue import QueueError
from app.assistant.legacy_bridge import LegacyBridge
from app.mention_receiver.browser import ReceiverInterventionRequired


class Queue:
    def __init__(self):
        self.claims = []; self.finishes = []
        self.task = {'id':'product', 'state':'delivery_unknown',
                     '_delivery_readback_claim_token':'synthetic-internal-capability'}
    def claim_delivery_readback(self, **kwargs):
        self.claims.append(kwargs)
        return self.task
    def finish_delivery_readback(self, *args, **kwargs):
        self.finishes.append((args,kwargs))
        return {'id':'product','state':'b_view_visible' if kwargs.get('confirmation') is not None else 'delivery_unknown'}
    def get_task(self, task_id):
        return {'id':task_id,'state':'delivery_unknown'}


def runtime(adapter, *, running=True, owner=None):
    queue = Queue()
    control = SimpleNamespace(snapshot=lambda:{'desired_state':'running' if running else 'paused','run_id':'run'})
    def forbidden(*args):
        pytest.fail('readback invoked model/import/publication adapter')
    value = AssistantRuntime(queue,control,run_id='run',owner_thread_id=owner,
        delivery_readback_adapter=adapter,execute=forbidden,publish=forbidden)
    return value,queue


def test_readback_passes_verified_object_without_serialization_or_copy():
    class Credential(dict):
        pass
    credential = Credential(confirmation_id='record')
    value,queue=runtime(lambda task:credential)
    assert value.reconcile_delivery_once()['state']=='b_view_visible'
    assert queue.finishes[0][1]['confirmation'] is credential
    assert queue.claims==[{'run_id':'run'}]


def test_paused_or_missing_adapter_cannot_claim_or_navigate():
    value,queue=runtime(lambda task:pytest.fail('browser called'),running=False)
    assert value.reconcile_delivery_once() is None and queue.claims==[]
    value,queue=runtime(None)
    assert value.reconcile_delivery_once() is None and queue.claims==[]


def test_wrong_thread_rejected_before_ledger_or_adapter():
    value,queue=runtime(lambda task:pytest.fail('browser called'),owner=threading.get_ident()+1)
    with pytest.raises(QueueError,match='browser_lane_wrong_thread'):
        value.reconcile_delivery_once()
    assert queue.claims==[]


def test_no_matching_reply_keeps_unknown_and_never_calls_other_adapters():
    value,queue=runtime(lambda task:None)
    assert value.reconcile_delivery_once()['state']=='delivery_unknown'
    assert queue.finishes[0][1]['reason_code']=='readback_not_visible'


def test_callback_failure_records_only_fixed_reason_not_private_exception():
    def broken(task):
        raise RuntimeError('SYNTHETIC_PRIVATE_PROVIDER_BODY')
    value,queue=runtime(broken)
    assert value.reconcile_delivery_once()['state']=='delivery_unknown'
    assert queue.finishes[0][1]=={'reason_code':'readback_unconfirmed'}


def test_human_challenge_surfaces_even_if_local_confirmation_write_fails():
    def challenged(task):
        raise ReceiverInterventionRequired('synthetic human challenge')
    value,queue=runtime(challenged)
    def bad_finish(*args,**kwargs):
        raise QueueError('synthetic_write_failure')
    queue.finish_delivery_readback=bad_finish
    with pytest.raises(ReceiverInterventionRequired):
        value.reconcile_delivery_once()


def bridge_case(monkeypatch, *, authorized=True, recorded=None):
    value=LegacyBridge.__new__(LegacyBridge)
    value.adapter=SimpleNamespace(check_intervention=lambda:None)
    value.browser_owner_thread_id=threading.get_ident()
    value.should_stop=lambda:False
    value.control=SimpleNamespace(snapshot=lambda:{'desired_state':'running'})
    value.admission_lock=threading.RLock()
    value._find=lambda task:{'task_id':'agent'}
    value._authorized=lambda task:authorized
    reply={'state':'delivery_unknown','send_token':'synthetic-only','text':'fixed public receipt',
        'target':{'source_mode':'live_ui','choice_evidence':{'source_kind':'library_choice_reply'}}}
    observed=[]; writes=[]
    credential=object()
    value.agent=SimpleNamespace(get_reply=lambda task_id:reply,
        get_reply_readback=lambda task_id:recorded,
        record_reply_readback=lambda task_id,observation:(writes.append((task_id,observation)) or credential))
    observation={'outcome':'b_view_visible','exact_body_in_original_thread':True}
    def observe(adapter,target,text):
        observed.append((adapter,target,text))
        return observation
    import app.mention_receiver.choice_browser as module
    monkeypatch.setattr(module,'observe_existing_choice_reply',observe,raising=False)
    return value,reply,observation,observed,writes,credential


def test_bridge_uses_original_target_and_body_then_returns_store_credential(monkeypatch):
    value,reply,observation,observed,writes,credential=bridge_case(monkeypatch)
    assert value.observe_existing_delivery({'state':'delivery_unknown'}) is credential
    assert observed==[(value.adapter,reply['target'],reply['text'])]
    assert writes==[('agent',observation)]


def test_bridge_reuses_valid_durable_confirmation_without_browser_work(monkeypatch):
    stored=object()
    value,reply,observation,observed,writes,_=bridge_case(monkeypatch,recorded=stored)
    assert value.observe_existing_delivery({'state':'delivery_unknown'}) is stored
    assert observed==writes==[]


@pytest.mark.parametrize('change', ['wrong_thread','stopped','paused','revoked','unsent','nonchoice','different_state'])
def test_bridge_blocks_non_authoritative_or_unstarted_cases(monkeypatch,change):
    value,reply,observation,observed,writes,_=bridge_case(monkeypatch,authorized=change!='revoked')
    task={'state':'delivery_unknown'}
    if change=='wrong_thread':value.browser_owner_thread_id+=1
    if change=='stopped':value.should_stop=lambda:True
    if change=='paused':value.control.snapshot=lambda:{'desired_state':'paused'}
    if change=='unsent':reply['send_token']=None
    if change=='nonchoice':reply['target'].pop('choice_evidence')
    if change=='different_state':task['state']='ready_to_publish'
    with pytest.raises(ValueError):value.observe_existing_delivery(task)
    assert observed==writes==[]


def test_bridge_post_read_revocation_cannot_persist_a_confirmation(monkeypatch):
    value,reply,observation,observed,writes,_=bridge_case(monkeypatch)
    checks=iter((True,False))
    value._authorized=lambda task:next(checks)
    with pytest.raises(ValueError,match='readback_scope_changed'):
        value.observe_existing_delivery({'state':'delivery_unknown'})
    assert len(observed)==1 and writes==[]


def test_bridge_exact_observation_is_required_before_store_validation(monkeypatch):
    value,reply,observation,observed,writes,_=bridge_case(monkeypatch)
    observation['exact_body_in_original_thread']=False
    assert value.observe_existing_delivery({'state':'delivery_unknown'}) is None
    assert len(observed)==1 and writes==[]


@pytest.mark.parametrize('broken_stage', ['stored_proof', 'projection'])
def test_bad_local_proof_cannot_starve_another_recorded_confirmation(broken_stage):
    value,queue=runtime(lambda task:pytest.fail('local confirmation opened browser'))
    credential=object()
    queue.list_delivery_readback_candidates=lambda **kwargs:[{'id':'broken','created_at':2},{'id':'good','created_at':1}]
    def recorded(task):
        if task['id']=='broken' and broken_stage=='stored_proof':
            raise ValueError('SYNTHETIC_PRIVATE_INVALID_PROOF')
        return credential
    projections=[]
    def project(task_id, proof, **kwargs):
        assert proof is credential
        if task_id=='broken':
            raise QueueError('synthetic_invalid_projection')
        projections.append(task_id)
        return {'id':task_id,'state':'b_view_visible'}
    value.recorded_readback_adapter=recorded
    queue.project_recorded_delivery_readback=project
    assert value.reconcile_delivery_once()=={'id':'good','state':'b_view_visible'}
    assert projections==['good'] and queue.claims==[]
