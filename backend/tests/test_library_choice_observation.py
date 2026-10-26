"""Optional observation I/O uses temporary paths and fake pages only."""
from copy import deepcopy
import json
import os
from types import SimpleNamespace

import pytest

from app.mention_receiver import choice_browser as module
from app.mention_receiver.browser import ReceiverDetailError, ReceiverInterventionRequired
from tests.test_library_choice_browser_dom import session, A, B, ROOT, VID


@pytest.fixture
def observations(tmp_path, monkeypatch):
    base = tmp_path / 'assistant'
    base.mkdir()
    monkeypatch.setattr(module, 'CHOICE_OBSERVATION_BASE', base)
    return base


def enable(base, choice='a' * 32):
    (base / 'choice-observation-enabled.json').write_text(json.dumps({'choice_id': choice}), encoding='utf-8')


def records(base):
    path = base / 'choice-observations' / ('a' * 32 + '.jsonl')
    return [json.loads(line) for line in path.read_text(encoding='ascii').splitlines()]


def fake_receiver(*, detail_error=None, values=None):
    values = iter(values or [{'ok': True, 'details': [], 'ask_missing': False, 'expand_index': None}])
    keys, expansions = [], []
    def detail(*args, **kwargs):
        if detail_error is not None:
            raise detail_error
        return {'sender_profile_url': A, 'receiver_profile_url': B, 'video_id': VID,
                'native_comment_id': None, 'command_text': ROOT}
    control = SimpleNamespace(nth=lambda index: SimpleNamespace(click=lambda **_: expansions.append(index)))
    page = SimpleNamespace(goto=lambda *_, **__: None, wait_for_timeout=lambda _: None,
        evaluate=lambda *_, **__: next(values), locator=lambda _: control,
        keyboard=SimpleNamespace(press=lambda key: keys.append(key)))
    element = SimpleNamespace(evaluate=lambda *_, **__: True, click=lambda **_: expansions.append(0))
    page.evaluate_handle = lambda *_, **__: SimpleNamespace(as_element=lambda: element, dispose=lambda: None)
    return SimpleNamespace(page=page, expected_profile_url=B, identity=lambda: object(),
        notifications=lambda: [{'source_profile_url': A, 'exact_text': ROOT}], detail=detail,
        check_intervention=lambda: None, keys=keys, expansions=expansions)


def test_missing_or_different_opt_in_never_creates_a_log(observations):
    module._choice_observation({'choice_id': 'a' * 32}, 'root_open', 'started')
    enable(observations, 'b' * 32)
    module._choice_observation({'choice_id': 'a' * 32}, 'root_open', 'started')
    assert not (observations / 'choice-observations').exists()


@pytest.mark.parametrize('content', [
    '{}', '{"choice_id":"../escape"}', '{"choice_id":"' + 'A' * 32 + '"}',
    '{"choice_id":"' + 'a' * 32 + '","path":"elsewhere"}', 'null', '{bad json',
])
def test_invalid_opt_in_cannot_choose_a_path_or_enable_logging(observations, content):
    (observations / 'choice-observation-enabled.json').write_text(content, encoding='utf-8')
    module._choice_observation({'choice_id': 'a' * 32}, 'root_open', 'started')
    assert not (observations / 'choice-observations').exists()


def test_append_only_codes_and_counts_never_record_payload_or_error_text(observations):
    enable(observations)
    args = {'choice_id': 'a' * 32, 'root_text': 'PRIVATE BODY', 'cookie': 'PRIVATE COOKIE'}
    module._choice_observation(args, 'root_open', 'started', notice_count=2, cookie='SECRET', video_id_count=True)
    path = observations / 'choice-observations' / ('a' * 32 + '.jsonl')
    first = path.read_bytes()
    module._choice_observation(args, 'failure', 'PRIVATE PROVIDER MESSAGE', command_match_count=1, token='SECRET')
    assert path.read_bytes().startswith(first)
    data = records(observations)
    assert data[0] == {'component': module._OBSERVATION_COMPONENT, 'choice_id': 'a' * 32}
    assert data[1]['counts'] == {'notice_count': 2}
    assert data[2]['reason_code'] == 'unclassified_choice_failure'
    assert data[2]['counts'] == {'command_match_count': 1}
    assert all(value not in path.read_text() for value in ('PRIVATE', 'SECRET', 'cookie', 'token', 'root_text'))


def test_unknown_existing_log_is_not_adopted_or_overwritten(observations):
    enable(observations)
    folder = observations / 'choice-observations'
    folder.mkdir()
    path = folder / ('a' * 32 + '.jsonl')
    path.write_bytes(b'unknown existing data\n')
    module._choice_observation({'choice_id': 'a' * 32}, 'root_open')
    assert path.read_bytes() == b'unknown existing data\n'


def test_hardlinked_opt_in_and_log_are_not_used(observations):
    enable(observations)
    config = observations / 'choice-observation-enabled.json'
    other = observations / 'other-config.json'
    os.link(config, other)
    module._choice_observation({'choice_id': 'a' * 32}, 'root_open')
    assert not (observations / 'choice-observations').exists()
    other.unlink()
    module._choice_observation({'choice_id': 'a' * 32}, 'root_open')
    path = observations / 'choice-observations' / ('a' * 32 + '.jsonl')
    os.link(path, observations / 'other-log')
    before = path.read_bytes()
    module._choice_observation({'choice_id': 'a' * 32}, 'root_open')
    assert path.read_bytes() == before


def test_write_failure_does_not_change_success_or_original_detail_error(observations, monkeypatch):
    enable(observations)
    monkeypatch.setattr(module.os, 'write', lambda *args: (_ for _ in ()).throw(OSError('disk failed PRIVATE')))
    receiver = fake_receiver()
    assert module.read_choice_replies(receiver, session(), ['1'], keep_open=True) == []
    original = ReceiverDetailError('PRIVATE BODY', {'reason_code': 'choice_original_root_changed'})
    receiver = fake_receiver(detail_error=original)
    with pytest.raises(ReceiverDetailError) as caught:
        module.read_choice_replies(receiver, session(), ['1'], keep_open=True)
    assert caught.value is original


def test_root_detail_failure_records_only_fixed_reason_and_safe_counts(observations):
    enable(observations)
    original = ReceiverDetailError('PRIVATE BODY TOKEN', {'sender_match_count': 2,
        'visible_video_ids': ['123456', '789123'], 'raw_html': 'SECRET', 'cookie': 'SECRET'})
    receiver = fake_receiver(detail_error=original)
    with pytest.raises(ReceiverDetailError) as caught:
        module.read_choice_replies(receiver, session(), ['1'], keep_open=True)
    assert caught.value is original
    data = records(observations)
    assert [(row.get('stage'), row.get('reason_code')) for row in data[1:]] == [
        ('read_begin', 'started'), ('root_open', 'started'), ('root_identity', 'ok'),
        ('root_notifications', 'ok'), ('root_detail', 'started'), ('failure', 'receiver_detail_error')]
    assert data[-1]['counts'] == {'sender_match_count': 2, 'video_id_count': 2}
    assert all(value not in str(data) for value in ('PRIVATE', 'SECRET', '123456', '789123'))


def test_fixed_thread_reason_and_expansion_counts_survive_without_payload(observations):
    enable(observations)
    values = [{'ok': True, 'ask_missing': True, 'details': [], 'expand_index': 0,
               'counts': {'ask_count': 0, 'descendant_count': 0, 'stage': 'SECRET'}},
              {'ok': False, 'reason_code': 'choice_original_root_not_unique',
               'counts': {'original_root_count': 0, 'detail_ok': 0, 'raw_html': 'SECRET'}}]
    receiver = fake_receiver(values=deepcopy(values))
    with pytest.raises(ReceiverDetailError):
        module.read_choice_replies(receiver, session(), ['1'], keep_open=True)
    data = records(observations)[1:]
    assert receiver.expansions == [0] and receiver.keys == []
    assert any(row['stage'] == 'expand' and row['counts'] == {'expand_attempt': 1, 'expand_available': 1} for row in data)
    assert data[-1]['reason_code'] == 'choice_original_root_not_unique'
    assert next(row for row in reversed(data) if row['stage'] == 'thread_read')['counts'] == {'original_root_count': 0, 'detail_ok': 0}
    assert 'SECRET' not in str(data)


def test_intervention_remains_intervention_and_has_no_cleanup_keypress(observations):
    enable(observations)
    error = ReceiverInterventionRequired('PRIVATE VERIFICATION')
    receiver = fake_receiver(detail_error=error)
    with pytest.raises(ReceiverInterventionRequired) as caught:
        module.read_choice_replies(receiver, session(), ['1'], keep_open=True)
    assert caught.value is error and receiver.keys == []
    assert records(observations)[-1]['reason_code'] == 'intervention_required'


@pytest.mark.parametrize('code', ['original_notification_not_found',
    'original_notification_not_unique', 'original_candidate_unreadable',
    'original_target_not_unique'])
def test_shared_opener_fixed_failure_survives_without_raw_context(observations, code):
    enable(observations)
    original = ReceiverDetailError('PRIVATE RAW ERROR', {'reason_code': code,
        'candidate_count': 2, 'checked_count': 1, 'unreadable_count': 1,
        'final_verified': 0, 'page_open_count': 2, 'cookie': 'SECRET',
        'raw_url': 'https://private.example/token'})
    receiver = fake_receiver()
    receiver.open_original_from_notifications = lambda *a, **kw: (_ for _ in ()).throw(original)
    with pytest.raises(ReceiverDetailError) as caught:
        module.read_choice_replies(receiver, session(), ['1'], keep_open=True)
    assert caught.value is original
    data = records(observations)
    assert data[-1]['reason_code'] == code
    assert data[-1]['counts'] == {'candidate_count': 2, 'checked_count': 1,
        'unreadable_count': 1, 'final_verified': 0, 'page_open_count': 2}
    assert all(value not in str(data) for value in ('PRIVATE', 'SECRET', 'private.example', 'cookie', 'raw_url'))
