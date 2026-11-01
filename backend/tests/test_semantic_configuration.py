"""The paid meaning entrypoint needs an explicit owned local opt-in."""
import json
import pytest
from app.assistant.semantic_configuration import semantic_intent_enabled,build_semantic_interpreter
from app.assistant.native_configuration import NativeConfigurationError


def test_semantic_layer_is_default_on_for_a_fresh_install(tmp_path):
    assert semantic_intent_enabled(tmp_path) is True


def test_explicit_owned_disable_never_constructs_provider_or_store(tmp_path,monkeypatch):
    (tmp_path/'semantic-intent.json').write_text('{"version":1,"enabled":false}',encoding='utf-8')
    def forbidden(*a,**kw):pytest.fail('disabled feature must not construct provider')
    monkeypatch.setattr('app.assistant.semantic_intent_model.default_semantic_model',forbidden)
    assert build_semantic_interpreter(tmp_path,None,None,should_stop=lambda:False) is None
    assert sorted(p.name for p in tmp_path.iterdir())==['semantic-intent.json']


@pytest.mark.parametrize('value',[{'version':1,'enabled':'true'},{'version':True,'enabled':True},
    {'version':1,'enabled':True,'model':'chosen-by-request'},['enabled'],{}])
def test_no_implicit_fee_optin_or_external_model_override(tmp_path,value):
    (tmp_path/'semantic-intent.json').write_text(json.dumps(value),encoding='utf-8')
    with pytest.raises(NativeConfigurationError,match='semantic_configuration_invalid'):
        semantic_intent_enabled(tmp_path)


def test_duplicate_optin_keys_are_rejected(tmp_path):
    (tmp_path/'semantic-intent.json').write_text('{"version":1,"enabled":false,"enabled":true}')
    with pytest.raises(NativeConfigurationError,match='semantic_configuration_invalid'):
        semantic_intent_enabled(tmp_path)


def test_default_on_keeps_working_with_an_existing_ledger(tmp_path):
    (tmp_path/'semantic-plans.sqlite3').write_bytes(b'owned-ledger-presence-only')
    assert semantic_intent_enabled(tmp_path) is True
    (tmp_path/'semantic-intent.json').write_text('{"version":1,"enabled":false}')
    assert semantic_intent_enabled(tmp_path) is False
