"""Historical success is preserved, but not reported as a fresh GET success."""
from copy import deepcopy

import pytest

import tests.test_mention_agent_import_tools as fixtures


@pytest.fixture
def case():
    value = fixtures.ImportToolsTests()
    value.setUp()
    try:
        yield value
    finally:
        value.doCleanups()


@pytest.mark.parametrize('response', [None, TimeoutError('synthetic response unavailable')])
def test_current_tool_status_does_not_claim_success_after_a_failed_receipt_read(case, response):
    job = case.existing_success()
    before = deepcopy(case.store.get_ingestion(job['job_id']))
    case.backend.status_actions = [response, response]
    tools = case.tools()
    result = tools.get_import_status()
    assert result['status'] == 'unknown'
    assert tools.confirmed_succeeded is False
    assert case.store.get_ingestion(job['job_id']) == before
    assert case.backend.posts == []
    with pytest.raises(ValueError, match='import_status_unverified'):
        tools.proposal()


def test_backend_scope_must_have_the_exact_frozen_integer_type_before_normalization(case):
    job = case.existing_success()
    before = deepcopy(case.store.get_ingestion(job['job_id']))
    case.backend.status_actions = [lambda request_id: case.backend.response(request_id, 'succeeded', knowledge_base_id=7.0)]
    tools = case.tools()
    assert tools.get_import_status()['status'] == 'unknown'
    assert tools.confirmed_succeeded is False
    assert case.store.get_ingestion(job['job_id']) == before
    assert case.backend.posts == []


def test_a_fresh_exact_success_remains_successful_without_a_second_import(case):
    job = case.existing_success()
    case.backend.default_status = 'succeeded'
    tools = case.tools()
    assert tools.get_import_status()['status'] == 'succeeded'
    assert tools.confirmed_succeeded is True
    assert tools.proposal()['import_state'] == 'succeeded'
    assert case.backend.posts == []
    assert case.store.get_ingestion(job['job_id'])['state'] == 'succeeded'
