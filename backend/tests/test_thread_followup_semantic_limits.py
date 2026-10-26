"""Private planning receipts cannot grant import capability to a plain reply.

These tests exercise semantic policy only, not browser proof or live delivery.
"""
from types import SimpleNamespace

from tests.test_semantic_command_resolution import case


def plain_reply(case):
    # Actual followup evidence is tested by the separate browser/contract suite.
    case.env['comment_text'] = '这个视频讲了什么事情'
    case.env['evidence']['source_kind'] = 'conversation_followup'
    return case.env


def test_followup_query_defaults_all_own_material_and_never_inherits_save_library(case):
    env = plain_reply(case)
    case.model.decision = {'action': 'query', 'target_state': 'unspecified', 'grant_id': None}
    result = case.router(env, source_checkpoint=lambda: True)
    assert result.action == 'query' and result.binding.is_all_local
    assert result.binding.knowledge_base_id is None
    assert case.model.requests[0][0][-1]['content'] == '这个视频讲了什么事情'
    assert case.router(env, source_checkpoint=lambda: True).binding == result.binding
    assert len(case.model.requests) == 1


def test_model_save_decision_on_plain_reply_cannot_grant_import_or_open_choice(case):
    result = case.router(plain_reply(case), source_checkpoint=lambda: True)
    assert result.action == 'clarify'
    assert result.reason_code == 'followup_save_requires_mention'
    assert not result.choice_options and result.choice_intent is None
    assert len(case.model.requests) == 1


def test_followup_without_recorded_meaning_cannot_fall_back_to_keyword_rules(case):
    env = plain_reply(case)
    task = {'id': 'isolated-unplanned-reply', 'envelope': env, 'binding': None}
    result = case.router.recorded_resolution(task)
    assert result.action != 'query' and result.action != 'save'
    assert result.reason_code == 'semantic_scope_unavailable'
    assert not case.model.requests


def test_native_authority_denies_followup_with_missing_private_receipt(case):
    env = plain_reply(case)
    case.router.native_inbox = SimpleNamespace()
    assert case.router.native_authority_current({'envelope': env}) is False
    assert not case.model.requests
