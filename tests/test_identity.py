"""Who this radio is, on first start and every start after.

Every radio used to ship as address 5 called "whisplay", so the first two
set up side by side could not talk at all: each dropped the other's
packets as its own echo. A fresh install now picks its own ID and keeps
it, and names itself after the host.
"""

from __future__ import annotations

import pytest

from app.config import settings as settings_module
from app.store.overrides import Overrides


@pytest.fixture
def config(tmp_path, monkeypatch):
    """Write a config.yaml and load it against a private data dir."""
    data = tmp_path / "data"
    monkeypatch.setenv("WALKIE_DATA_DIR", str(data))
    monkeypatch.delenv("WALKIE_RADIO_ADDRESS", raising=False)
    monkeypatch.delenv("WALKIE_IDENTITY_CALLSIGN", raising=False)

    def load(text: str):
        path = tmp_path / "config.yaml"
        path.write_text(text)
        return settings_module.load(str(path)), Overrides(data)

    return load


def test_auto_picks_an_id_and_keeps_it(config):
    first, saved = config("radio:\n  address: auto\n")
    assert 1 <= first.radio.address <= 0xFFFE
    assert saved.get("radio", "address") == first.radio.address
    second, _ = config("radio:\n  address: auto\n")
    assert second.radio.address == first.radio.address


def test_no_address_at_all_means_auto(config):
    settings, saved = config("identity:\n  callsign: Rover\n")
    assert saved.get("radio", "address") == settings.radio.address


def test_an_explicit_address_is_kept_and_not_saved(config):
    settings, saved = config("radio:\n  address: 7\n")
    assert settings.radio.address == 7
    assert saved.get("radio", "address") is None


def test_an_impossible_address_falls_back_to_auto(config):
    settings, _ = config("radio:\n  address: 70000\n")
    assert settings.radio.address != 70000
    assert 1 <= settings.radio.address <= 0xFFFE


def test_the_environment_still_wins(config, monkeypatch):
    monkeypatch.setenv("WALKIE_RADIO_ADDRESS", "9")
    settings, _ = config("radio:\n  address: auto\n")
    assert settings.radio.address == 9


def test_auto_callsign_is_the_hostname(config, monkeypatch):
    monkeypatch.setattr(settings_module.socket, "gethostname",
                        lambda: "orangepizero2w.local")
    settings, _ = config("identity:\n  callsign: auto\n")
    assert settings.identity.callsign == "orangepizero2w"


def test_a_chosen_callsign_is_kept(config):
    settings, _ = config("identity:\n  callsign: Rover\n")
    assert settings.identity.callsign == "Rover"


def test_an_assigned_id_avoids_the_ones_given(tmp_path):
    free = 1234
    taken = set(range(1, 0xFFFF)) - {free}
    assert Overrides(tmp_path).assign_address(taken) == free


def test_the_token_is_made_once_and_kept(tmp_path):
    token = Overrides(tmp_path).node_token
    assert len(token) == 4
    assert Overrides(tmp_path).node_token == token
