"""Configuration only: temporary paths, no browser/session/transport creation."""
import json

import pytest

from app.assistant.native_configuration import load_native_configuration, NativeConfigurationError


B = 'https://www.douyin.com/user/synthetic-native-B'


def config(**changes):
    return {'version': 1, 'enabled': True, 'receiver_uid': '7999999000000011',
        'receiver_sec_uid': 'synthetic-native-B', 'receiver_profile_url': B,
        'session_source': 'owned_capsule', **changes}


def write(tmp_path, value):
    (tmp_path / 'native-receiver.json').write_text(json.dumps(value), encoding='utf-8')


def test_no_native_opt_in_keeps_legacy_compatibility_without_creating_anything(tmp_path):
    assert load_native_configuration(tmp_path, B) is None
    assert list(tmp_path.iterdir()) == []


def test_valid_configuration_contains_public_scope_only(tmp_path):
    value = config(); write(tmp_path, value)
    assert load_native_configuration(tmp_path, B) == value
    assert len(list(tmp_path.iterdir())) == 1


def test_disabled_is_explicit_and_still_validated(tmp_path):
    write(tmp_path, config(enabled=False))
    assert load_native_configuration(tmp_path, B) is None


@pytest.mark.parametrize('changes', [
    {'enabled': 1}, {'version': True}, {'version': 2},
    {'receiver_uid': 7999999000000011}, {'receiver_uid': '123\n'},
    {'receiver_sec_uid': 'self'}, {'receiver_profile_url': B + '?token=private'},
    {'receiver_profile_url': 'https://www.douyin.com/user/another-B'},
    {'session_source': 'arbitrary_path'}, {'session_source': {'path': 'private'}},
    {'cookie': 'synthetic-secret'}, {'path': 'C:/private'},
])
def test_invalid_or_secret_configuration_fails_closed_with_fixed_reason(tmp_path, changes):
    write(tmp_path, config(**changes))
    with pytest.raises(NativeConfigurationError) as error:
        load_native_configuration(tmp_path, B)
    assert error.value.code == 'native_configuration_invalid'
    assert str(error.value) == 'native_configuration_invalid'


def test_receiver_cannot_be_changed_by_a_separate_browser_config(tmp_path):
    write(tmp_path, config())
    with pytest.raises(NativeConfigurationError):
        load_native_configuration(tmp_path, 'https://www.douyin.com/user/another-B')


def test_duplicate_json_keys_are_not_last_writer_wins(tmp_path):
    raw = json.dumps(config()).replace('"enabled": true', '"enabled": false, "enabled": true')
    (tmp_path / 'native-receiver.json').write_text(raw, encoding='utf-8')
    with pytest.raises(NativeConfigurationError):
        load_native_configuration(tmp_path, B)


def test_missing_previously_required_configuration_never_falls_back_to_ui(tmp_path):
    (tmp_path / 'native-receiver-required.json').write_text('{}', encoding='utf-8')
    with pytest.raises(NativeConfigurationError) as error:
        load_native_configuration(tmp_path, B)
    assert error.value.code == 'native_configuration_required'


def test_existing_native_database_also_fences_missing_configuration(tmp_path):
    root = tmp_path / 'native-receiver'; root.mkdir()
    (root / 'sources.sqlite3').write_bytes(b'synthetic-marker-no-open')
    with pytest.raises(NativeConfigurationError):
        load_native_configuration(tmp_path, B)


def test_oversized_configuration_is_not_parsed(tmp_path):
    (tmp_path / 'native-receiver.json').write_text('x' * 8193, encoding='utf-8')
    with pytest.raises(NativeConfigurationError) as error:
        load_native_configuration(tmp_path, B)
    assert error.value.code == 'native_configuration_too_large'
