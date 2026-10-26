"""Independent opt-in recovery counterexample; temporary paths only."""

import pytest

from app.assistant.native_configuration import NativeConfigurationError, load_native_configuration


B = "https://www.douyin.com/user/synthetic-config-review-B"


def test_orphan_native_owner_marker_cannot_silently_reenable_legacy_intake(tmp_path):
    # The explicit source-store creation protocol writes this owner marker
    # before creating the main SQLite file. A crash can leave only the marker.
    native = tmp_path / "native-receiver"
    native.mkdir()
    marker = native / "sources.sqlite3.owner.json"
    marker.write_text('{"synthetic":"orphan-ownership-witness"}', encoding="utf-8")
    before = marker.read_bytes()

    with pytest.raises(NativeConfigurationError) as caught:
        load_native_configuration(tmp_path, B)

    assert caught.value.code == "native_configuration_required"
    assert marker.read_bytes() == before
    assert not (native / "sources.sqlite3").exists()
    assert not (tmp_path / "native-receiver.json").exists()


def test_never_configured_empty_directory_remains_read_only_legacy_compatibility(tmp_path):
    assert load_native_configuration(tmp_path, B) is None
    assert list(tmp_path.iterdir()) == []
