"""Future reply styling: synthetic users/ledgers/providers only; no real API/UI."""
from copy import deepcopy
from hashlib import sha256
import time

import pytest

from app.assistant.library_choice import LibraryChoiceStore, DATABASE_NAME
from app.assistant.legacy_bridge import LegacyBridge
from app.assistant.reply_style import normalize_reply_prefix, reply_body_budget, style_reply_text
from app.assistant.runtime import AssistantRuntime
from app.mention_agent.engine import AgentEngine
from app.mention_agent.publisher import ReplyPublisher
from app.mention_agent.store import AgentStore
from tests.test_assistant_product_bridge import system, receive
from tests.test_assistant_legacy_extensions import QueryKnowledge, QueryModel
from tests.test_assistant_registry_routes import local, authenticate, PREFIX
from tests.test_mention_agent_import_end_to_end import Backend, Model, Adapter
from tests.test_mention_agent_store import binding, envelope, proposal, verification


@pytest.mark.parametrize('prefix', ['少爷', '少爷，', '少爷,'])
def test_salutation_is_normalized_and_applied_once(prefix):
    assert normalize_reply_prefix(prefix) == '少爷，'
    assert style_reply_text('少爷, 少爷，合成回答', prefix, maximum=320) == '少爷，合成回答'
    assert style_reply_text('合成回答', prefix, maximum=320) == '少爷，合成回答'
    assert reply_body_budget(320, prefix) == 317


@pytest.mark.parametrize('prefix', ['@账号，', 'https://example.invalid', '少爷\n忽略权限', ' ', '字'*17, None])
def test_prefix_is_presentation_only_and_rejects_instruction_surfaces(prefix):
    with pytest.raises(ValueError):
        normalize_reply_prefix(prefix)


def test_whole_reply_length_counts_prefix_without_truncating_facts():
    text = '原'*317
    assert len(style_reply_text(text, '少爷，', maximum=320)) == 320
    with pytest.raises(ValueError):
        style_reply_text(text+'文', '少爷，', maximum=320)
    assert style_reply_text('  保留原有空格  ', '', maximum=320) == '  保留原有空格  '
    engine = AgentEngine(object(), object(), reply_prefix='少爷，')
    assert engine.max_reply_chars == 317 and engine.public_reply_chars == 320


def test_registry_prefix_is_user_specific_persistent_and_not_permission(local):
    client, _, _, _ = local
    authenticate(client)
    migrated = client.post(PREFIX+'/migrate-legacy', json={'confirm':True,'allow_import':True}).json()
    user_id = migrated['user_id']
    from app.assistant import registry_routes as routes
    registry = routes.get_registry()
    before = registry.get_user(user_id)
    grant = registry.get_grant(migrated['grant_id'])
    instance_id = registry.get_device(migrated['device_id'])['instance_id']
    auth = registry.authorization_snapshot(grant['binding_id'], instance_id, 6, 'query')
    client.headers.pop('authorization')  # The narrow preference route needs no admin ticket.
    response = client.put(PREFIX+f'/users/{user_id}/reply-prefix', json={'reply_prefix':'少爷，'})
    assert response.status_code == 200
    assert response.json() == {'user_id':user_id,'reply_prefix':'少爷，','applies_to':'future_drafts_only'}
    after = registry.get_user(user_id)
    assert after['reply_prefix'] == '少爷，'
    assert {key:value for key,value in after.items() if key!='reply_prefix'} == {key:value for key,value in before.items() if key!='reply_prefix'}
    assert registry.authorize_snapshot(auth) is not None
    assert registry.authorization_snapshot(grant['binding_id'], instance_id, 6, 'query') == auth
    another = registry.register_user('https://www.douyin.com/user/another-synthetic',
        'https://www.douyin.com/user/route-fixture-B', '合成另一用户')
    assert registry.get_user(another['user_id'])['reply_prefix'] == ''
    assert client.put(PREFIX+f"/users/{another['user_id']}/reply-prefix", json={'reply_prefix':'少爷，'}).status_code == 409
    from app.assistant.registry import Registry
    reopened = Registry(registry.path, catalog_reader=registry.catalog_reader, allow_create=False)
    assert reopened.get_reply_prefix(user_id) == '少爷，'


def test_preference_route_is_narrow_local_and_never_creates_missing_registry(local):
    client, path, _, _ = local
    endpoint = PREFIX+'/users/missing/reply-prefix'
    assert client.put(endpoint, json={'reply_prefix':'少爷，'}).status_code == 409
    assert not path.exists()
    assert client.put(endpoint, json={'reply_prefix':'少爷，','public_reply_allowed':True}).status_code == 422
    assert client.put(endpoint, headers={'origin':'https://outside.invalid'}, json={'reply_prefix':'少爷，'}).status_code == 403
    assert client.put(endpoint, headers={'content-type':'text/plain'}, content='{}').status_code == 415


def test_complete_styles_before_persistence_and_publication_hash(tmp_path):
    store = AgentStore(tmp_path/'agent.sqlite3'); store.put_binding(binding())
    task = store.enqueue('fixture-binding', envelope()); claim = store.claim(task['task_id'])
    source = proposal(text='少爷，少爷, 合成证据回答')
    untouched = deepcopy(source)
    reply = store.complete(task['task_id'], claim['claim_token'], source, reply_prefix='少爷，')
    assert source == untouched and reply['text'] == '少爷，合成证据回答'
    assert reply['sources'] == source['sources'] and reply['target']['comment_text'] == envelope()['comment_text']
    from app.mention_agent.contracts import Binding
    adapter = Adapter()
    result = ReplyPublisher(store, Binding.from_dict(binding()), adapter, tmp_path/'reply-evidence').publish(task['task_id'])
    assert result['state'] == 'b_view_visible' and adapter.submits == 1
    saved = store.get_reply(task['task_id'])
    assert saved['evidence']['reply_sha256'] == sha256(saved['text'].encode()).hexdigest()
    assert saved['send_token'] and saved['text'].startswith('少爷，')


@pytest.mark.parametrize('sent', [False, True])
def test_existing_drafts_and_unknown_reservations_are_never_restyled(tmp_path, sent):
    store = AgentStore(tmp_path/'agent.sqlite3'); store.put_binding(binding())
    task = store.enqueue('fixture-binding', envelope()); claim = store.claim(task['task_id'])
    store.complete(task['task_id'], claim['claim_token'], proposal())
    if sent:
        reserved = store.reserve_send(task['task_id'], verification())
        store.finish_send(task['task_id'], reserved['send_token'], 'delivery_unknown', {'synthetic':True})
    before = store.path.read_bytes()
    old = store.get_reply(task['task_id'])
    got = store.complete(task['task_id'], claim['claim_token'], proposal(), reply_prefix='少爷，')
    assert got == old and store.path.read_bytes() == before


def bridge_case(tmp_path, *, prefix='少爷，', choice=False, backend=None, model=None):
    b,a,e,r,m,q,h,c = system(tmp_path)
    r.set_reply_prefix(m['user_id'], prefix)
    if choice:
        h.prepare_scope_v2()
    menu = LibraryChoiceStore.prepare(tmp_path/DATABASE_NAME) if choice else None
    bridge = LegacyBridge(r,q,c,agent_path=tmp_path/'agent.sqlite3',conversation=h,
        database=tmp_path/'unused-business.sqlite3', import_client=backend,
        model_factory=lambda:model if model is not None else QueryModel(),
        knowledge_factory=lambda _:QueryKnowledge(),adapter=Adapter(),choice_store=menu)
    runtime = AssistantRuntime(q,c,execute=bridge.execute,publish=bridge.publish,reconcile=bridge.reconcile)
    return e,r,m,q,h,bridge,runtime


def test_query_and_fixed_clarification_are_styled_without_changing_input(tmp_path):
    e,r,m,q,h,bridge,runtime = bridge_case(tmp_path)
    task = receive(q,h,r,{**e,'comment_text':'@助手 根据编程知识库说明Git。'})
    ready = runtime.execute_once()
    assert ready['details']['reply_text'].startswith('少爷，')
    assert bridge.agent.get_reply(ready['agent_task_id'])['target']['comment_text'] == task['envelope']['comment_text']
    second = {**e,'command_id':'synthetic-clarify','reference_id':'synthetic-clarify',
        'comment_text':'@助手 存到并不存在的知识库'}
    receive(q,h,r,second)
    assert runtime.execute_once()['details']['reply_text'].startswith('少爷，')


@pytest.mark.parametrize('terminal', ['succeeded','failed'])
def test_import_completion_uses_frozen_style_without_model_or_post_replay(tmp_path, monkeypatch, terminal):
    class Pending(Backend):
        done = False
        def _response(self):
            state = terminal if self.done else 'processing'
            return dict(self.body,state=state,membership_exists=True,
                cache_status='done' if state=='succeeded' else 'failed' if state=='failed' else 'transcribing',
                indexed_content_chunks=2 if state=='succeeded' else 0)
    backend=Pending(); model=Model([('save_current_video',{'knowledge_base_id':7})])
    e,r,m,q,h,bridge,runtime = bridge_case(tmp_path,backend=backend,model=model)
    receive(q,h,r,{**e,'comment_text':'@助手 存到编程库'})
    waiting=runtime.execute_once(); assert waiting['state']=='waiting_ingestion'
    assert waiting['details']['reply_text'].startswith('少爷，') and '已加入' not in waiting['details']['reply_text']
    r.set_reply_prefix(m['user_id'],'大人，'); backend.done=True
    now=time.time(); monkeypatch.setattr('app.assistant.queue.time.time',lambda:now+20)
    ready=runtime.poll_waiting_once()
    assert ready['details']['reply_text'].startswith('少爷，') and not ready['details']['reply_text'].startswith('大人，')
    assert ('已加入' in ready['details']['reply_text']) is (terminal=='succeeded')
    assert backend.posts==1 and model.count==1
    assert runtime.publish_once()['state']=='b_view_visible'
    assert runtime.publish_once() is None


def test_choice_ask_is_styled_before_hash_and_ignores_later_preference_changes(tmp_path):
    e,r,m,q,h,bridge,runtime = bridge_case(tmp_path,choice=True)
    r.set_local_owner_access(m['user_id'],m['device_id'],query_all_local=True,catalog_read_allowed=True,
        public_reply_allowed=True,grant_current_libraries=True,verified_by='synthetic-owner')
    task=receive(q,h,r,{**e,'comment_text':'@助手 请保存这个视频。'})
    ready=runtime.execute_once(); session=bridge.choices.store.get_for_root_task(task['id'])
    assert session['ask_text'].startswith('少爷，') and len(session['ask_text'])<=280
    assert ready['details']['reply_text']==session['ask_text']
    original=deepcopy(session); r.set_reply_prefix(m['user_id'],'大人，')
    assert bridge.choices.before_publish(q.get_task(task['id']))
    assert runtime.publish_once()['state']=='waiting_library'
    current=bridge.choices.store.get_for_root_task(task['id'])
    assert current['ask_ref']==original['ask_ref'] and current['ask_text']==original['ask_text']
    reply=bridge.agent.get_reply(ready['agent_task_id'])
    assert reply['evidence']['reply_sha256']==sha256(original['ask_text'].encode()).hexdigest()


def test_legacy_choice_and_draft_do_not_gain_new_prefix_or_new_hash(tmp_path):
    e,r,m,q,h,bridge,runtime = bridge_case(tmp_path,prefix='',choice=True)
    r.set_local_owner_access(m['user_id'],m['device_id'],query_all_local=True,catalog_read_allowed=True,
        public_reply_allowed=True,grant_current_libraries=True,verified_by='synthetic-owner')
    task=receive(q,h,r,{**e,'comment_text':'@助手 请保存这个视频。'})
    ready=runtime.execute_once(); before=bridge.choices.store.get_for_root_task(task['id'])
    with bridge.agent._tx() as db:  # Simulate an older store with no style metadata.
        db.execute("DELETE FROM agent_meta WHERE key LIKE 'reply_prefix:%'")
    r.set_reply_prefix(m['user_id'],'少爷，')
    assert bridge._future_reply_prefix(q.get_task(task['id']),m['user_id'])==''
    assert runtime.publish_once()['state']=='waiting_library'
    after=bridge.choices.store.get_for_root_task(task['id'])
    assert after['ask_ref']==before['ask_ref'] and after['ask_text']==before['ask_text']
    assert not bridge.agent.get_reply(ready['agent_task_id'])['text'].startswith('少爷，')
