"""Getting a voice message again, later.

A message that arrived with gaps can be completed afterwards: replaying
it (Receive, three clicks) asks the sender for just the missing parts,
which it answers from the copy it keeps of everything it sent. A message
missed outright -- out of range when it went -- is listed in the
sender's pings, and asked for whole once the two are back in range.
"""

from __future__ import annotations

import dataclasses
import time
from types import SimpleNamespace

import pytest

from app.config.settings import Contact
from app.radio import link as link_module
from app.radio import protocol
from app.radio.framing import decode_frame
from app.radio.link import LoraLink
from app.radio.linkcheck import LinkMonitor
from app.radio.sx126x import SX126x
from app.store.inbox import Inbox
from app.store.keyring import Keyring
from app.store.roster import Roster
from tests.fakes import FakeModule, LossyModule
from tests.test_link_end_to_end import Collector
from tests.test_pairing import FakeLink
from tests.test_settings_flow import app  # noqa: F401

VOICE = bytes(range(200)) * 3          # 600 B: four fragments of 168
SIZE = protocol.VOICE_CHUNK


@pytest.fixture(autouse=True)
def quick(monkeypatch):
    monkeypatch.setattr(protocol, "QUIET_SECONDS", 0.3)
    monkeypatch.setattr(protocol, "REPAIR_WAIT", 0.6)
    monkeypatch.setattr(protocol, "REPAIR_WAIT_PER_FRAGMENT", 0.2)
    monkeypatch.setattr(link_module, "RETRIEVE_WAIT", 1.0)


# --- the request ---------------------------------------------------------------
def test_a_request_names_the_message_and_what_is_wanted():
    body = protocol.retrieve_body(17, 24, check_seq=0, check=0xBEEF, seqs=[3, 9])
    request = protocol.parse_retrieve(body)
    assert (request.msg_id, request.total, request.check_seq, request.check,
            request.seqs) == (17, 24, 0, 0xBEEF, [3, 9])
    assert protocol.parse_retrieve(b"\x01\x02") is None


def test_the_fingerprint_is_of_one_fragment():
    assert protocol.chunk_check(VOICE, 1, SIZE) == protocol.chunk_check(VOICE[SIZE:], 0, SIZE)
    assert protocol.chunk_check(VOICE, 1, SIZE) != protocol.chunk_check(VOICE, 2, SIZE)


# --- waiting for what was asked for ------------------------------------------
def packet(seq, total=4, body=None, msg_id=9, type_=protocol.RESENT, flags=0):
    chunk = protocol.chunk_of(VOICE, seq, SIZE) if body is None else body
    return protocol.Packet(type=type_, src=1, msg_id=msg_id, seq=seq, total=total,
                           flags=flags, body=chunk)


def test_an_expected_resend_is_done_when_the_wanted_fragments_are_in():
    reassembler = protocol.Reassembler()
    reassembler.expect((1, 9, protocol.RESENT), total=4, wanted=[1, 3],
                       fragment_size=SIZE, seconds=5.0)
    assert reassembler.push(packet(1)) is None
    message = reassembler.push(packet(3, flags=8))
    assert message is not None and message.type == protocol.RESENT
    assert sorted(set(range(4)) - set(message.missing)) == [1, 3]
    assert message.flags == 8                      # the sender's codec mode
    assert protocol.chunk_of(message.body, 3, SIZE) == protocol.chunk_of(VOICE, 3, SIZE)


def test_an_unanswered_request_still_ends():
    reassembler = protocol.Reassembler()
    reassembler.expect((1, 9, protocol.RESENT), total=4, wanted=[1], seconds=0.0)
    (key, _partial), = reassembler.due(time.monotonic() + 0.01)
    message = reassembler.finish(key)
    assert message.missing == [0, 1, 2, 3]


# --- over the air ---------------------------------------------------------------
def make_link(port, addr, monkeypatch, keyring):
    monkeypatch.setattr("serial.Serial", lambda *a, **k: port)
    radio = SX126x(port="fake", addr=addr, freq_mhz=868, mode_pins=None)
    return LoraLink(radio, duty_cycle_percent=100.0, keyring=keyring)


class LosesVoiceFragment(FakeModule):
    """Every transmission of one voice fragment is lost -- as sent, and as
    repaired while fresh -- the way a fade lasting a few seconds does."""

    def __init__(self, seq: int):
        super().__init__("fading", addr=1)
        self.seq = seq

    def _air(self, dst, payload):
        packet = protocol.decode(decode_frame(payload) or b"")
        if packet and packet.type == protocol.VOICE and packet.seq == self.seq:
            return
        super()._air(dst, payload)


def sealed_pair(monkeypatch, tmp_path, left=None):
    ours, theirs = Keyring(tmp_path / "a"), Keyring(tmp_path / "b")
    ours.add_peer(2, theirs.public, theirs.broadcast_key)
    theirs.add_peer(1, ours.public, ours.broadcast_key)
    _unused, right = FakeModule.pair()
    left = left or LossyModule("lossy", addr=1)
    left.peers, right.peers = [right], [left]
    return make_link(left, 1, monkeypatch, ours), make_link(right, 2, monkeypatch, theirs)


def test_a_gap_that_repair_could_not_fill_is_filled_later(monkeypatch, tmp_path):
    """Fragment 1 is lost however often it is repaired; asked for later, it comes."""
    alice, bob = sealed_pair(monkeypatch, tmp_path, LosesVoiceFragment(seq=1))
    kept = {}
    alice.on_retrieve(lambda src, request: (kept[request.msg_id], 8))
    inbox = Collector()
    bob.on_message(inbox)
    alice.start()
    bob.start()
    try:
        msg_id = alice.send_voice(2, VOICE, codec_mode=8)
        kept[msg_id] = VOICE
        first, _peer = inbox.wait(timeout=15.0)[0]
        assert first.missing == [1]

        check = protocol.chunk_check(first.body, 0, SIZE)
        bob.retrieve(1, msg_id, first.total, wanted=[1], check_seq=0, check=check,
                     flags=8)
        again, _peer = inbox.wait(count=2, timeout=10.0)[1]
        assert again.type == protocol.RESENT and 1 not in again.missing
        assert protocol.chunk_of(again.body, 1, SIZE) == protocol.chunk_of(VOICE, 1, SIZE)
        assert alice.stats.retrieves_answered == 1
    finally:
        alice.stop()
        bob.stop()


def test_a_request_the_sender_cannot_answer_ends_with_nothing(monkeypatch, tmp_path):
    alice, bob = sealed_pair(monkeypatch, tmp_path)
    alice.on_retrieve(lambda src, request: None)
    inbox = Collector()
    bob.on_message(inbox)
    alice.start()
    bob.start()
    try:
        bob.retrieve(1, 42, 4, wanted=[2])
        message, _peer = inbox.wait(timeout=10.0)[0]
        assert message.type == protocol.RESENT and message.missing == [0, 1, 2, 3]
        assert alice.stats.retrieves_refused == 1
        assert bob.stats.repairs_asked == 0      # what was asked for is not repaired
    finally:
        alice.stop()
        bob.stop()


def test_the_same_request_heard_twice_is_answered_once(monkeypatch, tmp_path):
    alice, bob = sealed_pair(monkeypatch, tmp_path)
    alice.on_retrieve(lambda src, request: (VOICE, 8))
    alice.start()
    bob.start()
    try:
        bob.retrieve(1, 42, 4, wanted=[2])
        bob.retrieve(1, 42, 4, wanted=[2])
        time.sleep(1.5)
        assert alice.stats.retrieves_answered == 1
    finally:
        alice.stop()
        bob.stop()


# --- keeping messages ------------------------------------------------------------
def voice_message(missing=(), msg_id=9, src=1, body=VOICE, total=4):
    held = bytearray(body)
    for seq in missing:
        held[seq * SIZE:(seq + 1) * SIZE] = bytes(len(held[seq * SIZE:(seq + 1) * SIZE]))
    if total - 1 in missing:
        del held[(total - 1) * SIZE:]            # a lost last fragment has no place
    return protocol.Message(type=protocol.VOICE, src=src, msg_id=msg_id, body=bytes(held),
                            flags=8, missing=list(missing), rssi_dbm=-90,
                            received_at=time.time(), fragment_size=SIZE, total=total)


def resent(present, msg_id=9, total=4):
    missing = [s for s in range(total) if s not in present]
    return dataclasses.replace(voice_message(missing, msg_id), type=protocol.RESENT)


def test_what_comes_again_goes_into_its_places(tmp_path):
    inbox = Inbox(tmp_path)
    item = inbox.add_voice(voice_message(missing=[1, 3]), "Base", 2.0)
    assert item.can_retrieve and item.missing == [1, 3]
    assert inbox.merge(item, resent([1, 3])) == [1, 3]
    assert inbox.voice_bytes(item) == VOICE
    assert not item.incomplete and not item.can_retrieve


def test_only_what_is_missing_is_taken(tmp_path):
    inbox = Inbox(tmp_path)
    item = inbox.add_voice(voice_message(missing=[2]), "Base", 2.0)
    assert inbox.merge(item, resent([0, 1])) == []
    assert item.missing == [2]


def test_sent_voice_is_kept_to_answer_from(tmp_path):
    inbox = Inbox(tmp_path)
    sent = dataclasses.replace(voice_message(), src=5, dst=1)
    item = inbox.add_voice(sent, "Base", 2.0, outgoing=True)
    assert inbox.voice_bytes(item) == VOICE
    assert inbox.find(None, 9, 4, outgoing=True) is item
    assert inbox.sent_since(time.time() - 60, 3) == [item]


def test_an_old_inbox_still_loads(tmp_path):
    (tmp_path / "inbox.json").write_text(
        '[{"id": "a", "kind": "voice", "src": 1, "peer_name": "Base", '
        '"received_at": 1.0, "retrieving": true, "from_the_future": 1}]')
    item, = Inbox(tmp_path).items
    assert item.msg_id == -1 and not item.can_retrieve and not item.retrieving


# --- in the app -------------------------------------------------------------------
class RetrieveLink(FakeLink):
    reassembling = 0

    def __init__(self):
        super().__init__()
        self.retrieves, self.pings = [], []
        self.unreadable = {}

    def retrieve(self, src, msg_id, total, wanted=(), check_seq=protocol.NO_CHECK,
                 check=0, flags=0, fragment_size=SIZE):
        self.retrieves.append((src, msg_id, total, list(wanted), check_seq, check))
        return 3.0

    def send_ping(self, dst, body):
        self.pings.append((dst, protocol.parse_ping(body)))

    def packet_seconds(self, *_a):
        return 0.16


@pytest.fixture
def radio(app, tmp_path):
    app.keyring = Keyring(tmp_path / "keys")
    base = Keyring(tmp_path / "base")
    app.keyring.add_peer(1, base.public, base.broadcast_key)
    app.settings.contacts = [Contact("Base", 1)]
    app.roster = Roster(app.settings.contacts, tmp_path)
    app.inbox = Inbox(tmp_path / "inbox")
    app.link = RetrieveLink()
    app.monitor = LinkMonitor(120)
    app.board = SimpleNamespace(foreground_ready=True)
    app._parents = {}
    app.played = []
    app._autoplay = lambda item: app.played.append(item.id)
    app._voice_duration = lambda message: 2.0
    app._silence_gaps = lambda message: message
    app._fetch_state()
    app._refresh_entries()
    return app


def test_replaying_a_message_with_gaps_asks_for_them(radio):
    item = radio.inbox.add_voice(voice_message(missing=[1, 3]), "Base", 2.0)
    assert radio._retrieve(item, play_when_done=True)
    (src, msg_id, total, wanted, check_seq, check), = radio.link.retrieves
    assert (src, msg_id, total, wanted, check_seq) == (1, 9, 4, [1, 3], 0)
    assert check == protocol.chunk_check(VOICE, 0, SIZE)
    assert item.retrieving and "asking Base" in radio.state.active_banner


def test_the_answer_completes_it_and_plays_it(radio):
    item = radio.inbox.add_voice(voice_message(missing=[1, 3]), "Base", 2.0)
    radio._retrieve(item, play_when_done=True)
    peer = SimpleNamespace(name="Base")
    radio._on_resent(resent([1, 3]), peer)
    assert radio.inbox.voice_bytes(item) == VOICE and not item.retrieving
    assert radio.played == [item.id]
    assert "complete now" in radio.state.active_banner


def test_no_answer_says_so(radio):
    item = radio.inbox.add_voice(voice_message(missing=[1]), "Base", 2.0)
    radio._retrieve(item, play_when_done=True)
    radio._on_resent(resent([]), SimpleNamespace(name="Base"))
    assert item.missing == [1] and not item.retrieving
    assert "no answer from Base" in radio.state.active_banner


def test_a_message_missed_while_away_is_asked_for_whole(radio):
    listing = [protocol.Recent(33, 4, protocol.chunk_check(VOICE, 0, SIZE),
                               radio.settings.radio.address)]
    radio._catch_up(1, listing)
    (src, msg_id, total, wanted, check_seq, _check), = radio.link.retrieves
    assert (src, msg_id, total, wanted, check_seq) == (1, 33, 4, [], 0)

    radio._on_resent(resent([0, 1, 2, 3], msg_id=33), SimpleNamespace(name="Base"))
    item = radio.inbox.find(1, 33, 4)
    assert item is not None and radio.inbox.voice_bytes(item) == VOICE
    assert "missed voice from Base" in radio.state.active_banner
    assert radio.played == [item.id]


def test_what_we_already_have_is_not_asked_for(radio):
    radio.inbox.add_voice(voice_message(msg_id=33), "Base", 2.0)
    radio._catch_up(1, [protocol.Recent(33, 4, 0, protocol.BROADCAST)])
    assert radio.link.retrieves == []


def test_a_missed_message_is_asked_for_twice_at_most(radio):
    listing = [protocol.Recent(33, 4, 0, protocol.BROADCAST)]
    for _ in range(4):
        radio._retrieving.clear()               # each request has timed out
        radio._catch_up(1, listing)
    assert len(radio.link.retrieves) == 2


def test_someone_elses_message_is_not_asked_for(radio):
    radio._catch_up(1, [protocol.Recent(33, 4, 0, 999)])
    assert radio.link.retrieves == []


def test_the_sender_answers_only_for_the_message_meant(radio):
    sent = dataclasses.replace(voice_message(msg_id=21), src=5, dst=1)
    radio.inbox.add_voice(sent, "Base", 2.0, outgoing=True)
    good = protocol.Retrieve(21, 4, 0, protocol.chunk_check(VOICE, 0, SIZE), [1])
    assert radio._on_retrieve(1, good) == (VOICE, 8)
    stale = dataclasses.replace(good, check=good.check ^ 1)     # an older msg 21
    assert radio._on_retrieve(1, stale) is None
    assert radio._on_retrieve(7, good) is None                   # not sent to 7
    assert radio._on_retrieve(1, dataclasses.replace(good, msg_id=22)) is None


def test_pings_list_what_was_sent_lately(radio):
    sent = dataclasses.replace(voice_message(msg_id=21), src=5, dst=protocol.BROADCAST)
    radio.inbox.add_voice(sent, "ALL", 2.0, outgoing=True)
    (entry,) = radio._recent_sent()
    assert (entry.msg_id, entry.total, entry.dst) == (21, 4, protocol.BROADCAST)
    assert entry.check == protocol.chunk_check(VOICE, 0, SIZE)
