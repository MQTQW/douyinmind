"""Display-only menu shortening; original scopes and v1 receipts stay frozen."""
from copy import deepcopy
import re

import pytest

from app.assistant import choice_controller as controller_module
from app.assistant.choice_controller import ChoiceController
from app.assistant.choice_intent import choice_prompt, choice_prompt_v2, parse_library_selection
from app.assistant.commands import CommandResolver
from app.assistant.reply_style import reply_body_budget, style_reply_text
from tests.test_assistant_choice_controller import case, publish_fact, A, B
from tests.test_mention_agent_store import envelope


def options(count=2):
    return [{'knowledge_base_id':i + 7, 'name':f'第{i + 1}个原名_' + '资料' * 59,
             'ordinal':i + 1, 'can_import':True} for i in range(count)]


@pytest.mark.parametrize('prefix', ['', '少爷，', '称呼' * 8 + '，'])
def test_v2_long_names_keep_visible_ordinals_inside_total_prefix_budget_without_new_aliases(prefix):
    original = options()
    original[0]['ordinal'], original[1]['ordinal'] = 2, 9
    before = deepcopy(original)
    body = choice_prompt_v2(original, catalog_count=2, max_chars=reply_body_budget(280, prefix))
    final = style_reply_text(body, prefix, maximum=280)
    assert len(final) <= 280 and '2. ' in body and '9. ' in body and '1. ' not in body
    assert '…' in body and '完整库名' in body and '本机' in body
    labels = re.findall(r'(?:[：；])(\d+)\. ([^；。]+)', body)
    assert [int(number) for number, _ in labels] == [2, 9]
    for number, label in labels:
        assert label.endswith('…') and parse_library_selection(label, original) is None
        assert parse_library_selection(number, original)['ordinal'] == int(number)
    assert parse_library_selection(original[0]['name'], original) == original[0]
    assert original == before


def test_v2_filtered_options_never_renumber_the_original_catalog_positions():
    catalog = [
        {'knowledge_base_id':1, 'name':'只读甲', 'can_import':False},
        {'knowledge_base_id':2, 'name':'可存乙', 'can_import':True},
        {'knowledge_base_id':3, 'name':'只读丙', 'can_import':False},
        {'knowledge_base_id':4, 'name':'可存丁', 'can_import':True},
    ]
    body = choice_prompt_v2(catalog, catalog_count=4)
    assert '2. 可存乙' in body and '4. 可存丁' in body
    assert '1. ' not in body and '3. ' not in body and '只读' not in body


def test_v2_omitted_option_count_and_full_name_instructions_are_inside_budget():
    catalog = options(8)
    body = choice_prompt_v2(catalog, catalog_count=8, max_chars=160)
    assert len(body) <= 160 and '另有5个未列出' in body
    assert all(f'{i}. ' in body for i in range(1, 4)) and '4. ' not in body
    assert '回上面序号' in body and '任一完整库名' in body and '全部目录' in body
    assert parse_library_selection(catalog[-1]['name'], catalog) == catalog[-1]


def test_v2_accepts_maximum_catalog_and_name_lengths_without_expanding_public_text():
    catalog = [{'knowledge_base_id':i, 'name':str(i) + '名' * (128 - len(str(i))),
                'ordinal':i, 'can_import':True} for i in range(1, 129)]
    before = deepcopy(catalog)
    body = choice_prompt_v2(catalog, catalog_count=128, max_chars=263)
    assert len(body) <= 263 and '128个知识库' in body and '另有125个未列出' in body
    assert all(f'{i}. ' in body for i in range(1, 4)) and catalog == before


def test_v2_zero_authorized_libraries_gives_a_bounded_nonexecuting_explanation():
    body = choice_prompt_v2([{'knowledge_base_id':1,'name':'不能展示的只读库','can_import':False}],
                            catalog_count=1, max_chars=263)
    assert '授权' in body and '先不存' in body and '不能展示' not in body
    with pytest.raises(ValueError, match='library_choice_prompt_budget_exceeded'):
        choice_prompt_v2([], catalog_count=0, max_chars=10)


@pytest.mark.parametrize('budget', [0, 10, 20, -1, True])
def test_v2_never_drops_the_ordinal_to_force_an_unusable_small_budget(budget):
    with pytest.raises(ValueError, match='library_choice_prompt_budget_exceeded'):
        choice_prompt_v2(options(), catalog_count=2, max_chars=budget)


def create_root(c, *, version, prefix='', number=1, reference_prefix=None):
    env = envelope(sender_profile_url=A, receiver_profile_url=B, comment_text='@助手 请保存这个视频。',
                   reference_id='menu-version-root-' + str(number), command_id='menu-version-command-' + str(number))
    resolution = CommandResolver(c.registry).resolve(env)
    task = c.queue.enqueue(resolution.binding, env, kind='clarify', user_id=resolution.user_id,
                           device_id=resolution.device_id, authorization=resolution.authorization)
    c.history.bind_task(task['id'], resolution.binding, env, env['reference_id'],
                        {'quality':'current_comment','root_reference_id':env['reference_id']})
    if version == 2 and reference_prefix is None:
        return task, c.controller.prepare_ask(task, resolution, reply_prefix=prefix), resolution
    frozen = c.agent.freeze_reply_prefix('assistant:' + task['id'], prefix)
    text = c.controller._menu(resolution, frozen, version=version)
    reference = c.controller._ask_ref(task, resolution, text, version=version)
    if reference_prefix is not None:
        reference = reference_prefix + reference.rsplit(':', 1)[1]
    session = c.store.create(root_task_id=task['id'], root_binding=task['binding'], root_envelope=env,
                             ask_ref=reference, ask_text=text, options=c.controller._eligible_options(resolution),
                             authorization=task['authorization'])
    return task, session, resolution


def test_v1_original_long_name_fallback_is_unchanged_and_never_upgraded_on_replay(case, monkeypatch):
    case.catalog[0]['name'] = '旧甲' + '名' * 126
    case.catalog[1]['name'] = '旧乙' + '名' * 126
    task, old, resolution = create_root(case, version=1)
    old = case.store.get(old['choice_id'])
    assert old['ask_ref'].startswith('choice-ask-intent:')
    assert '1. ' not in old['ask_text'] and case.catalog[0]['name'] in old['ask_text']
    assert old['ask_text'] == choice_prompt(resolution.choice_options, catalog_count=resolution.catalog_count,
                                            catalog_scope=resolution.catalog_scope, max_chars=280)
    before = case.store.path.read_bytes()
    def forbidden(*args, **kwargs):
        pytest.fail('Existing v1 menu must never enter v2 rendering')
    monkeypatch.setattr(controller_module, 'choice_prompt_v2', forbidden)
    assert case.controller.prepare_ask(task, resolution) == old
    assert case.store.path.read_bytes() == before


@pytest.mark.parametrize('version', [1, 2])
def test_unknown_publication_keeps_its_version_body_hash_and_once_barrier(case, version):
    task, session, resolution = create_root(case, version=version)
    publish_fact(case, task, session, outcome='delivery_unknown')
    old = case.store.get(session['choice_id'])
    assert old['state'] == 'ask_unknown'
    reply = deepcopy(case.agent.get_reply(case.agent.list_tasks()[0]['task_id']))
    for _ in range(2):
        assert case.controller.prepare_ask(task, resolution) == old
        assert case.controller.before_publish(task) is False
    assert case.agent.get_reply(reply['task_id']) == reply
    assert case.store.get(session['choice_id']) == old


def test_new_v2_menu_keeps_full_frozen_options_and_one_original_publication(case):
    case.catalog[0]['name'] = '首先_Git#' + '甲' * 121
    case.catalog[1]['name'] = '最后_笔记#' + '乙' * 121
    task, session, resolution = create_root(case, version=2, prefix='少爷，')
    assert session['ask_ref'].startswith('choice-ask-intent-v2:')
    assert len(session['ask_text']) <= 280 and '…' in session['ask_text']
    assert [item['name'] for item in session['options']] == [item['name'] for item in case.catalog]
    original = deepcopy(session)
    publish_fact(case, task, session)
    waiting = case.store.get(session['choice_id'])
    assert waiting['state'] == 'waiting_library'
    assert waiting['ask_text'] == original['ask_text'] and waiting['ask_ref'] == original['ask_ref']
    assert waiting['options'] == original['options']
    assert case.controller.before_publish(task) is False
    before = case.store.path.read_bytes()
    assert case.controller.prepare_ask(task, resolution) == waiting
    assert case.store.path.read_bytes() == before and len(case.agent.list_tasks()) == 1


def test_unknown_menu_version_fails_closed_without_replacing_old_text_or_reference(case):
    task, session, resolution = create_root(case, version=1, reference_prefix='choice-ask-intent-v9:')
    assert case.controller._fresh_root(session) is None
    assert case.controller.prepare_ask(task, resolution) is None
    held = case.store.get(session['choice_id'])
    assert held['state'] == 'needs_review'
    assert held['ask_ref'] == session['ask_ref'] and held['ask_text'] == session['ask_text']
    assert held['options'] == session['options'] and not case.agent.list_tasks()


@pytest.mark.parametrize('reference', ['choice-ask-intent-v2:' + 'x' * 64,
                                     'choice-ask-intent-v3:' + 'a' * 64, '', None])
def test_only_known_versions_and_exact_digest_shape_select_a_renderer(reference):
    assert ChoiceController._ask_version(reference) is None
