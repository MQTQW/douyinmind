from copy import deepcopy

import pytest

from app.mention_agent.choice_evidence import make_choice_envelope, validate_choice_envelope
from app.mention_agent.contracts import Binding

A = 'https://www.douyin.com/user/choice-sender'
B = 'https://www.douyin.com/user/choice-receiver'
V = '1234567890123456789'


def sample(*, native=None, parent='unique_pending_choice_in_verified_root', mention=False):
    binding = Binding('choice-selected', 'local-instance', A, B, 7, True)
    proof = {
        'source_kind': 'library_choice_reply', 'evidence_version': 1,
        'choice_id': 'a' * 32, 'instance_id': binding.instance_id,
        'real_mention': mention, 'sender_verified': True, 'receiver_verified': True,
        'video_verified': True, 'unique_count': 1,
        'direct_reply_to_ask': parent == 'verified_ask_parent',
        'root_chain_verified': True, 'parent_scope': parent,
        'sender_profile_url': A, 'receiver_profile_url': B, 'video_id': V,
        'root_reference_id': 'reference:actual-root-ui', 'root_comment_text': '@B 请保存当前视频',
        'ask_ref': 'ask:durable-reply-intent', 'ask_reply_ref': 'verified-ui:actual-B-prompt',
        'ask_text': '你目前有2个知识库：1.编程库；2.经济库。请回复库名或序号。',
        'selection_reference_id': 'will-be-derived', 'selection_text': '编程库',
        'observed_at_utc': '2026-10-22T06:30:00+00:00',
        'unique_pending_choice': True, 'ask_visible_unique': True,
        'receiver_reply_target_verified': True, 'native_comment_id': native,
    }
    notice = {'source_profile_url': A, 'exact_text': '编程库', 'semantic_fingerprint': 'verified-root-cover'}
    detail = {'ok': True, 'command_text': '编程库', 'sender_profile_url': A,
              'receiver_profile_url': B, 'video_id': V, 'unique_count': 1,
              'native_comment_id': native, 'source_url': 'https://www.douyin.com/video/' + V,
              'evidence': proof}
    return binding, notice, detail


def built(**kwargs):
    binding, notice, detail = sample(**kwargs)
    return binding, make_choice_envelope(notice, detail, binding, 'isolated')


@pytest.mark.parametrize('parent', ['verified_ask_parent', 'unique_pending_choice_in_verified_root'])
@pytest.mark.parametrize('mention', [False, True])
def test_selection_preserves_actual_body_and_mention_observation(parent, mention):
    binding, envelope = built(parent=parent, mention=mention)
    assert envelope['comment_text'] == '编程库'
    assert envelope['evidence']['real_mention'] is mention
    assert envelope['native_comment_id'] is None
    assert envelope['reference_quality'] == 'verified_ui'
    assert validate_choice_envelope(envelope, binding) == envelope


def test_native_reference_does_not_invent_a_platform_id():
    binding, envelope = built(native='9876543210123456789')
    assert envelope['native_comment_id'] == '9876543210123456789'
    assert envelope['reference_quality'] == 'native'
    assert validate_choice_envelope(envelope, binding) == envelope


@pytest.mark.parametrize('key,value', [
    ('sender_verified', False), ('receiver_verified', False), ('video_verified', False),
    ('unique_count', True), ('unique_count', 2), ('root_chain_verified', False),
    ('unique_pending_choice', False), ('ask_visible_unique', False),
    ('receiver_reply_target_verified', False), ('direct_reply_to_ask', True),
    ('real_mention', 'true'), ('evidence_version', True), ('choice_id', 'not-a-choice'),
    ('instance_id', 'different-instance'), ('selection_text', '@B 保存视频'),
    ('selection_reference_id', 'reference:another-comment'),
    ('sender_profile_url', B), ('receiver_profile_url', A), ('video_id', '1111111111111111111'),
    ('observed_at_utc', '2026-10-22T06:30:00'), ('source_kind', 'unverified_reply'),
])
def test_incomplete_or_conflicting_selection_proof_is_rejected(key, value):
    binding, envelope = built()
    envelope['evidence'][key] = value
    with pytest.raises(ValueError):
        validate_choice_envelope(envelope, binding)


def test_same_ui_text_under_another_prompt_has_a_different_source_reference():
    binding, notice, detail = sample()
    first = make_choice_envelope(notice, detail, binding, 'isolated')
    detail['evidence']['ask_reply_ref'] = 'verified-ui:another-observed-prompt'
    second = make_choice_envelope(notice, detail, binding, 'isolated')
    assert first['reference_id'] != second['reference_id']
    assert first['command_id'] != second['command_id']


@pytest.mark.parametrize('key,value', [
    ('root_reference_id', 'reference:other-root'), ('root_comment_text', '@B 另一条原请求'),
    ('ask_ref', 'ask:another-intent'), ('ask_reply_ref', 'verified-ui:another-prompt'),
    ('ask_text', '另一条问库消息'), ('choice_id', 'b' * 32),
])
def test_choice_store_context_cannot_be_switched_to_another_parent(key, value):
    binding, envelope = built()
    proof = envelope['evidence']
    root = {'sender_profile_url': A, 'receiver_profile_url': B, 'video_id': V,
            'source_mode': 'isolated', 'reference_id': proof['root_reference_id'],
            'comment_text': proof['root_comment_text'], 'evidence': {'real_mention': True}}
    context = {'root_binding': binding, 'root_envelope': root,
               'ask_ref': proof['ask_ref'], 'ask_reply_ref': proof['ask_reply_ref'],
               'ask_text': proof['ask_text'], 'choice_id': proof['choice_id']}
    if key.startswith('root_'):
        root['reference_id' if key == 'root_reference_id' else 'comment_text'] = value
    else:
        context[key] = value
    with pytest.raises(ValueError):
        validate_choice_envelope(envelope, **context)


def test_nonlive_root_cannot_be_relabelled_as_live_choice():
    binding, envelope = built()
    proof = envelope['evidence']
    root = {'sender_profile_url': A, 'receiver_profile_url': B, 'video_id': V,
            'source_mode': 'live_ui', 'reference_id': proof['root_reference_id'],
            'comment_text': proof['root_comment_text'], 'evidence': {'real_mention': True}}
    with pytest.raises(ValueError, match='original_save_root'):
        validate_choice_envelope(envelope, root_binding=binding, root_envelope=root)


def test_a_readonly_all_scope_cannot_be_used_as_selected_import_library():
    _, envelope = built()
    binding = {'binding_id': 'all-readonly', 'instance_id': 'local-instance',
               'sender_profile_url': A, 'receiver_profile_url': B,
               'knowledge_base_id': None, 'active': True}
    with pytest.raises(ValueError, match='selected_library_binding'):
        validate_choice_envelope(envelope, binding)


def test_builder_rejects_notice_body_differing_from_actual_selection():
    binding, notice, detail = sample()
    notice['exact_text'] = '经济库'
    with pytest.raises(ValueError, match='notice_detail_conflict'):
        make_choice_envelope(notice, detail, binding, 'isolated')


def test_extra_proof_fields_cannot_smuggle_credentials_or_authority():
    binding, envelope = built()
    envelope['evidence']['authorization_override'] = True
    with pytest.raises(ValueError, match='proof_fields'):
        validate_choice_envelope(envelope, binding)


def test_visible_reply_prefix_is_separate_from_the_preserved_selection_body():
    binding, notice, detail = sample()
    text = '回复@6：存到编程库'
    notice['exact_text'] = detail['command_text'] = text
    detail['evidence'].update(selection_text=text, selection_value='存到编程库', receiver_reply_label='@6')
    envelope = make_choice_envelope(notice, detail, binding, 'isolated')
    assert envelope['comment_text'] == text
    assert envelope['evidence']['selection_value'] == '存到编程库'
    assert envelope['evidence']['real_mention'] is False
    bad = deepcopy(envelope)
    bad['evidence']['selection_value'] = '存到经济库'
    with pytest.raises(ValueError, match='not_derived'):
        validate_choice_envelope(bad, binding)


@pytest.mark.parametrize('field', ['selection_value', 'receiver_reply_label'])
def test_reply_prefix_fields_cannot_be_only_half_provided(field):
    binding, envelope = built()
    envelope['evidence'][field] = '编程库' if field == 'selection_value' else '@6'
    with pytest.raises(ValueError, match='must_pair'):
        validate_choice_envelope(envelope, binding)
