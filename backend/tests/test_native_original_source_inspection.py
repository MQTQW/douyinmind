"""Paused owner original-source reads use synthetic pages and inert stores."""
from copy import deepcopy
import json
import threading
from types import SimpleNamespace

import pytest

from app.assistant.native_lane import NativeLane, NativeLaneError
from app.mention_receiver.browser import (
    ReceiverDetailError, ReceiverInterventionRequired, ReceiverPageError,
)


@pytest.fixture
def inspection():
    source_id = 'native:' + 'a' * 64
    state = {'run_id': 'b' * 32, 'state': 'paused', 'desired_state': 'paused'}
    candidate = {'source_kind': 'native_notice_candidate', 'namespace': 'douyin_web_v1',
        'sender_uid': '10001', 'recipient_uid': '10002', 'sender_sec_uid': 'fake-author',
        'sender_profile_url': 'https://www.douyin.com/user/fake-author',
        'receiver_profile_url': 'https://www.douyin.com/user/fake-receiver',
        'native_comment_id': '7000000000000000001', 'video_id': '7000000000000000002',
        'content': '@receiver 加入知识库', 'native_source_key': source_id,
        'identity_link_revision': '1', 'root_comment_id': None, 'parent_comment_id': None}
    saved = {'source_id': source_id, 'state': 'held', 'hold_code': 'native_ui_unverified',
        'historical': False, 'attempts': 1, 'candidate': candidate,
        'plan': None, 'task_id': None, 'reference_id': None}
    detail = {key: candidate[key] for key in ('sender_profile_url', 'receiver_profile_url', 'video_id')}
    detail.update(command_text=candidate['content'], native_comment_id=candidate['native_comment_id'],
        ok=True, unique_count=1, real_mention=True, sender_verified=True, video_verified=True,
        native_correlation={'source_native_comment_id': candidate['native_comment_id'],
            'observed_native_comment_id': candidate['native_comment_id'], 'quality': 'native_equal'},
        native_ui_notice={'source_profile_url': candidate['sender_profile_url'],
            'exact_text': candidate['content'], 'semantic_fingerprint': 'fake-visible-notice'})
    events = []
    gates = {'scope': True, 'effects_absent': True, 'checkpoint': True}
    counts = {'reason_code': 'matched', 'candidate_limit': 8, 'candidate_count': 1,
        'checked_count': 1, 'match_count': 1, 'final_rechecks': 1, 'final_verified': 1,
        'lookup_stage_index': 10}
    def forbidden(*args, **kwargs):
        pytest.fail('inspection consumed a source or invoked an execution effect')
    lane = NativeLane.__new__(NativeLane)
    lane._owner_thread = threading.get_ident()
    lane.browser_lane = SimpleNamespace()
    lane.run_id = state['run_id']
    lane.queue = SimpleNamespace(get_control=lambda: deepcopy(state), enqueue=forbidden)
    lane.source_store = SimpleNamespace(get_source=lambda key: deepcopy(saved) if key == source_id else None,
        claim_ui_candidate=forbidden, record_ui_plan=forbidden, hold=forbidden,
        request_followup_review=forbidden)
    lane._recovery_identity = lambda: events.append('identity')
    lane._visibility_scope_current = lambda value: gates['scope']
    lane._visibility_absent_from_effects = lambda value: gates['effects_absent']
    lane._checkpoint = lambda: gates['checkpoint']
    lane.adapter = SimpleNamespace(prepare_reply=forbidden, submit_reply=forbidden,
        last_original_lookup={'reason_code': 'matched', 'raw': 'STALE-PRIVATE'})
    def read(target, **kwargs):
        assert lane.adapter.last_original_lookup == {}
        assert target['native_comment_id'] == candidate['native_comment_id']
        assert target['reference_id'] == 'native-inspection:' + 'a' * 64
        assert kwargs == {'max_candidates': 8, 'native_candidate_correlation': True}
        events.append('read')
        lane.adapter.last_original_lookup = deepcopy(counts)
        return deepcopy(detail)
    lane.adapter.open_original_from_notifications = read
    return SimpleNamespace(lane=lane, source_id=source_id, state=state, saved=saved,
        candidate=candidate, detail=detail, counts=counts, events=events, gates=gates, read=read)


@pytest.mark.parametrize('dom_id', ['7000000000000000001', None])
def test_original_source_read_is_repeatable_and_preserves_source_and_missing_dom_id(inspection, dom_id):
    c = inspection
    c.detail['native_comment_id'] = dom_id
    c.detail['native_correlation'].update(observed_native_comment_id=dom_id,
        quality='native_equal' if dom_id is not None else 'semantic_only')
    before = deepcopy(c.saved)
    for _ in range(2):
        result = c.lane.inspect_original_source(c.source_id)
        assert result == {'source_id': c.source_id, 'status': 'original_source_inspected',
            'dispatchable': False, 'matched': True}
        assert c.saved == before and c.detail['native_comment_id'] == dom_id
        assert c.lane.last_native_recovery_diagnostic['lookup_reason_code'] == 'matched'
    assert c.events == ['identity', 'read', 'identity'] * 2


@pytest.mark.parametrize('field,value', [
    ('state', 'running'), ('desired_state', 'running'), ('run_id', 'c' * 32),
])
def test_original_source_requires_same_paused_run_before_read(inspection, field, value):
    c = inspection
    c.state[field] = value
    with pytest.raises(NativeLaneError, match='native_recovery_not_safe'):
        c.lane.inspect_original_source(c.source_id)
    assert c.events == []


@pytest.mark.parametrize('source', [None, True, 'native-hint:' + 'a' * 64, 'native:' + 'a' * 63])
def test_original_source_rejects_wrong_namespace_and_bad_ids(inspection, source):
    with pytest.raises(NativeLaneError, match='native_recovery_not_safe'):
        inspection.lane.inspect_original_source(source)
    assert inspection.events == []


@pytest.mark.parametrize('field,value', [
    ('state', 'fresh'), ('hold_code', 'native_unknown_lease'), ('historical', True),
    ('plan', {}), ('task_id', 'c' * 32), ('reference_id', 'fake-reference'),
])
def test_original_source_only_reads_unplanned_ui_held_records(inspection, field, value):
    c = inspection
    c.saved[field] = value
    with pytest.raises(NativeLaneError, match='native_recovery_scope_unavailable'):
        c.lane.inspect_original_source(c.source_id)
    assert c.events == ['identity']


def test_original_source_rejects_an_ordinary_reply(inspection):
    c = inspection
    c.candidate['source_kind'] = 'native_reply_notice_candidate'
    with pytest.raises(NativeLaneError, match='native_recovery_scope_unavailable'):
        c.lane.inspect_original_source(c.source_id)
    assert c.events == ['identity']


@pytest.mark.parametrize('gate', ['scope', 'effects_absent', 'checkpoint'])
def test_original_source_needs_scope_effect_and_checkpoint_gates(inspection, gate):
    c = inspection
    c.gates[gate] = False
    with pytest.raises(NativeLaneError, match='native_recovery_scope_unavailable'):
        c.lane.inspect_original_source(c.source_id)
    assert c.events == ['identity']


@pytest.mark.parametrize('field,value', [
    ('sender_profile_url', 'https://www.douyin.com/user/other-author'),
    ('receiver_profile_url', 'https://www.douyin.com/user/other-receiver'),
    ('video_id', '7000000000000000003'), ('command_text', 'different body'),
    ('command_text', True), ('native_comment_id', '7000000000000000003'),
    ('native_comment_id_conflict', True), ('native_correlation', {}),
    ('native_ui_notice', {}), ('ok', False), ('unique_count', True),
    ('real_mention', False), ('sender_verified', False), ('video_verified', False),
])
def test_original_source_requires_full_matching_readback(inspection, field, value):
    c = inspection
    c.detail[field] = value
    before = deepcopy(c.saved)
    assert c.lane.inspect_original_source(c.source_id)['matched'] is False
    assert c.saved == before
    assert c.lane.last_native_recovery_diagnostic['code'] == 'original_source_unverified'


@pytest.mark.parametrize('field,value', [
    ('source_profile_url', 'https://www.douyin.com/user/other-author'),
    ('exact_text', 'changed'), ('semantic_fingerprint', ''), ('semantic_fingerprint', True),
    ('unexpected', 'PRIVATE'),
])
def test_original_source_needs_actual_matching_notice(inspection, field, value):
    c = inspection
    c.detail['native_ui_notice'][field] = value
    assert c.lane.inspect_original_source(c.source_id)['matched'] is False


@pytest.mark.parametrize('field,value', [
    ('reason_code', 'original_target_not_unique'), ('final_verified', True),
    ('final_verified', 0), ('lookup_stage_index', 9), ('lookup_stage_index', True),
])
def test_original_source_requires_fresh_final_adapter_confirmation(inspection, field, value):
    c = inspection
    c.counts[field] = value
    assert c.lane.inspect_original_source(c.source_id)['matched'] is False


@pytest.mark.parametrize('change', ['source', 'scope', 'effects', 'run', 'resume'])
def test_original_source_rechecks_gates_after_browser_wait(inspection, change):
    c = inspection
    def read(*args, **kwargs):
        detail = c.read(*args, **kwargs)
        if change == 'source':
            c.saved['attempts'] += 1
        elif change == 'scope':
            c.gates['scope'] = False
        elif change == 'effects':
            c.gates['effects_absent'] = False
        elif change == 'run':
            c.state['run_id'] = 'c' * 32
        else:
            c.state['desired_state'] = 'running'
        return detail
    c.lane.adapter.open_original_from_notifications = read
    with pytest.raises(NativeLaneError, match='native_recovery_scope_unavailable'):
        c.lane.inspect_original_source(c.source_id)
    assert c.lane.last_native_recovery_diagnostic['code'] == 'native_recovery_scope_unavailable'


@pytest.mark.parametrize('error', [ReceiverPageError('PRIVATE'), ReceiverInterventionRequired('PRIVATE')])
def test_original_source_propagates_page_and_intervention_failures(inspection, error):
    c = inspection
    def read(*args, **kwargs):
        c.lane.adapter.last_original_lookup = {'reason_code': 'intervention_required', 'raw': 'PRIVATE'}
        raise error
    c.lane.adapter.open_original_from_notifications = read
    before = deepcopy(c.saved)
    with pytest.raises(type(error)) as caught:
        c.lane.inspect_original_source(c.source_id)
    assert caught.value is error and c.saved == before
    diagnostic = c.lane.last_native_recovery_diagnostic
    assert diagnostic['lookup_reason_code'] == 'intervention_required'
    assert 'PRIVATE' not in json.dumps(diagnostic)


def test_original_source_detail_failure_keeps_exact_diagnostic_without_consuming_source(inspection):
    c = inspection
    def read(*args, **kwargs):
        c.lane.adapter.last_original_lookup = {'reason_code': 'original_notification_not_found',
            'candidate_count': 0, 'checked_count': True, 'final_verified': -1,
            'page_open_count': 1000001, 'raw': 'PRIVATE'}
        raise ReceiverDetailError('PRIVATE', {'raw': 'PRIVATE'})
    c.lane.adapter.open_original_from_notifications = read
    before = deepcopy(c.saved)
    assert c.lane.inspect_original_source(c.source_id)['matched'] is False
    assert c.saved == before
    assert c.lane.last_native_recovery_diagnostic['lookup_counts'] == {'candidate_count': 0}
    assert c.lane.last_native_recovery_diagnostic['lookup_reason_code'] == 'original_notification_not_found'
    assert 'PRIVATE' not in json.dumps(c.lane.last_native_recovery_diagnostic)


def test_original_source_requires_browser_owner_thread(inspection):
    inspection.lane._owner_thread += 1
    with pytest.raises(NativeLaneError, match='native_browser_owner_required'):
        inspection.lane.inspect_original_source(inspection.source_id)
    assert inspection.events == []
