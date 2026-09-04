"""Settings the operator changed on the device.

These are kept apart from config.yaml because rewriting that file would
destroy the comments that explain it, and because it is version
controlled and shared between nodes -- while the most important value
here, the device's address, must differ on every node.
"""

from __future__ import annotations

import json

import pytest

from app.config.settings import Contact, Settings
from app.store.overrides import Overrides, apply


@pytest.fixture
def overrides(tmp_path):
    return Overrides(tmp_path)


def test_starts_empty_and_persists_across_restarts(tmp_path, overrides):
    assert overrides.data == {}
    overrides.set("radio", "address", 7)
    assert Overrides(tmp_path).get("radio", "address") == 7


def test_only_whitelisted_keys_are_settable():
    """Air speed and duty cycle change on-air behaviour; they stay in the file."""
    store = Overrides.__new__(Overrides)
    store.data = {}
    store.save = lambda: None
    with pytest.raises(KeyError):
        store.set("radio", "air_speed", 62500)


def test_a_corrupt_file_is_ignored_rather_than_fatal(tmp_path):
    (tmp_path / "settings.json").write_text("{not json")
    assert Overrides(tmp_path).data == {}


def test_saving_is_atomic(tmp_path, overrides):
    """A truncated settings file would strand the radio on next boot."""
    overrides.set("radio", "address", 9)
    assert json.loads((tmp_path / "settings.json").read_text())["radio"]["address"] == 9
    assert not list(tmp_path.glob("*.tmp"))


def test_contacts_are_added_once(overrides):
    assert overrides.add_contact("Rover", 5) is True
    assert overrides.add_contact("Rover again", 5) is False
    assert len(overrides.contacts) == 1


def test_contacts_can_be_removed(overrides):
    overrides.add_contact("Rover", 5)
    assert overrides.remove_contact(5) is True
    assert overrides.remove_contact(5) is False


def test_base_station_round_trips_and_clears(overrides):
    overrides.set_base(5)
    assert overrides.base_address == 5
    overrides.set_base(None)
    assert overrides.base_address is None


def test_a_negligible_clock_offset_is_not_stored(overrides):
    overrides.set_clock_offset(0.2)
    assert overrides.clock_offset == 0.0
    overrides.set_clock_offset(-3600.0)
    assert overrides.clock_offset == -3600.0


def test_apply_folds_overrides_into_settings(overrides):
    settings = Settings()
    settings.contacts = [Contact("Base", 1)]
    overrides.set("radio", "address", 5)
    overrides.set("identity", "callsign", "Rover")
    overrides.add_contact("Hilltop", 9)

    apply(settings, overrides)
    assert settings.radio.address == 5
    assert settings.identity.callsign == "Rover"
    assert [c.address for c in settings.contacts] == [1, 9]


def test_apply_does_not_duplicate_a_contact_already_in_the_file(overrides):
    settings = Settings()
    settings.contacts = [Contact("Base", 1)]
    overrides.add_contact("Base copy", 1)
    apply(settings, overrides)
    assert len(settings.contacts) == 1


def test_reset_clears_the_file(tmp_path, overrides):
    overrides.set("radio", "address", 3)
    overrides.clear()
    assert overrides.data == {}
    assert not (tmp_path / "settings.json").exists()
    assert Overrides(tmp_path).data == {}
