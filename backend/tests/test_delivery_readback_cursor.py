"""Cross-module local proof recovery fairness, isolated SQLite only."""
from datetime import datetime, timezone
import hashlib
import sqlite3
import time

import pytest

from app.assistant.queue import AssistantQueue, QueueError
from app.assistant.runtime import AssistantRuntime
from app.mention_agent.store import AgentStore
from tests.library_choice_fixture import choice_fixture, visible, selection, reserve
from tests.test_mention_agent_store import proposal
from tests.test_reply_readback_ledger import setup_queue


def now():return datetime.now(timezone.utc).isoformat()


def four_unknown(tmp_path):
    records={};queue=None
    for number in range(1,5):
        folder=tmp_path/str(number);folder.mkdir()
        case=choice_fixture(folder,source_mode='live_ui',root_number=number);visible(case)
        env=selection(case);reserve(case,envelope=env)
        agent=AgentStore(folder/'agent.sqlite3');agent.put_binding(case.selected_bindings[7].to_dict())
        task=agent.enqueue('selected-7',env);claim=agent.claim(task['task_id'])
        reply=agent.complete(task['task_id'],claim['claim_token'],proposal(kind='clarification',source_ids=[],sources=[]))
        proof={**env['evidence'],**{key:env[key] for key in ('sender_profile_url','receiver_profile_url','video_id',
            'comment_text','reference_id','native_comment_id')},'current_receiver_verified':True,'observed_at_utc':now()}
        send=agent.reserve_send(task['task_id'],proof)
        agent.finish_send(task['task_id'],send['send_token'],'delivery_unknown',{'submit_gate_passed':True,
            'reply_sha256':hashlib.sha256(reply['text'].encode()).hexdigest()})
        queue,product=setup_queue(tmp_path,case,env,reply)
        records[product['id']]={'agent':agent,'reply':reply,'verification':proof}
    # Identical timestamps make the id tie-breaker part of the real fixture.
    with sqlite3.connect(queue.path) as db:db.execute('UPDATE assistant_tasks SET created_at=1000')
    return queue,records


def test_keyset_pages_are_pure_and_cover_four_equal_timestamp_candidates(tmp_path):
    queue,records=four_unknown(tmp_path)
    before=hashlib.sha256(queue.path.read_bytes()).hexdigest()
    first=queue.list_delivery_readback_candidates(limit=2)
    cursor=(first[-1]['created_at'],first[-1]['id'])
    second=AssistantQueue(queue.path).list_delivery_readback_candidates(limit=2,before=cursor)
    ids=[item['id'] for item in first+second]
    assert ids==sorted(records,reverse=True) and len(set(ids))==4
    assert queue.list_delivery_readback_candidates(before=(second[-1]['created_at'],second[-1]['id']))==[]
    assert hashlib.sha256(queue.path.read_bytes()).hexdigest()==before


@pytest.mark.parametrize('cursor',[(),(1,),(-1,'a'*32),(True,'a'*32),(float('nan'),'a'*32),
    (float('inf'),'a'*32),(1,'x'),(1,'A'*32),{'created_at':1,'id':'a'*32}])
def test_malformed_keyset_cursor_is_rejected_without_writes(tmp_path,cursor):
    queue=AssistantQueue(tmp_path/'queue.sqlite3');before=hashlib.sha256(queue.path.read_bytes()).hexdigest()
    with pytest.raises(QueueError,match='cursor'):queue.list_delivery_readback_candidates(before=cursor)
    assert hashlib.sha256(queue.path.read_bytes()).hexdigest()==before


def test_real_runtime_reaches_fourth_proof_after_cap_without_browser_and_restart_is_safe(tmp_path,monkeypatch):
    queue,records=four_unknown(tmp_path)
    clock=time.time();monkeypatch.setattr('app.assistant.queue.time.time',lambda:clock)
    for _ in range(12):
        claim=queue.claim_delivery_readback('owner')
        assert claim is not None
        queue.finish_delivery_readback(claim['id'],claim['_delivery_readback_claim_token'],reason_code='readback_not_visible')
        clock+=121
    assert queue.claim_delivery_readback('owner') is None
    order=[item['id'] for item in queue.list_delivery_readback_candidates(limit=24)]
    fourth=records[order[3]];reply=fourth['reply']
    timestamp=datetime.fromtimestamp(clock,timezone.utc).isoformat()
    observation={'outcome':'b_view_visible','exact_body_in_original_thread':True,
        'verification':{**fourth['verification'],'observed_at_utc':timestamp},
        'reply_sha256':hashlib.sha256(reply['text'].encode()).hexdigest(),'observed_at_utc':timestamp,
        'readback_quality':'direct_selected_parent','counts':{'matching_reply_count':1,'existing_matching_reply_count':1,
            'strict_matching_reply_count':1,'weak_matching_reply_count':0,'verified_root_count':1,
            'verified_selection_count':1,'selected_source_count':1}}
    credential=fourth['agent'].record_reply_readback(reply['task_id'],observation)
    calls=[];browser_calls=[]
    class Control:
        def snapshot(self):return queue.get_control()
    def recorded(task):
        calls.append(task['id'])
        record=records[task['id']]
        return AgentStore(record['agent'].path).get_reply_readback(record['reply']['task_id'])
    def forbidden_browser(task):
        browser_calls.append(task['id']);raise AssertionError('no fourth browser observation allowed')
    runtime=AssistantRuntime(queue,Control(),run_id='owner',delivery_readback_adapter=forbidden_browser,
        recorded_readback_adapter=recorded)
    assert runtime.reconcile_delivery_once() is None
    assert calls==order[:3]
    done=runtime.reconcile_delivery_once()
    assert done['id']==order[3] and done['state']=='b_view_visible' and browser_calls==[]
    assert calls==order
    restarted=AssistantRuntime(AssistantQueue(queue.path),Control(),run_id='owner',
        delivery_readback_adapter=forbidden_browser,recorded_readback_adapter=recorded)
    assert restarted.reconcile_delivery_once() is None and browser_calls==[]
    assert queue.project_recorded_delivery_readback(done['id'],credential,run_id='owner')==done
    with sqlite3.connect(queue.path) as db:
        assert db.execute("SELECT count(*) FROM assistant_events WHERE kind='delivery_readback_claimed'").fetchone()[0]==12
        assert db.execute("SELECT count(*) FROM assistant_events WHERE kind='delivery_readback_recovered'").fetchone()[0]==1
        assert db.execute('SELECT sum(execution_attempts),sum(publish_attempts) FROM assistant_tasks').fetchone()==(4,4)
    assert all(record['agent'].get_reply(record['reply']['task_id'])['state']=='delivery_unknown' for record in records.values())
