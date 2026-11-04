"""Private planning receipts cannot grant import capability to a plain reply.

These tests exercise semantic policy only, not browser proof or live delivery.
"""
from copy import deepcopy
from types import SimpleNamespace

from app.assistant.semantic_resolution import semantic_source_key
from app.mention_agent.followup_evidence import make_followup_envelope
from tests.test_semantic_command_resolution import case as semantic_case
from tests.test_thread_followup_owner_review import case as followup_case


def plain_reply(case):
    # Actual followup evidence is tested by the separate browser/contract suite.
    case.env['comment_text'] = '这个视频讲了什么事情'
    case.env['evidence']['source_kind'] = 'conversation_followup'
    return case.env


def verified_reply(case):
    detail = deepcopy(case.detail)
    detail['command_text'] = detail['evidence']['followup_text'] = '这个视频讲了什么事情'
    return make_followup_envelope(detail, case.binding)


def test_followup_query_defaults_all_own_material_and_never_inherits_save_library(followup_case):
    case = followup_case.c
    env = verified_reply(followup_case)
    case.model.decision = {'action': 'query', 'target_state': 'unspecified', 'grant_id': None}
    result = case.router(env, source_checkpoint=lambda: True)
    assert result.action == 'query' and result.binding.is_all_local
    assert result.binding.knowledge_base_id is None
    assert case.model.requests[0][0][-1]['content'] == '这个视频讲了什么事情'
    assert case.router(env, source_checkpoint=lambda: True).binding == result.binding
    assert len(case.model.requests) == 1


def test_model_save_decision_on_plain_reply_cannot_grant_import_or_open_choice(followup_case):
    case = followup_case.c
    env = verified_reply(followup_case)
    case.model.decision = {'action': 'save', 'target_state': 'one', 'grant_id': case.grant['grant_id']}
    result = case.router(env, source_checkpoint=lambda: True)
    assert result.action == 'clarify'
    # The verified query-only input reaches the model. Its forbidden output
    # is then held; this is not the earlier missing-source-proof rejection.
    assert result.reason_code == 'semantic_response_invalid'
    assert not result.choice_options and result.choice_intent is None
    assert len(case.model.requests) == 1
    tools = case.model.requests[0][1]
    assert tools[0]['function']['parameters']['properties']['action']['enum'] == ['query', 'clarify']
    receipt = case.store.lookup_receipt(semantic_source_key(env))
    assert receipt['state'] == 'held' and receipt['dispatchable'] is False
    assert receipt['usage']['total_tokens'] == 20 and case.store.get_recorded_decision(env) is None
    assert case.router(env, source_checkpoint=lambda: True).action == 'clarify'
    assert len(case.model.requests) == 1


def test_followup_without_recorded_meaning_cannot_fall_back_to_keyword_rules(semantic_case):
    case = semantic_case
    env = plain_reply(case)
    task = {'id': 'isolated-unplanned-reply', 'envelope': env, 'binding': None}
    result = case.router.recorded_resolution(task)
    assert result.action != 'query' and result.action != 'save'
    assert result.reason_code == 'semantic_scope_unavailable'
    assert not case.model.requests


def test_followup_flag_without_verified_parent_proof_is_rejected_before_model(semantic_case):
    case = semantic_case
    env = plain_reply(case)
    result = case.router(env, source_checkpoint=lambda: True)
    assert result.action == 'clarify' and result.reason_code == 'semantic_scope_unavailable'
    assert not result.choice_options and result.choice_intent is None
    assert not case.model.requests and case.budget.summary()['all_time']['request_count'] == 0
    assert case.store.get_recorded_decision(env) is None


def test_native_authority_denies_followup_with_missing_private_receipt(semantic_case):
    case = semantic_case
    env = plain_reply(case)
    case.router.native_inbox = SimpleNamespace()
    assert case.router.native_authority_current({'envelope': env}) is False
    assert not case.model.requests
