"""Final import receipt text, temporary ledgers and fake backend responses only."""
from copy import deepcopy

import pytest

from app.mention_agent.imports import ImportTools, import_status_proposal
from app.mention_agent.store import AgentStore, AgentStoreError
from tests.test_mention_agent_store import binding, envelope, ingestion_observation, verification
import tests.test_mention_agent_import_tools as import_fixtures


PREFIX = '少爷，'
LONG_NAME = '首先_Git#最后_' + '原名' * 54


def test_fixed_success_receipt_preserves_long_library_name_and_reserves_prefix_budget():
    job = {'job_id': 'synthetic-import-receipt', 'state': 'succeeded'}
    before = deepcopy(job)
    result = import_status_proposal(job, LONG_NAME, reply_prefix=PREFIX)
    expected = f'存好啦，在「{LONG_NAME}」里，想问啥直接问我就行。'
    assert result['text'] == expected and LONG_NAME in result['text']
    assert 90 < len(result['text']) < 320 - len(PREFIX)
    assert result == {'kind': 'import_status', 'text': expected, 'source_ids': [], 'sources': [],
                      'import_job_id': job['job_id'], 'import_state': 'succeeded'}
    assert job == before
    exact = len(PREFIX) + len(expected)
    assert import_status_proposal(job, LONG_NAME, reply_prefix=PREFIX, maximum=exact) == result
    with pytest.raises(ValueError, match='invalid_public_reply'):
        import_status_proposal(job, LONG_NAME, reply_prefix=PREFIX, maximum=exact - 1)


@pytest.fixture
def import_case():
    case = import_fixtures.ImportToolsTests()
    case.setUp()
    try:
        yield case
    finally:
        case.doCleanups()


def test_import_tools_proposal_uses_frozen_prefix_budget_and_fresh_receipt_without_a_post(import_case):
    case = import_case
    job = case.existing_success()
    case.backend.default_status = 'succeeded'
    expected = import_status_proposal(job, LONG_NAME, reply_prefix=PREFIX)
    maximum = len(PREFIX) + len(expected['text'])
    tools = ImportTools(case.store, case.binding, case.claim['task_id'], case.claim['claim_token'],
                        case.envelope, LONG_NAME, client=case.backend, wait_seconds=0,
                        reply_prefix=PREFIX, max_reply_chars=maximum)
    assert tools.get_import_status()['status'] == 'succeeded'
    assert tools.proposal() == expected
    assert case.backend.gets == [job['job_id'], job['job_id']]
    assert case.backend.posts == []


def processing_draft(tmp_path):
    store = AgentStore(tmp_path / 'agent.sqlite3')
    store.put_binding(binding())
    task = store.enqueue('fixture-binding', envelope())
    claim = store.claim(task['task_id'])
    job = store.ensure_ingestion(task['task_id'], claim['claim_token'])
    job = store.observe_ingestion(job['job_id'], ingestion_observation(
        job, backend_state='processing', membership_exists=False,
        cache_status='transcribing', indexed_content_chunks=0))
    old = {**import_status_proposal(job, LONG_NAME), 'text': '旧版处理回执，保留原文。'}
    reply = store.complete(task['task_id'], claim['claim_token'], old, reply_prefix=PREFIX)
    return store, task, job, reply


def test_same_state_receipt_preserves_legacy_body_target_and_archive_bytes(tmp_path):
    store, task, job, old = processing_draft(tmp_path)
    candidate = import_status_proposal(job, LONG_NAME, reply_prefix=PREFIX)
    before = store.path.read_bytes()
    original = deepcopy(candidate)
    assert store.refresh_unsent_import_reply(task['task_id'], candidate) == old
    assert store.path.read_bytes() == before and candidate == original
    assert store.list_import_reply_updates(task['task_id']) == []


@pytest.mark.parametrize('change', ['job', 'state', 'binding'])
def test_same_state_preservation_still_checks_job_receipt_and_binding(tmp_path, change):
    store, task, job, old = processing_draft(tmp_path)
    candidate = import_status_proposal(job, LONG_NAME)
    if change == 'job':
        candidate['import_job_id'] = 'different-synthetic-import'
    elif change == 'state':
        candidate['import_state'] = 'succeeded'
    else:
        store.deactivate('fixture-binding')
    with pytest.raises(AgentStoreError):
        store.refresh_unsent_import_reply(task['task_id'], candidate)
    current = store.get_reply(task['task_id'])
    if change == 'binding':
        assert store.get_binding('fixture-binding')['active'] is False
        assert current['text'] == old['text'] and current['proposal'] == old['proposal']
        assert current['target'] == old['target'] and current['send_token'] == old['send_token']
        assert current['state'] in {old['state'], 'needs_review'}
        assert store.get_task(task['task_id'])['state'] in {'answer_ready', 'needs_review'}
        assert store.reserve_send(task['task_id'], verification()) is None
    else:
        assert current == old
    assert store.list_import_reply_updates(task['task_id']) == []


def test_state_transition_archives_once_and_freezes_full_library_name_with_original_prefix(tmp_path):
    store, task, job, old = processing_draft(tmp_path)
    succeeded = store.observe_ingestion(job['job_id'], ingestion_observation(job))
    candidate = import_status_proposal(succeeded, LONG_NAME, reply_prefix=PREFIX)
    result = store.refresh_unsent_import_reply(task['task_id'], candidate)
    assert result['text'] == PREFIX + candidate['text'] and LONG_NAME in result['text']
    assert result['target'] == old['target'] and result['import_state'] == 'succeeded'
    assert result['source_ids'] == result['sources'] == []
    updates = store.list_import_reply_updates(task['task_id'])
    assert len(updates) == 1 and updates[0]['reply'] == old
    changed = {**candidate, 'text': '另一种成功话术，不得覆盖旧稿。'}
    before = store.path.read_bytes()
    assert store.refresh_unsent_import_reply(task['task_id'], changed) == result
    assert store.path.read_bytes() == before
    assert store.list_import_reply_updates(task['task_id']) == updates


def test_state_transition_cannot_exceed_frozen_prefix_budget_or_replace_old_draft(tmp_path):
    store, task, job, old = processing_draft(tmp_path)
    succeeded = store.observe_ingestion(job['job_id'], ingestion_observation(job))
    candidate = {**import_status_proposal(succeeded, LONG_NAME), 'text': '回' * 318}
    with pytest.raises(ValueError, match='invalid_public_reply'):
        store.refresh_unsent_import_reply(task['task_id'], candidate)
    assert store.get_reply(task['task_id']) == old
    assert store.list_import_reply_updates(task['task_id']) == []


@pytest.mark.parametrize('unknown', [False, True])
def test_reserved_or_unknown_receipt_never_refreshes_or_rewrites(tmp_path, unknown):
    store, task, job, _ = processing_draft(tmp_path)
    reserved = store.reserve_send(task['task_id'], verification())
    assert reserved is not None
    if unknown:
        store.finish_send(task['task_id'], reserved['send_token'], 'delivery_unknown', {'synthetic': True})
    old = store.get_reply(task['task_id'])
    candidate = import_status_proposal(job, LONG_NAME)
    with pytest.raises(AgentStoreError, match='reply_not_unsent_ready'):
        store.refresh_unsent_import_reply(task['task_id'], candidate)
    assert store.get_reply(task['task_id']) == old
    assert store.list_import_reply_updates(task['task_id']) == []
