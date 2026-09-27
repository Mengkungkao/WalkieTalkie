"""Sealing: what keeps other radios on the frequency from listening in."""

from __future__ import annotations

import stat

import pytest

from app.radio import crypto, protocol
from app.store.keyring import Keyring

# Fixed keys, so the tests do not depend on luck (a random pair of codes
# matches one time in ten thousand).
ALICE = bytes(range(1, 33))
BOB = bytes(range(33, 65))
MALLORY = bytes(range(65, 97))


def public(private):
    return crypto.public_of(private)


def test_both_ends_derive_the_same_pairwise_key():
    assert crypto.pairwise_key(ALICE, public(BOB)) == crypto.pairwise_key(BOB, public(ALICE))


def test_another_pair_gets_another_key():
    assert crypto.pairwise_key(ALICE, public(BOB)) != crypto.pairwise_key(ALICE, public(MALLORY))


def test_both_screens_show_the_same_code():
    code = crypto.pairing_code(ALICE, public(BOB))
    assert code == crypto.pairing_code(BOB, public(ALICE))
    assert len(code) == 4 and code.isdigit()


def test_a_key_swapped_in_the_middle_shows_different_codes():
    """Mallory answers each side with her own key: the codes disagree."""
    alice_sees = crypto.pairing_code(ALICE, public(MALLORY))
    bob_sees = crypto.pairing_code(BOB, public(MALLORY))
    assert alice_sees != bob_sees


@pytest.fixture
def sealed():
    key = crypto.new_key()
    header = protocol.encode_header(protocol.TEXT, 1, 7, 0, 1, dst=2,
                                    sealing=protocol.PAIRWISE)
    return key, header, crypto.seal(key, header, b"meet at the gate")


def test_a_sealed_body_opens_with_its_key(sealed):
    key, header, body = sealed
    assert crypto.open_sealed(key, header, body) == b"meet at the gate"
    assert b"meet" not in body
    assert len(body) == len(b"meet at the gate") + crypto.OVERHEAD


def test_the_wrong_key_opens_nothing(sealed):
    _key, header, body = sealed
    assert crypto.open_sealed(crypto.new_key(), header, body) is None


def test_a_changed_body_opens_nothing(sealed):
    key, header, body = sealed
    tampered = bytearray(body)
    tampered[10] ^= 1
    assert crypto.open_sealed(key, header, bytes(tampered)) is None


def test_a_changed_header_opens_nothing(sealed):
    """Redirecting a packet to another radio breaks it."""
    key, _header, body = sealed
    redirected = protocol.encode_header(protocol.TEXT, 1, 7, 0, 1, dst=3,
                                        sealing=protocol.PAIRWISE)
    assert crypto.open_sealed(key, redirected, body) is None


def test_the_same_words_twice_look_different_on_air(sealed):
    key, header, body = sealed
    assert crypto.seal(key, header, b"meet at the gate") != body


def test_a_pairing_blob_is_bound_to_sender_and_receiver():
    key = crypto.new_key()
    blob = crypto.seal_blob(key, 1, 2, b"secret")
    assert crypto.open_blob(key, 1, 2, blob) == b"secret"
    assert crypto.open_blob(key, 1, 3, blob) is None


# --- keyring -----------------------------------------------------------------
def test_keys_are_made_once_and_kept(tmp_path):
    first = Keyring(tmp_path)
    second = Keyring(tmp_path)
    assert first.public == second.public
    assert first.broadcast_key == second.broadcast_key


def test_the_key_file_is_private(tmp_path):
    Keyring(tmp_path)
    mode = stat.S_IMODE((tmp_path / "keys.json").stat().st_mode)
    assert mode == 0o600


def test_peers_survive_a_restart(tmp_path):
    ours, theirs = Keyring(tmp_path / "a"), Keyring(tmp_path / "b")
    ours.add_peer(77, theirs.public, theirs.broadcast_key)
    again = Keyring(tmp_path / "a")
    assert again.is_paired(77)
    assert again.pairwise(77) == ours.pairwise(77)
    assert again.peer_broadcast(77) == theirs.broadcast_key


def test_two_keyrings_agree_on_their_pairwise_key(tmp_path):
    ours, theirs = Keyring(tmp_path / "a"), Keyring(tmp_path / "b")
    ours.add_peer(2, theirs.public, theirs.broadcast_key)
    theirs.add_peer(1, ours.public, ours.broadcast_key)
    assert ours.pairwise(2) == theirs.pairwise(1)
    assert ours.code_with(theirs.public) == theirs.code_with(ours.public)


def test_a_pairing_message_opens_only_for_its_addressee(tmp_path):
    ours, theirs, other = (Keyring(tmp_path / n) for n in "abc")
    body = ours.pair_body(theirs.public, 1, 2, "MengPi")
    assert theirs.open_pair_body(body, 1, 2) == (ours.public, ours.broadcast_key, "MengPi")
    assert other.open_pair_body(body, 1, 2) is None
    assert theirs.open_pair_body(body, 1, 3) is None
    assert ours.broadcast_key not in body


def test_reset_makes_new_keys_and_forgets_everyone(tmp_path):
    ours, theirs = Keyring(tmp_path / "a"), Keyring(tmp_path / "b")
    ours.add_peer(2, theirs.public, theirs.broadcast_key)
    old = ours.public
    ours.reset()
    assert ours.public != old
    assert not ours.is_paired(2)
    assert not Keyring(tmp_path / "a").is_paired(2)
