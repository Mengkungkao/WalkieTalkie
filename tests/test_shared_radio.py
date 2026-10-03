"""WalkieTalkie on the shared radio store: one Device ID, keys and contacts
per device, shared with the Messenger."""

import json
from types import SimpleNamespace

import pytest

from app.store import shared_radio
from app.store.overrides import Overrides

pytest.importorskip("mfruit_sdk.radio.keyring", reason="needs cryptography")
from mfruit_sdk.radio import settings as shared  # noqa: E402
from mfruit_sdk.radio.contacts import Contacts  # noqa: E402


def make_settings(address=None, callsign="Base"):
    return SimpleNamespace(radio=SimpleNamespace(address=address, frequency_mhz=868,
                                                 air_speed=9600, port="/dev/ttyS0"),
                           identity=SimpleNamespace(callsign=callsign))


@pytest.fixture
def walkie_dir(tmp_path, monkeypatch):
    path = tmp_path / "walkie"
    path.mkdir()
    monkeypatch.setenv("WALKIE_DATA_DIR", str(path))
    return path


def test_an_existing_walkietalkie_keeps_its_device_id_and_shares_it(walkie_dir):
    overrides = Overrides(walkie_dir)
    overrides.set("radio", "address", 4321)
    settings = make_settings(4321)
    assert shared_radio.sync_identity(settings, overrides)
    assert settings.radio.address == 4321
    assert shared.load_device().address == 4321


def test_a_shared_id_made_by_the_messenger_first_is_adopted(walkie_dir):
    shared.save_device(shared.Device(777, "Pi"))
    overrides = Overrides(walkie_dir)
    settings = make_settings(None)
    shared_radio.sync_identity(settings, overrides)
    assert settings.radio.address == 777
    assert Overrides(walkie_dir).get("radio", "address") == 777


def test_provisioned_radio_settings_are_used(walkie_dir):
    shared.save_radio(shared.RadioSettings(frequency_mhz=920, air_speed=2400, band="au915"))
    settings = make_settings(5)
    shared_radio.sync_identity(settings, Overrides(walkie_dir))
    assert (settings.radio.frequency_mhz, settings.radio.air_speed) == (920, 2400)


def test_own_keys_are_adopted_once_and_kept(walkie_dir):
    from app.store.keyring import Keyring as OwnKeyring
    own = OwnKeyring(walkie_dir)
    own.add_peer(9, bytes(32), bytes(32))
    ring = shared_radio.open_keyring(walkie_dir)
    assert ring.public == own.public and ring.paired == [9]
    assert (walkie_dir / "keys.json").is_file()
    assert json.loads((walkie_dir / "keys.json").read_text())["peers"]


def test_contacts_are_shared_both_ways(walkie_dir):
    shared_radio.remember_contact(12, "Hilltop")
    assert Contacts().all() == {12: "Hilltop"}
    Contacts().set(13, "Valley")
    assert shared_radio.shared_contacts() == {12: "Hilltop", 13: "Valley"}
    shared_radio.forget_contact(12)
    assert shared_radio.shared_contacts() == {13: "Valley"}


def test_a_device_id_or_name_changed_here_is_shared(walkie_dir):
    shared_radio.save_identity(4321, "Hilltop")
    assert shared.load_device() == shared.Device(4321, "Hilltop")
