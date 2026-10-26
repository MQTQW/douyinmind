"""Followup lineage: real temporary ledgers, synthetic UI/model/import only."""
from copy import deepcopy
from types import SimpleNamespace

from app.assistant.legacy_bridge import LegacyBridge
from app.assistant.thread_followup import ThreadFollowupCoordinator
from app.assistant.runtime import AssistantRuntime
from tests.test_conversation_followup_bridge import (
    prepared, planned_child, child_detail, AllModel, AllKnowledge,
)
from app.assistant.commands import CommandResolver


def published_followup(tmp_path, monkeypatch):
    original_publish = AssistantRuntime.publish_once
    def publication(self):
        result = original_publish(self)
        if result is not None and result['state'] != 'b_view_visible':
            raise AssertionError({'fixture_state': result['state'], 'fixed_reason_code': result.get('reason_code')})
        return result
    monkeypatch.setattr(AssistantRuntime, 'publish_once', publication)
    values = prepared(tmp_path)
    binding, agent, root, registry, migrated, queue, history, control, original, anchor, backend, adapter = values
    env, task, budget, semantic, planner, resolution = planned_child(tmp_path, values)
    model = AllModel()
    knowledge = AllKnowledge()
    monkeypatch.setattr('app.assistant.legacy_bridge.BackendKnowledgeTools', lambda *a, **k: knowledge)
    bridge = LegacyBridge(registry, queue, control, agent_path=agent.path, conversation=history,
        database=tmp_path/'never-open-business.db', import_client=backend, model_factory=lambda: model,
        adapter=adapter, semantic_store=semantic, usage_budget=budget, command_resolver=CommandResolver(registry))
    runtime = AssistantRuntime(queue, control, execute=bridge.execute, publish=bridge.publish)
    assert runtime.execute_once()['state'] == 'ready_to_publish'
    assert runtime.publish_once()['state'] == 'b_view_visible'
    return values, bridge, model, task


def next_candidate(values):
    binding, _, root, *_ = values
    return {'source_kind': 'native_reply_notice_candidate',
        'sender_profile_url': binding.sender_profile_url,
        'receiver_profile_url': binding.receiver_profile_url, 'video_id': root['video_id'],
        'native_comment_id': '7999999000000000093', 'content': '这个做法有什么作用？',
        'parent_id_hint': root['native_comment_id'], 'recipient_uid': '99999999'}


def test_second_flat_followup_uses_same_published_origin_instead_of_rejecting_two_B_receipts(tmp_path, monkeypatch):
    values, bridge, model, first_child = published_followup(tmp_path, monkeypatch)
    binding, agent, root, registry, migrated, queue, history, control, original, origin, backend, adapter = values
    candidate = next_candidate(values)
    anchors = bridge.followup_anchors(candidate)
    # Root save receipt and the already published answer are one explicit lineage.
    assert len(anchors) == 1
    assert anchors[0]['root_task_id'] == origin['root_task_id']
    reads = []
    def read(anchor, observed):
        reads.append((anchor['root_task_id'], observed['native_comment_id']))
        detail = child_detail(anchor, text=observed['content'])
        detail['native_comment_id'] = observed['native_comment_id']
        detail['evidence']['native_comment_id'] = observed['native_comment_id']
        return detail
    lane = SimpleNamespace(pending={}, registry=registry)
    coordinator = ThreadFollowupCoordinator(lane, bridge)
    detail, envelope, anchor, failure = coordinator.observe(candidate, checkpoint=lambda: True, read=read)
    assert failure is None and envelope is not None and len(reads) == 1
    assert envelope['evidence']['real_mention'] is False
    assert envelope['evidence']['parent_scope'] == 'unique_followup_in_verified_root'
    assert envelope['evidence']['direct_reply_to_prior'] is False
    assert envelope['native_comment_id'] == candidate['native_comment_id']
    assert envelope['evidence']['root_native_comment_id'] == root['native_comment_id']
    assert queue.list_tasks()['total'] == 2  # Pure observation has not admitted the second followup.
    assert model.calls == 2 and backend.posts == 1

def lineage_node(task_id, root, *, predecessor=None, kind=None, owner='owner-one'):
    text = 'Synthetic published participation'
    import hashlib
    return {'root_task_id':task_id, 'root_agent_task_id':task_id[::-1],
        'root_envelope':deepcopy(root), 'prior_reply_text':text,
        'prior_reply_sha256':hashlib.sha256(text.encode()).hexdigest(),
        'instance_id':'instance-one', 'user_id':owner, 'device_id':'device-one',
        'anchor_source_kind':kind, 'predecessor_task_id':predecessor}


def test_explicit_three_level_derivation_is_order_independent_and_not_a_dom_parent_claim():
    root={'reference_id':'root-reference', 'native_comment_id':'7999999000000000101',
        'sender_profile_url':'https://www.douyin.com/user/synthetic-sender',
        'receiver_profile_url':'https://www.douyin.com/user/synthetic-receiver',
        'video_id':'7999999000000000100', 'comment_text':'@synthetic receiver save this'}
    ids=['1'*32,'2'*32,'3'*32,'4'*32]
    nodes={ids[0]:lineage_node(ids[0],root,kind='library_choice_reply')}
    for current,previous in zip(ids[1:],ids):
        nodes[current]=lineage_node(current,root,predecessor=previous,kind='conversation_followup')
    fake=SimpleNamespace(_followup_root_anchor=lambda task:deepcopy(nodes.get(task)))
    result=LegacyBridge._followup_origin_anchor(fake,ids[-1])
    assert result['root_task_id']==ids[0]
    assert result['root_envelope']['native_comment_id']==root['native_comment_id']
    assert nodes[ids[-1]]['predecessor_task_id']==ids[-2]


import pytest


@pytest.mark.parametrize('problem',['cross_user','cross_device','cross_instance','cross_sender',
    'cross_receiver','cross_video','same_text_other_root','changed_root_body','cycle','missing',
    'bad_predecessor','unknown_kind','unexpected_parent'])
def test_broken_or_cross_scope_persistent_lineage_fails_closed(problem):
    root={'reference_id':'root-one','native_comment_id':'7999999000000000101',
        'sender_profile_url':'https://www.douyin.com/user/synthetic-sender',
        'receiver_profile_url':'https://www.douyin.com/user/synthetic-receiver',
        'video_id':'7999999000000000100','comment_text':'same public root text'}
    origin=lineage_node('1'*32,root)
    child=lineage_node('2'*32,root,predecessor='1'*32,kind='conversation_followup')
    nodes={'1'*32:origin,'2'*32:child}
    if problem.startswith('cross_'):
        key={'cross_user':'user_id','cross_device':'device_id','cross_instance':'instance_id'}.get(problem)
        if key:origin[key]='other-scope'
        else:origin['root_envelope'][{'cross_sender':'sender_profile_url','cross_receiver':'receiver_profile_url','cross_video':'video_id'}[problem]]='other-scope'
    elif problem=='same_text_other_root':origin['root_envelope']['reference_id']='root-two'
    elif problem=='changed_root_body':origin['root_envelope']['comment_text']='different body'
    elif problem=='cycle':origin.update(anchor_source_kind='conversation_followup',predecessor_task_id='2'*32)
    elif problem=='missing':nodes.pop('1'*32)
    elif problem=='bad_predecessor':child['predecessor_task_id']='not-a-task'
    elif problem=='unknown_kind':origin['anchor_source_kind']='untrusted'
    elif problem=='unexpected_parent':origin['predecessor_task_id']='3'*32
    fake=SimpleNamespace(_followup_root_anchor=lambda task:deepcopy(nodes.get(task)))
    assert LegacyBridge._followup_origin_anchor(fake,'2'*32) is None


def test_two_independent_origins_same_root_are_not_chosen_by_order():
    # The coordinator does not choose a first/latest candidate merely because
    # both have the same profiles, video, public text or original Root.
    import hashlib
    root={'sender_profile_url':'https://www.douyin.com/user/synthetic-sender',
        'receiver_profile_url':'https://www.douyin.com/user/synthetic-receiver',
        'video_id':'7999999000000000100','reference_id':'root-one','evidence':{'real_mention':True}}
    anchors=[lineage_node('1'*32,root),lineage_node('2'*32,root)]
    candidate={key:root[key] for key in ('sender_profile_url','receiver_profile_url','video_id')}
    candidate['source_kind']='native_reply_notice_candidate'
    lane=SimpleNamespace(pending={})
    reads=[]
    for values in [anchors,list(reversed(anchors))]:
        bridge=SimpleNamespace(followup_anchors=lambda candidate:deepcopy(values))
        result=ThreadFollowupCoordinator(lane,bridge).observe(candidate,checkpoint=lambda:True,
            read=lambda *args:reads.append(1))
        assert result[-1]=='native_reply_parent_unresolved'
    assert reads==[]


def test_lineage_depth_is_bounded_without_extending_existing_anchor_authority():
    root={'reference_id':'root-one','comment_text':'same root'}
    ids=[format(index,'032x') for index in range(1,10)]
    nodes={ids[0]:lineage_node(ids[0],root)}
    for current,previous in zip(ids[1:],ids):
        nodes[current]=lineage_node(current,root,predecessor=previous,kind='conversation_followup')
    fake=SimpleNamespace(_followup_root_anchor=lambda task:deepcopy(nodes.get(task)))
    assert LegacyBridge._followup_origin_anchor(fake,ids[7])['root_task_id']==ids[0]
    assert LegacyBridge._followup_origin_anchor(fake,ids[8]) is None


def test_origin_withdrawal_still_blocks_existing_published_followup(tmp_path,monkeypatch):
    values,bridge,model,first_child=published_followup(tmp_path,monkeypatch)
    registry,migrated=values[3],values[4]
    registry.revoke_grant(migrated['grant_id'])
    assert bridge.followup_anchors(next_candidate(values))==[]
    assert model.calls==2 and values[-2].posts==1
