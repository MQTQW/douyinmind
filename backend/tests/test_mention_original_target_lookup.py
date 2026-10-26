"""Normal original lookup uses frozen full targets; fake pages, no network/UI."""
from copy import deepcopy

import pytest

from app.mention_receiver.browser import (
    NormalPageReceiver, ReceiverIdentity, ReceiverDetailError,
    ReceiverInterventionRequired, ReceiverPageError, SELF_URL,
)

A = 'https://www.douyin.com/user/lookup-synthetic-A'
B = 'https://www.douyin.com/user/lookup-synthetic-B'
C = 'https://www.douyin.com/user/lookup-synthetic-C'
VIDEO = '7999999000000000011'
OTHER = '7999999000000000022'
BODY = '@6 帮我加入到知识库'
TARGET = {'sender_profile_url': A, 'receiver_profile_url': B, 'video_id': VIDEO,
          'comment_text': BODY, 'native_comment_id': None, 'reference_id': 'frozen-reference'}


def notice(key, index=0, **changes):
    return {'source_profile_url': A, 'exact_text': BODY,
            'semantic_fingerprint': key, 'pre_index': index, **changes}


def detail(video=VIDEO, native=None, **changes):
    return {'ok': True, 'sender_profile_url': A, 'receiver_profile_url': B,
            'video_id': video, 'native_comment_id': native, 'command_text': BODY,
            'unique_count': 1, 'real_mention': True, 'sender_verified': True,
            'video_verified': True, **changes}


class LookupReached(Exception):
    pass


class Text:
    def and_(self, _other): return self
    def wait_for(self, **_kwargs): pass
    def count(self): return 1
    def scroll_into_view_if_needed(self, **_kwargs): pass


class Page:
    def __init__(self):
        self.navigations = []
        self.current_detail = None
        self.keyboard = self
        self.keys = []

    def goto(self, url, **_kwargs):
        assert url == SELF_URL
        self.navigations.append(url)

    def wait_for_timeout(self, _milliseconds): pass
    def press(self, value): self.keys.append(value)
    def get_by_text(self, _text, **_kwargs): return Text()
    def locator(self, selector):
        assert selector == ':visible', 'lookup must never acquire an editor/reply/send control'
        return Text()


def receiver(monkeypatch, notices, details):
    page = Page()
    value = NormalPageReceiver(page, '123456789', B)
    calls = []
    monkeypatch.setattr(value, 'identity', lambda: ReceiverIdentity('123456789', 'syntheticB'))
    monkeypatch.setattr(value, '_check_challenge', lambda: None)
    monkeypatch.setattr(value, 'notifications', lambda: deepcopy(notices() if callable(notices) else notices))
    def inspect(candidate, *, keep_open=False):
        assert keep_open is True
        calls.append(deepcopy(candidate))
        result = details(candidate, len(calls)) if callable(details) else details[candidate['semantic_fingerprint']]
        if isinstance(result, Exception): raise result
        page.current_detail = deepcopy(result)
        return deepcopy(result)
    monkeypatch.setattr(value, 'detail', inspect)
    return value, page, calls


def test_prepare_same_sender_text_different_videos_reaches_frozen_original_not_ambiguity(monkeypatch):
    value, page, calls = receiver(monkeypatch, [notice('wrong'), notice('right', 1)],
                                 {'wrong': detail(OTHER), 'right': detail()})
    def preflight(_target, _text): raise LookupReached()
    monkeypatch.setattr(value, '_reply_state', preflight)
    with pytest.raises(LookupReached): value.prepare_reply(TARGET, 'not-filled')
    assert page.current_detail['video_id'] == VIDEO
    assert [n['semantic_fingerprint'] for n in calls] == ['wrong', 'right', 'right']
    assert page.keys == []


def test_unique_full_video_target_wins_even_when_cover_fingerprint_looks_like_other_video(monkeypatch):
    wrong_key = '/cover/' + VIDEO
    value, page, calls = receiver(monkeypatch, [notice(wrong_key), notice('/unrelated-cover', 1)],
                                 {wrong_key: detail(OTHER), '/unrelated-cover': detail()})
    observed = value.open_original_from_notifications(TARGET)
    assert observed['video_id'] == VIDEO and page.current_detail == observed
    assert len(calls) == 3 and len(page.navigations) == 3
    assert value.last_original_lookup['candidate_count'] == 2
    assert value.last_original_lookup['checked_count'] == 2
    assert value.last_original_lookup['match_count'] == 1
    assert value.last_original_lookup['final_verified'] == 1


def test_more_than_three_candidates_refuses_without_opening_any(monkeypatch):
    value, page, calls = receiver(monkeypatch, [notice(str(i), i) for i in range(4)], {})
    with pytest.raises(ReceiverDetailError): value.open_original_from_notifications(TARGET)
    assert calls == [] and len(page.navigations) == 1


def test_identical_card_identity_is_not_resolved_by_first_or_newest_index(monkeypatch):
    value, _, calls = receiver(monkeypatch, [notice('same', 0), notice('same', 7)], {'same': detail()})
    with pytest.raises(ReceiverDetailError): value.open_original_from_notifications(TARGET)
    assert calls == []


def test_two_full_same_video_text_matches_without_native_cannot_be_disambiguated(monkeypatch):
    value, _, calls = receiver(monkeypatch, [notice('one'), notice('two', 1)],
                              {'one': detail(), 'two': detail()})
    with pytest.raises(ReceiverDetailError): value.open_original_from_notifications(TARGET)
    assert len(calls) == 2 and value.last_original_lookup['match_count'] == 2


def test_native_id_can_disambiguate_distinct_openable_cards(monkeypatch):
    value, _, calls = receiver(monkeypatch, [notice('one'), notice('two', 1)],
                              {'one': detail(native='111111'), 'two': detail(native='222222')})
    observed = value.open_original_from_notifications({**TARGET, 'native_comment_id': '222222'})
    assert observed['native_comment_id'] == '222222' and len(calls) == 3


@pytest.mark.parametrize('change', [
    {'video_id': OTHER}, {'native_comment_id': '111111'},
    {'receiver_profile_url': C}, {'sender_profile_url': C},
    {'command_text': BODY + ' changed'}, {'unique_count': 2}, {'real_mention': False},
])
def test_missing_or_wrong_frozen_detail_never_leaves_a_verified_target(monkeypatch, change):
    value, _, _ = receiver(monkeypatch, [notice('one')], {'one': detail(**change)})
    with pytest.raises(ReceiverDetailError): value.open_original_from_notifications(TARGET)
    assert value.last_original_lookup['final_verified'] == 0


def test_one_unreadable_candidate_prevents_proving_unique_among_others(monkeypatch):
    value, _, _ = receiver(monkeypatch, [notice('one'), notice('two', 1)],
                           {'one': detail(), 'two': ReceiverDetailError('synthetic', {})})
    with pytest.raises(ReceiverDetailError): value.open_original_from_notifications(TARGET)
    assert value.last_original_lookup['unreadable_count'] == 1


def test_final_reopen_checks_frozen_target_instead_of_reusing_prior_detail(monkeypatch):
    value, _, calls = receiver(monkeypatch, [notice('one')],
                              lambda _notice, count: detail() if count == 1 else detail(OTHER))
    with pytest.raises(ReceiverDetailError): value.open_original_from_notifications(TARGET)
    assert len(calls) == 2 and value.last_original_lookup['final_verified'] == 0


def test_caller_cannot_retarget_the_frozen_fields_during_a_candidate_read(monkeypatch):
    original = deepcopy(TARGET)
    def inspect(_notice, _count):
        original['video_id'] = OTHER
        return detail(OTHER)
    value, _, _ = receiver(monkeypatch, [notice('one')], inspect)
    with pytest.raises(ReceiverDetailError): value.open_original_from_notifications(original)
    assert value.last_original_lookup['final_verified'] == 0


def test_notice_index_drift_is_refreshed_before_final_open(monkeypatch):
    counter = [0]
    def candidates():
        counter[0] += 1
        return [notice('one', counter[0] * 7)]
    value, _, calls = receiver(monkeypatch, candidates, {'one': detail()})
    value.open_original_from_notifications(TARGET)
    assert [n['pre_index'] for n in calls] == [7, 14]


def test_candidate_set_change_is_not_silently_accepted_after_partial_check(monkeypatch):
    snapshots = iter([[notice('one')], [notice('one'), notice('new', 1)]])
    value, _, _ = receiver(monkeypatch, lambda: next(snapshots), {'one': detail()})
    with pytest.raises(ReceiverDetailError): value.open_original_from_notifications(TARGET)
    assert value.last_original_lookup['snapshot_changes'] == 1


def test_stop_during_candidate_read_prevents_navigation_and_cleanup(monkeypatch):
    stopped = [False]
    def inspect(_notice, _count): stopped[0] = True; return detail()
    value, page, calls = receiver(monkeypatch, [notice('one')], inspect)
    value.choice_should_stop = lambda: stopped[0]
    with pytest.raises(ReceiverPageError): value.open_original_from_notifications(TARGET)
    assert len(calls) == len(page.navigations) == 1 and page.keys == []


def test_challenge_during_candidate_read_is_preserved_without_second_open(monkeypatch):
    value, page, calls = receiver(monkeypatch, [notice('one')],
                                 {'one': ReceiverInterventionRequired('synthetic challenge')})
    with pytest.raises(ReceiverInterventionRequired): value.open_original_from_notifications(TARGET)
    assert len(calls) == 1 and len(page.navigations) == 1 and page.keys == []


def test_page_error_exposes_only_fixed_counts_and_reason(monkeypatch):
    value, page, _ = receiver(monkeypatch, [notice('one')], {'one': detail()})
    def unavailable(*_args, **_kwargs): raise RuntimeError('private-url-and-token-value')
    monkeypatch.setattr(page, 'goto', unavailable)
    with pytest.raises(ReceiverDetailError) as error:
        value.open_original_from_notifications(TARGET)
    assert error.value.diagnostics['reason_code'] == 'original_lookup_unavailable'
    assert 'private-url-and-token-value' not in str(error.value.diagnostics)
    assert value.last_original_lookup['final_verified'] == 0


@pytest.mark.parametrize('limit', [0, 4, True, None])
def test_untrusted_limit_cannot_expand_bounded_lookup(monkeypatch, limit):
    value, page, calls = receiver(monkeypatch, [notice('one')], {'one': detail()})
    with pytest.raises(ValueError): value.open_original_from_notifications(TARGET, max_candidates=limit)
    assert page.navigations == calls == []


def test_last_lookup_exports_only_fixed_reason_and_counts(monkeypatch):
    value, _, _ = receiver(monkeypatch, [notice('one')], {'one': detail()})
    value.open_original_from_notifications(TARGET)
    safe = value.last_original_lookup
    assert set(safe) == {'candidate_limit', 'candidate_count', 'checked_count', 'match_count',
                         'unreadable_count', 'final_rechecks', 'final_verified', 'snapshot_changes',
                         'page_open_count', 'lookup_stage_index', 'reason_code'}
    assert all(type(v) is int for k, v in safe.items() if k != 'reason_code')
    assert not any(value in str(safe) for value in [A, B, VIDEO, BODY, 'send_token'])


def test_native_correlation_preserves_missing_dom_id_and_labels_semantic_only(monkeypatch):
    value, page, calls = receiver(monkeypatch, [notice('one')], {'one': detail()})
    target = {**TARGET, 'native_comment_id': '7999999000000000033'}
    observed = value.open_original_from_notifications(target, native_candidate_correlation=True)
    assert observed['native_comment_id'] is None
    assert observed['native_correlation'] == {
        'source_native_comment_id': target['native_comment_id'],
        'observed_native_comment_id': None, 'quality': 'semantic_only',
    }
    assert target['native_comment_id'] == '7999999000000000033'
    assert observed['native_ui_notice'] == {'source_profile_url': A, 'exact_text': BODY,
                                            'semantic_fingerprint': 'one'}
    assert len(calls) == len(page.navigations) == 2 and page.keys == []


def test_native_correlation_equal_public_id_records_actual_native_equality(monkeypatch):
    native = '7999999000000000033'
    value, _, _ = receiver(monkeypatch, [notice('one')], {'one': detail(native=native)})
    observed = value.open_original_from_notifications({**TARGET, 'native_comment_id': native},
                                                       native_candidate_correlation=True)
    assert observed['native_comment_id'] == native
    assert observed['native_correlation']['quality'] == 'native_equal'


@pytest.mark.parametrize('observed_id', [None, '7999999000000000044'])
def test_legacy_lookup_remains_strict_when_native_id_is_known(monkeypatch, observed_id):
    value, _, _ = receiver(monkeypatch, [notice('one')], {'one': detail(native=observed_id)})
    with pytest.raises(ReceiverDetailError):
        value.open_original_from_notifications({**TARGET, 'native_comment_id': '7999999000000000033'})
    assert value.last_original_lookup['final_verified'] == 0


def test_native_correlation_rejects_conflicting_public_id(monkeypatch):
    value, _, _ = receiver(monkeypatch, [notice('one')], {'one': detail(native='7999999000000000044')})
    with pytest.raises(ReceiverDetailError):
        value.open_original_from_notifications({**TARGET, 'native_comment_id': '7999999000000000033'},
                                                native_candidate_correlation=True)
    assert value.last_original_lookup['final_verified'] == 0


def test_native_correlation_never_chooses_one_of_two_semantically_equal_comments(monkeypatch):
    value, _, calls = receiver(monkeypatch, [notice('one'), notice('two', 1)],
                              {'one': detail(), 'two': detail()})
    with pytest.raises(ReceiverDetailError):
        value.open_original_from_notifications({**TARGET, 'native_comment_id': '7999999000000000033'},
                                                native_candidate_correlation=True)
    assert len(calls) == 2 and value.last_original_lookup['match_count'] == 2


def test_native_correlation_known_other_id_does_not_identify_an_unnumbered_sibling(monkeypatch):
    value, _, calls = receiver(monkeypatch, [notice('known-other'), notice('unknown', 1)],
                              {'known-other': detail(native='7999999000000000044'), 'unknown': detail()})
    with pytest.raises(ReceiverDetailError) as error:
        value.open_original_from_notifications({**TARGET, 'native_comment_id': '7999999000000000033'},
                                                native_candidate_correlation=True)
    assert len(calls) == 2 and value.last_original_lookup['final_verified'] == 0
    assert error.value.diagnostics['reason_code'] == 'original_native_semantic_ambiguous'


@pytest.mark.parametrize('first, final', [(None, '7999999000000000033'), ('7999999000000000033', None)])
def test_native_correlation_rejects_native_quality_change_on_final_reopen(monkeypatch, first, final):
    value, _, _ = receiver(monkeypatch, [notice('one')],
                           lambda _notice, count: detail(native=first if count == 1 else final))
    with pytest.raises(ReceiverDetailError):
        value.open_original_from_notifications({**TARGET, 'native_comment_id': '7999999000000000033'},
                                                native_candidate_correlation=True)
    assert value.last_original_lookup['final_verified'] == 0


@pytest.mark.parametrize('flag', [1, 'true', {}, None])
def test_native_correlation_mode_is_not_coerced_from_untrusted_values(monkeypatch, flag):
    value, page, calls = receiver(monkeypatch, [notice('one')], {'one': detail()})
    with pytest.raises(ValueError):
        value.open_original_from_notifications(TARGET, native_candidate_correlation=flag)
    assert calls == page.navigations == []


def test_native_correlation_requires_a_real_source_native_id(monkeypatch):
    value, page, calls = receiver(monkeypatch, [notice('one')], {'one': detail()})
    with pytest.raises(ReceiverDetailError):
        value.open_original_from_notifications(TARGET, native_candidate_correlation=True)
    assert calls == page.navigations == []
