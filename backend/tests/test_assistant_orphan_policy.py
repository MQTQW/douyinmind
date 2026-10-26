"""Only temporary metadata/files; no ingestion, provider or real browser."""
import pytest

from app.assistant.conversation import ConversationStore
from app.assistant.integration import registry_if_configured
from app.assistant.legacy_policy import legacy_testing_blocked
from app.mention_agent.store import AgentStore
from app.services.mention_ingestion_service import MentionIngestionService
from tests.test_assistant_registry_backend_fence import factory, denied


@pytest.mark.parametrize('suffix', ['-journal','-wal','-shm'])
@pytest.mark.parametrize('component', ['agent','conversation','receipt'])
def test_root_components_never_bootstrap_over_an_orphan(tmp_path, component, suffix):
    path = tmp_path/(component+'.sqlite3')
    orphan = tmp_path/(path.name+suffix)
    contents = b'orphan fixture must stay unchanged'
    orphan.write_bytes(contents)
    with pytest.raises(ValueError,match='orphan_sqlite_sidecar'):
        if component=='agent':
            AgentStore(path)
        elif component=='conversation':
            ConversationStore(path)
        else:
            service = MentionIngestionService(sessions=None,worker=None,pipeline=None,index_reader=None,
                receipt_path=path,binding_path=tmp_path/'unused-public-binding.json')
            with service._ledger(create=True):
                pytest.fail('orphan state must not reach schema creation')
    assert not path.exists()
    assert orphan.read_bytes()==contents


@pytest.mark.parametrize('suffix', ['-journal','-wal','-shm'])
def test_orphan_registry_is_unavailable_and_blocks_old_compatibility(tmp_path,suffix):
    orphan=tmp_path/('registry.sqlite3'+suffix)
    orphan.write_bytes(b'original state remains')
    assert legacy_testing_blocked(tmp_path)
    with pytest.raises(ValueError,match='registry_orphaned_state'):
        registry_if_configured(tmp_path,database=tmp_path/'unused-business.sqlite3')
    assert not (tmp_path/'registry.sqlite3').exists()
    assert not (tmp_path/'registry-required.json').exists()
    assert orphan.read_bytes()==b'original state remains'


def test_initial_unconfigured_read_creates_no_parent(tmp_path):
    state=tmp_path/'absent-assistant-state'
    assert registry_if_configured(state,database=tmp_path/'unused-business.sqlite3') is None
    assert not state.exists()
    assert not legacy_testing_blocked(state)


@pytest.mark.parametrize('suffix', ['-journal','-wal','-shm'])
def test_default_backend_cannot_revive_legacy_for_orphan_registry(factory,suffix):
    factory.registry_path.parent.mkdir(parents=True)
    orphan=factory.registry_path.with_name(factory.registry_path.name+suffix)
    orphan.write_bytes(b'durable origin unresolved')
    denied(factory.build(),factory.payload)
    assert not factory.registry_path.exists() and not factory.marker.exists()
    assert orphan.read_bytes()==b'durable origin unresolved'
