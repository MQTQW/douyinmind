"""Temporary ledgers + original documents; no query/model/browser/real HTTP."""
from copy import deepcopy
import hashlib
import json
import os
from types import SimpleNamespace

import pytest

from app.assistant.operator_draft_service import (OperatorDraftService, OperatorDraftError,
    operator_snapshot_digest, existing_agent_readonly, fetch_operator_draft_bundle, validate_operator_proposal)
from app.mention_agent.contracts import Binding
from tests.test_operator_query_draft_store import eligible, QUEUE_ID


def case(tmp_path):
    c = eligible(tmp_path)
    snapshot = c.store.operator_query_draft_snapshot(c.task_id)
    b, env = snapshot['binding'], snapshot['envelope']
    authorization = {'user_id': 'synthetic-owner', 'device_id': 'synthetic-local', 'action': 'query'}
    task = {'id': QUEUE_ID, 'agent_task_id': c.task_id, 'kind': 'query', 'state': 'needs_review',
        'reason_code': 'model_round_budget_exceeded', 'publication_token': None,
        'binding': b, 'envelope': env, 'authorization': authorization,
        'user_id': authorization['user_id'], 'device_id': authorization['device_id'],
        'knowledge_base_id': b['knowledge_base_id']}
    class Registry:
        authorized = True
        def authorize_snapshot(self, value, *, action):
            assert value == authorization and action == 'query'
            return Binding.from_dict(b) if self.authorized else None
        def get_user(self, user_id):
            return {'user_id': user_id, 'active': True, 'verification_status': 'verified',
                'sender_profile_url': b['sender_profile_url'], 'receiver_profile_url': b['receiver_profile_url']}
        def get_device(self, device_id):
            return {'device_id': device_id, 'user_id': authorization['user_id'], 'instance_id': b['instance_id'],
                'is_local': True, 'enabled': True, 'revoked': False, 'verification_status': 'verified'}
    state = tmp_path / 'state'
    state.mkdir()
    control = {'run_id': '2' * 32, 'pid': 12345, 'process_alive': True, 'state': 'paused', 'desired_state': 'paused'}
    docs = [{'platform_item_id': env['video_id'], 'chunk_id': 'synthetic:1', 'chunk_index': 1,
             'text': '合成原文讲解资料整理。', 'title': '合成资料'}]
    allowed = {env['video_id']}
    def loader(ids):
        assert ids == {env['video_id']}
        return deepcopy(docs)
    registry = Registry()
    queue = SimpleNamespace(get_task=lambda task_id: deepcopy(task) if task_id == QUEUE_ID else None)
    service = OperatorDraftService(queue, registry, tmp_path / 'unused-business.sqlite3',
        existing_agent_readonly(c.store.path), state, control=SimpleNamespace(snapshot=lambda: deepcopy(control)),
        receiver_provider=lambda: {'receiver_profile_url': b['receiver_profile_url']}, source_loader=loader,
        membership_reader=lambda binding: ({'name': '合成库', 'id': binding.knowledge_base_id}, set(allowed)))
    return SimpleNamespace(**vars(c), task=task, env=env, docs=docs, allowed=allowed, registry=registry,
        service=service, control=control, state=state)


def request(result, **changes):
    return {'text': '原文介绍资料整理的方法。', 'source_ids': ['资料1'],
        'expected_snapshot_digest': result['snapshot_digest'],
        'expected_evidence_digest': result['evidence_digest'], **changes}


def update_ref(c, *, text, chunk_id='synthetic:1', video=None):
    with c.store._tx() as db:
        row = db.execute("SELECT id,metadata FROM agent_steps WHERE task_id=? AND kind='tool_result' ORDER BY id LIMIT 1", (c.task_id,)).fetchone()
        value = json.loads(row['metadata'])
        ref = value['evidence_refs'][0]
        ref.update(text_sha256=hashlib.sha256(text.encode()).hexdigest(), chunk_id=chunk_id,
                   platform_item_id=video or c.env['video_id'])
        db.execute('UPDATE agent_steps SET metadata=? WHERE id=?', (json.dumps(value), row['id']))


def test_readonly_exact_evidence_and_immutable_first_candidate_do_not_change_ledgers(tmp_path):
    c = case(tmp_path)
    before, queue_before = c.store.path.read_bytes(), deepcopy(c.task)
    evidence = c.service.evidence(QUEUE_ID)
    assert not c.service.directory.exists()
    assert evidence['evidence'][0]['text'] == c.docs[0]['text']
    assert evidence['effective_max_chars'] == 317 and evidence['prefix'] == '少爷，'
    assert 'agent_snapshot' not in evidence and 'claim_token' not in json.dumps(evidence)
    snapshot = c.store.operator_query_draft_snapshot(c.task_id)
    assert evidence['snapshot_digest'] == operator_snapshot_digest(c.task, snapshot,
        c.task['authorization'], c.task['binding'], 'assistant:' + QUEUE_ID, '少爷，', '2' * 32,
        c.task['binding']['receiver_profile_url'])
    body = request(evidence)
    result = c.service.register(QUEUE_ID, body)
    assert result['proposal']['kind'] == 'answer' and result['proposal']['text'] == body['text']
    assert 'text' not in result['proposal']['sources'][0]
    path = c.service.directory / (QUEUE_ID + '.json')
    original, observed = path.read_bytes(), path.stat()
    assert c.service.register(QUEUE_ID, body) == result == c.service.bundle(QUEUE_ID)
    assert path.read_bytes() == original and path.stat().st_mtime_ns == observed.st_mtime_ns
    with pytest.raises(OperatorDraftError, match='candidate_already_registered'):
        c.service.register(QUEUE_ID, request(evidence, text='另一个有效但不同的合成回答。'))
    assert path.read_bytes() == original
    assert c.store.path.read_bytes() == before and c.task == queue_before
    assert c.store.get_reply(c.task_id) is None


@pytest.mark.parametrize('change', ['publication', 'queue_state', 'authorization', 'membership', 'receiver', 'owner', 'style', 'other_video'])
def test_rejects_changed_authority_scope_or_effects_before_original_read(tmp_path, change):
    c = case(tmp_path)
    if change == 'publication': c.task['publication_token'] = 'existing-barrier'
    elif change == 'queue_state': c.task['state'] = 'publishing'
    elif change == 'authorization': c.registry.authorized = False
    elif change == 'membership': c.allowed.clear()
    elif change == 'receiver': c.service.receiver_provider = lambda: {'receiver_profile_url': 'https://www.douyin.com/user/other-synthetic'}
    elif change == 'owner': c.control['process_alive'] = False
    elif change == 'style': c.service.prefix_reader = lambda key: None
    elif change == 'other_video': update_ref(c, text=c.docs[0]['text'], video='7999999000000000099')
    called = []
    c.service.source_loader = lambda ids: called.append(ids)
    with pytest.raises(OperatorDraftError): c.service.evidence(QUEUE_ID)
    assert called == [] and not c.service.directory.exists()


@pytest.mark.parametrize('change', ['hash', 'missing', 'duplicate', 'other_video', 'title_only', 'prefix_guess'])
def test_never_guesses_original_text_or_adopts_ambiguous_index_data(tmp_path, change):
    c = case(tmp_path)
    if change == 'hash': c.docs[0]['text'] += '正文发生变化。'
    elif change == 'missing': c.docs.clear()
    elif change == 'duplicate': c.docs.append(deepcopy(c.docs[0]))
    elif change == 'other_video': c.docs[0]['platform_item_id'] = '7999999000000000099'
    elif change == 'title_only':
        c.docs[0]['text'] = c.docs[0]['title']; update_ref(c, text=c.docs[0]['text'])
    elif change == 'prefix_guess': update_ref(c, text=c.docs[0]['text'][:5])
    with pytest.raises(OperatorDraftError): c.service.evidence(QUEUE_ID)
    assert not c.service.directory.exists()


def test_pure_existing_expansion_and_context_selection_are_allowed_only_with_exact_sha(tmp_path, monkeypatch):
    from app.core.config import settings
    from app.services.retrieval_context import expand_hit_context, select_context_hits
    from app.services.rag_service import RagService
    c = case(tmp_path)
    video = c.env['video_id']
    text = ''.join(f'第{i}句说明条件与结果，只有核对原文才知道这一项的具体含义。' for i in range(12))
    docs = [{'chunk_id': f'{video}:{i}', 'chunk_index': i, 'platform_item_id': video,
        'title': '合成视频', 'text': '语音文字稿（来自视频声音）：\n' + text[start:start + 120].strip()}
        for i, start in enumerate(range(0, len(text), 80))]
    c.docs[:] = docs
    expanded = expand_hit_context([docs[2]], docs, max_extra_chars=settings.retrieval_neighbor_chars)[0]
    assert expanded['text'] != docs[2]['text']
    update_ref(c, text=expanded['text'], chunk_id=docs[2]['chunk_id'])
    assert c.service.evidence(QUEUE_ID)['evidence'][0]['text'] == expanded['text']
    monkeypatch.setattr(settings, 'rag_prompt_max_context_chars', 90)
    selected = select_context_hits([expanded], max_count=8, max_chars=90,
        format_blocks=RagService._format_context_blocks, has_source_text=RagService._has_source_text, scope_ids={video})
    assert selected and selected[0]['text'] != expanded['text']
    update_ref(c, text=selected[0]['text'], chunk_id=docs[2]['chunk_id'])
    assert c.service.evidence(QUEUE_ID)['evidence'][0]['text'] == selected[0]['text']


@pytest.mark.parametrize('change', ['auth', 'member', 'agent', 'owner'])
def test_original_read_has_after_authority_snapshot_fence(tmp_path, change):
    c = case(tmp_path)
    def changed(ids):
        if change == 'auth': c.registry.authorized = False
        elif change == 'member': c.allowed.clear()
        elif change == 'agent': c.task['envelope']['comment_text'] += '已改动'
        elif change == 'owner': c.control['run_id'] = '3' * 32
        return deepcopy(c.docs)
    c.service.source_loader = changed
    with pytest.raises(OperatorDraftError): c.service.evidence(QUEUE_ID)
    assert not c.service.directory.exists()


def test_second_original_read_cannot_register_changed_source(tmp_path):
    c = case(tmp_path)
    evidence = c.service.evidence(QUEUE_ID)
    calls = []
    def loader(ids):
        calls.append(ids)
        docs = deepcopy(c.docs)
        if len(calls) > 1: docs[0]['text'] += '源已变动'
        return docs
    c.service.source_loader = loader
    with pytest.raises(OperatorDraftError): c.service.register(QUEUE_ID, request(evidence))
    assert not c.service.directory.exists() and c.store.get_reply(c.task_id) is None


@pytest.mark.parametrize('change', ['processing', 'removed_member', 'deleted_library'])
def test_default_sql_membership_requires_done_member_and_live_library(tmp_path, change):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session
    from app.db.base import Base
    from app.models.entities import KnowledgeBase, KnowledgeBaseVideo, VideoCache
    from datetime import datetime, timezone
    c = case(tmp_path)
    database = tmp_path / 'business.sqlite3'
    engine = create_engine('sqlite:///' + str(database))
    Base.metadata.create_all(engine)
    kb = c.task['knowledge_base_id']
    with Session(engine) as db:
        db.add(KnowledgeBase(id=kb, name='合成库', name_key='synthetic'))
        db.add(VideoCache(platform_item_id=c.env['video_id'], title='合成资料', status='done'))
        db.flush()
        db.add(KnowledgeBaseVideo(knowledge_base_id=kb, platform_item_id=c.env['video_id']))
        db.commit()
    c.service.database, c.service.membership_reader = database, c.service._membership
    assert c.service.evidence(QUEUE_ID)['evidence'][0]['text'] == c.docs[0]['text']
    with Session(engine) as db:
        if change == 'processing': db.query(VideoCache).filter_by(platform_item_id=c.env['video_id']).one().status = 'processing'
        elif change == 'removed_member':
            db.query(KnowledgeBaseVideo).filter_by(knowledge_base_id=kb).delete()
        else: db.get(KnowledgeBase, kb).deleted_at = datetime.now(timezone.utc)
        db.commit()
    with pytest.raises(OperatorDraftError): c.service.evidence(QUEUE_ID)
    engine.dispose()


@pytest.mark.parametrize('changes', [
    {'text': '字' * 318}, {'text': '原文\r介绍内容'}, {'text': '@某人 原文介绍内容'},
    {'text': 'https://synthetic.invalid/unsafe'}, {'text': '资料2介绍了内容。'},
    {'text': '这个视频已经保存成功。'}, {'text': '原文说：“没有提供过的完整原文句子。”'},
    {'source_ids': ['未返回资料']}, {'source_ids': ['资料1', '资料1']},
    {'expected_snapshot_digest': 'f' * 64}, {'expected_evidence_digest': 'f' * 64},
    {'send': True},
])
def test_registration_uses_normal_public_reply_and_strict_request_guards(tmp_path, changes):
    c = case(tmp_path)
    before = c.store.path.read_bytes()
    with pytest.raises(OperatorDraftError): c.service.register(QUEUE_ID, request(c.service.evidence(QUEUE_ID), **changes))
    assert not c.service.directory.exists() and c.store.path.read_bytes() == before


@pytest.mark.parametrize('change', ['body', 'scope', 'run', 'unknown_boolean', 'partial', 'hardlink'])
def test_bundle_revalidates_candidate_instead_of_trusting_file_flags(tmp_path, change):
    c = case(tmp_path)
    c.service.register(QUEUE_ID, request(c.service.evidence(QUEUE_ID)))
    path = c.service.directory / (QUEUE_ID + '.json')
    if change == 'run': c.control['run_id'] = '3' * 32
    elif change == 'scope': c.registry.authorized = False
    elif change == 'partial': path.write_text('{', encoding='utf-8')
    elif change == 'hardlink': os.link(path, c.state / 'candidate-copy.json')
    else:
        data = json.loads(path.read_text(encoding='utf-8'))
        if change == 'body': data['proposal']['text'] = '替换过的正文'
        else: data['validated'] = True
        path.write_text(json.dumps(data, ensure_ascii=False), encoding='utf-8')
    with pytest.raises(OperatorDraftError): c.service.bundle(QUEUE_ID)


def test_foreign_directory_and_missing_agent_are_not_created_or_adopted(tmp_path):
    c = case(tmp_path)
    c.service.directory.mkdir()
    with pytest.raises(OperatorDraftError): c.service.register(QUEUE_ID, request(c.service.evidence(QUEUE_ID)))
    assert list(c.service.directory.iterdir()) == []
    missing = tmp_path / 'missing-agent.sqlite3'
    with pytest.raises(Exception): existing_agent_readonly(missing)
    assert not missing.exists()


def test_transport_is_fixed_proxyless_bounded_no_redirect_and_one_attempt(monkeypatch):
    from app.assistant import operator_draft_service as module
    calls = []
    class Response:
        headers = {'Content-Type': 'application/json; charset=utf-8'}
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def getcode(self): return 200
        def geturl(self): return 'http://127.0.0.1:8000/api/assistant/operator-query-draft/' + QUEUE_ID + '/bundle'
        def read(self, limit):
            assert limit == module.MAX_BUNDLE_BYTES + 1
            return json.dumps({'task_id': QUEUE_ID, 'snapshot_digest': 'a' * 64,
                'evidence_digest': 'b' * 64, 'candidate_sha256': 'c' * 64}).encode()
    def build(proxy, redirect):
        assert proxy.proxies == {} and isinstance(redirect, module._NoRedirect)
        def open_(req, timeout):
            calls.append(req)
            assert req.get_method() == 'GET' and req.get_header('Origin') == 'http://127.0.0.1:8000'
            assert timeout == 30
            return Response()
        return SimpleNamespace(open=open_)
    monkeypatch.setattr(module.urllib.request, 'build_opener', build)
    assert fetch_operator_draft_bundle(QUEUE_ID)['task_id'] == QUEUE_ID and len(calls) == 1
    with pytest.raises(OperatorDraftError): fetch_operator_draft_bundle('../arbitrary')
    with pytest.raises(OperatorDraftError): module._NoRedirect().redirect_request(None, None, 302, '', {}, 'https://outside.invalid')


def test_owner_and_server_share_pure_quote_source_and_public_text_validation():
    text = '先检查土壤是否已经湿润。如果盆土已经干燥，再用清水缓慢浇灌。'
    item = {'source_id': '资料1', 'platform_item_id': '7999999000000000011', 'chunk_id': 'synthetic:1',
        'title': '合成资料', 'url': 'https://www.douyin.com/video/7999999000000000011',
        'text': text, 'text_sha256': hashlib.sha256(text.encode()).hexdigest()}
    source = {key: value for key, value in item.items() if key != 'text'}
    proposal = {'kind': 'answer', 'text': '1.“先检查土壤是否已经湿润。”；2.“如果盆土已经干燥，再用清水缓慢浇灌。”',
        'source_ids': ['资料1'], 'sources': [source]}
    options = {'comment_text': '给出两条原文依据', 'library_name': '合成库'}
    assert validate_operator_proposal(proposal, [item], '少爷，', 277, **options) == proposal
    with pytest.raises(OperatorDraftError):
        validate_operator_proposal({**proposal, 'text': '只有概括并没有原文摘录。'}, [item], '少爷，', 277, **options)
    with pytest.raises(OperatorDraftError):
        validate_operator_proposal(proposal, [item], '少爷，', 317, **options)
    with pytest.raises(OperatorDraftError):
        validate_operator_proposal(proposal, [{**item, 'text': text + '已变动'}], '少爷，', 277, **options)


def test_route_paths_local_json_and_duplicate_key_guards_without_event_loop(monkeypatch):
    from fastapi import HTTPException
    from starlette.requests import Request
    from app.assistant import evidence_routes as routes
    from app.assistant.routes import local_request
    paths = {route.path for route in routes.router.routes}
    assert '/assistant/evidence/query' in paths
    assert {'/assistant/operator-query-draft/{task_id}/evidence', '/assistant/operator-query-draft/{task_id}',
            '/assistant/operator-query-draft/{task_id}/bundle'} <= paths
    assert any(dependency.dependency is local_request for dependency in routes.router.dependencies)
    def request_(raw, *, host='127.0.0.1', origin='http://127.0.0.1:8000', content_type='application/json'):
        async def receive(): return {'type': 'http.request', 'body': raw, 'more_body': False}
        return Request({'type': 'http', 'method': 'POST', 'path': '/', 'scheme': 'http',
            'server': ('127.0.0.1', 8000), 'client': (host, 50000), 'query_string': b'',
            'headers': [(b'host', b'127.0.0.1:8000'), (b'origin', origin.encode()), (b'content-type', content_type.encode())]}, receive)
    local_request(request_(b'{}'))
    for changes in ({'host': '203.0.113.1'}, {'origin': 'https://outside.invalid'}, {'content_type': 'text/plain'}):
        with pytest.raises(HTTPException): local_request(request_(b'{}', **changes))
    calls = []
    async def direct(action, *args): return action(*args)
    monkeypatch.setattr(routes, 'run_in_threadpool', direct)
    service = SimpleNamespace(register=lambda *args: calls.append(args) or {'registered': True})
    def finish(coroutine):
        try: coroutine.send(None)
        except StopIteration as done: return done.value
        raise AssertionError('synthetic route must not start an event loop')
    assert finish(routes.operator_draft_register(QUEUE_ID, request_(b'{"text":"synthetic"}'), service)) == {'registered': True}
    assert len(calls) == 1
    for raw in (b'{"text":"a","text":"b"}', b'{"text":NaN}', b'\xff'):
        with pytest.raises(HTTPException) as error:
            finish(routes.operator_draft_register(QUEUE_ID, request_(raw), service))
        assert error.value.status_code == 422
    assert len(calls) == 1
