"""Only temporary ledgers: no browser, provider, HTTP, audio or business DB."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import json
import os

import pytest

from app.assistant.conversation import ConversationStore
from app.mention_agent.store import AgentStore
from app.services.mention_ingestion_service import MentionIngestionService
from tests.test_mention_agent_import_end_to_end import scope


def test_agent_store_threaded_read_write_lifecycle(tmp_path):
    binding, original, envelope = scope(tmp_path)
    task = original.enqueue(binding.binding_id, envelope)
    claimed = original.claim(task['task_id'], lease_seconds=600)
    # Each actor has its own store instance, as child startup and model worker do.
    instances = [AgentStore(original.path) for _ in range(4)]
    def actor(index):
        store = instances[index]
        for number in range(160):
            if index % 2:
                store.record_step(task['task_id'], claimed['claim_token'], 'decision', {'actor': index, 'n': number})
            else:
                assert store.get_task(task['task_id'])['command_id'] == envelope['command_id']
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(actor, range(4)))
    assert len(original.list_steps(task['task_id'])) == 320


def test_receipt_ledger_threaded_guard_and_commit(tmp_path):
    path = tmp_path/'receipt.sqlite3'
    def service():
        return MentionIngestionService(sessions=None, worker=None, pipeline=None, index_reader=None,
            receipt_path=path, binding_path=tmp_path/'unused-binding.json')
    actors = [service() for _ in range(4)]
    with actors[0]._ledger(create=True):
        pass
    def actor(index):
        for number in range(100):
            with actors[index]._ledger() as db:
                if index % 2:
                    db.execute('INSERT INTO import_receipts VALUES(?,?,?,?,?)',
                        (f'{index}-{number}', str(70000+index*100+number), '{}', 'succeeded', 'fixture'))
                else:
                    assert db.execute('SELECT count(*) FROM import_receipts').fetchone()[0] >= 0
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(actor, range(4)))
    with actors[0]._ledger() as db:
        assert db.execute('SELECT count(*) FROM import_receipts').fetchone()[0] == 200


def test_conversation_concurrent_creation_and_scope_reads(tmp_path):
    binding, _, envelope = scope(tmp_path)
    path = tmp_path/'conversation.sqlite3'
    def actor(index):
        history = ConversationStore(path)
        for number in range(80):
            name = f'{index}-{number}'
            env = deepcopy(envelope)
            env.update(command_id=name, reference_id=name)
            history.bind_task(name, binding, env, name,
                {'quality':'current_comment', 'root_reference_id':name})
            assert history.task_scope(name)['library_id'] == binding.knowledge_base_id
            assert history.context_for(name, binding) is None
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(actor, range(4)))
    history = ConversationStore(path)
    with history._db() as db:
        assert db.execute('SELECT count(*) FROM comment_context_tasks').fetchone()[0] == 320


@pytest.mark.parametrize('suffix', ['', '-journal', '-wal', '-shm'])
def test_conversation_rejects_hardlinked_main_or_sidecar(tmp_path, suffix):
    path = tmp_path/'conversation.sqlite3'
    history = ConversationStore(path)
    if suffix:
        target = tmp_path/'other-data'
        target.write_bytes(b'preserve me')
        os.link(target, str(path)+suffix)
    else:
        target = tmp_path/'main-alias'
        os.link(path, target)
    before = target.read_bytes()
    with pytest.raises(ValueError):
        history.task_scope('anything')
    assert target.read_bytes() == before
