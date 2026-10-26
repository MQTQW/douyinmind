"""Intent can select a relevance intersection, never a user/library authorization."""
import json

import pytest

from app.assistant import backend_knowledge as transport
from tests.test_all_local_evidence_transport import case, request, result, VIDEOS
from tests.test_assistant_evidence_route import fixture, body, VIDEO

URL = '/api/assistant/evidence/query'


def cross_video_question(case):
    case.task['envelope']['video_id'] = VIDEOS[2]
    case.task['envelope']['comment_text'] = '这个视频里的观点，在我以前收藏的视频里有没有类似的？'


def test_explicit_authorized_focus_can_find_related_owned_material_in_other_videos(case):
    cross_video_question(case)
    reply = case.client.post(URL, json=request(case, focus='authorized_scope'))
    assert reply.status_code == 200
    assert [e['platform_item_id'] for e in reply.json()['evidence']] == [VIDEOS[0], VIDEOS[2]]
    assert case.calls[0][1:4] == (None, None, 'all_local')


def test_current_focus_comes_from_frozen_original_even_without_literal_video_phrase(case):
    case.task['envelope']['video_id'] = VIDEOS[2]
    case.task['envelope']['comment_text'] = '能给我讲讲这里主要说的事吗？'
    reply = case.client.post(URL, json=request(case, focus='current_video'))
    assert reply.status_code == 200
    assert [e['platform_item_id'] for e in reply.json()['evidence']] == [VIDEOS[2]]
    assert case.calls[0][2] == VIDEOS[2]


def test_old_client_without_focus_keeps_legacy_current_video_intersection(case):
    cross_video_question(case)
    reply = case.client.post(URL, json=request(case))
    assert reply.status_code == 200
    assert [e['platform_item_id'] for e in reply.json()['evidence']] == [VIDEOS[2]]


@pytest.mark.parametrize('during', [False, True])
def test_explicit_relevance_choice_does_not_bypass_revoked_authorization(case, during):
    cross_video_question(case)
    if during:
        case.state['hook'] = lambda: case.state.update(active=False)
    else:
        case.state['active'] = False
    reply = case.client.post(URL, json=request(case, focus='authorized_scope'))
    assert reply.status_code == 403
    assert len(case.calls) == int(during)


def test_authorized_focus_for_a_library_stays_in_that_library_not_all_local(fixture):
    reply = fixture.client.post(URL, json=body(fixture, focus='authorized_scope'))
    assert reply.status_code == 200
    assert reply.json()['knowledge_base_id'] == 6
    assert [e['platform_item_id'] for e in reply.json()['evidence']] == [VIDEO]
    assert fixture.calls[0][1:3] == (6, None)


@pytest.mark.parametrize('extra', [
    {'focus': 'all_users'}, {'focus': None}, {'focus': True},
    {'focus': 'authorized_scope', 'knowledge_base_id': 99},
    {'focus': 'authorized_scope', 'query_scope': 'all_local'},
    {'focus': 'authorized_scope', 'user_id': 'different-user'},
    {'focus': 'authorized_scope', 'video_id': VIDEOS[0]},
])
def test_focus_enum_cannot_smuggle_owner_library_or_arbitrary_video(case, extra):
    assert case.client.post(URL, json=request(case, **extra)).status_code == 422
    assert not case.calls


def test_conflicting_explicit_scope_and_video_narrowing_are_rejected(case):
    cross_video_question(case)
    reply = case.client.post(URL, json=request(case, focus='authorized_scope', focus_video_id=VIDEOS[2]))
    assert reply.status_code == 422
    assert not case.calls


def test_current_mode_does_not_allow_another_requested_video(case):
    cross_video_question(case)
    reply = case.client.post(URL, json=request(case, focus='current_video', focus_video_id=VIDEOS[0]))
    assert reply.status_code == 403
    assert not case.calls


def test_explicit_focus_does_not_reuse_an_old_execution_claim(case):
    case.task['execution_token'] = 'c' * 48
    assert case.client.post(URL, json=request(case, focus='authorized_scope')).status_code == 403
    assert not case.calls


def test_transport_explicit_focus_uses_fixed_endpoint_and_no_model_chosen_ids(case, monkeypatch):
    bodies = []
    response_value = result()
    response_value['evidence'] = response_value['evidence'][:2]

    class Response:
        headers = {'Content-Type': 'application/json'}
        def __enter__(self): return self
        def __exit__(self, *_): pass
        def getcode(self): return 200
        def geturl(self): return transport.EVIDENCE_URL
        def read(self, _): return json.dumps(response_value, ensure_ascii=False).encode('utf-8')

    class Opener:
        def open(self, req, timeout):
            assert req.full_url == transport.EVIDENCE_URL
            bodies.append(json.loads(req.data))
            return Response()

    monkeypatch.setattr(transport.urllib.request, 'build_opener', lambda *_: Opener())
    tools = transport.BackendKnowledgeTools(case.database, None, case.task['id'],
        focus_video_id=VIDEOS[2], execution_token=case.task['execution_token'], query_scope='all_local')
    expanded = tools.query_with_focus('找找我以前资料中的类似观点', focus='authorized_scope')
    assert len(expanded['evidence']) == 2
    assert bodies[0] == {'task_id': case.task['id'], 'question': '找找我以前资料中的类似观点', 'focus': 'authorized_scope'}
    tools.query_with_focus('说说这里', focus='current_video')
    assert bodies[1]['focus'] == 'current_video' and 'focus_video_id' not in bodies[1]
    narrowed = tools.query('原来的默认查询')
    assert bodies[2]['focus_video_id'] == VIDEOS[2] and 'focus' not in bodies[2]
    assert [e['platform_item_id'] for e in narrowed['evidence']] == [VIDEOS[2]]


@pytest.mark.parametrize('focus', [None, True, {}, 'all_users'])
def test_transport_invalid_focus_is_rejected_before_any_http(case, monkeypatch, focus):
    monkeypatch.setattr(transport.urllib.request, 'build_opener', lambda *_: pytest.fail('unexpected request'))
    tools = transport.BackendKnowledgeTools(case.database, None, case.task['id'],
        execution_token=case.task['execution_token'], query_scope='all_local')
    with pytest.raises(ValueError, match='invalid_evidence_focus_mode'):
        tools.query_with_focus('问题', focus=focus)
