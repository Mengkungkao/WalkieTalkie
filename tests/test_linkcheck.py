"""Is each paired radio in range? Pings, pongs and what they add up to.

Paired radios ping each other every couple of minutes; a ping that asks
gets a pong, carrying how strongly the ping arrived. From those each
radio knows, per paired radio: in range, weak, or disconnected -- and
the signal both ways, because a link that works one way only is the one
that fools you.
"""

from __future__ import annotations

import threading
import time

import pytest

from app.radio import linkcheck, protocol
from app.radio.link import LoraLink
from app.radio.linkcheck import (DISCONNECTED, IN_RANGE, UNKNOWN, WEAK,
                                 LinkMonitor)
from app.radio.sx126x import SX126x
from app.store.keyring import Keyring
from tests.fakes import FakeModule
from tests.test_link_end_to_end import Collector


# --- the packets --------------------------------------------------------------
def test_a_ping_carries_reports_and_recent_messages():
    recent = [protocol.Recent(17, 24, 0xBEEF, 51), protocol.Recent(18, 3, 1, 0xFFFF)]
    body = protocol.ping_body(9, interval=120, reply=True,
                              reports={51: -97, 6235: None}, recent=recent)
    ping = protocol.parse_ping(body)
    assert (ping.seq, ping.reply, ping.interval) == (9, True, 120)
    assert ping.reports == {51: -97, 6235: None}
    assert ping.recent == recent


def test_a_bare_ping_parses_and_a_truncated_one_keeps_what_it_has():
    assert protocol.parse_ping(protocol.ping_body(1)).reports == {}
    body = protocol.ping_body(1, reports={51: -80, 52: -90})
    assert protocol.parse_ping(body[:-2]).reports == {51: -80}
    assert protocol.parse_ping(b"\x01") is None


def test_signal_strength_fits_one_byte_both_ways():
    for rssi in (-1, -30, -91, -140, -255):
        assert protocol.rssi_from_byte(protocol.rssi_byte(rssi)) == rssi
    assert protocol.rssi_byte(None) == 0
    assert protocol.rssi_from_byte(0) is None


def test_a_pong_says_how_the_ping_arrived_and_how_many_did():
    assert protocol.parse_pong(protocol.pong_body(200, -104, 70000)) == (200, -104, 70000 % 65536)
    assert protocol.parse_pong(b"\x01") is None


# --- what it adds up to --------------------------------------------------------
@pytest.fixture
def monitor():
    watch = LinkMonitor(interval=120)
    watch.watch([51], now=0.0)
    return watch


def ping(reports=None, interval=120):
    return protocol.Ping(seq=1, reply=False, interval=interval,
                         reports=reports or {}, recent=[])


def test_a_radio_is_not_checked_until_heard(monitor):
    assert monitor.state_of(51, 10.0) == UNKNOWN


def test_heard_well_both_ways_is_in_range(monitor):
    monitor.ping(51, ping({6235: -88}), rssi=-80, me=6235, now=10.0)
    assert monitor.state_of(51, 11.0) == IN_RANGE
    assert monitor.summary(51, 11.0) == "in range · -80/-88 dBm"


def test_weak_either_way_is_weak(monitor):
    monitor.ping(51, ping({6235: -112}), rssi=-80, me=6235, now=10.0)
    assert monitor.state_of(51, 11.0) == WEAK


def test_two_missed_pings_is_disconnected(monitor):
    monitor.heard(51, -80, now=10.0)
    grace = 2 * 120 + linkcheck.MARGIN_SECONDS
    assert monitor.state_of(51, 10.0 + grace - 1) == IN_RANGE
    assert monitor.state_of(51, 10.0 + grace + 1) == DISCONNECTED
    assert monitor.summary(51, 10.0 + 400) == "disconnected · 6m ago"


def test_the_other_radios_interval_sets_the_grace(monitor):
    monitor.ping(51, ping(interval=30), rssi=-80, me=6235, now=0.0)
    assert monitor.state_of(51, 2 * 30 + 31 + 1) == DISCONNECTED


def test_a_radio_never_heard_is_disconnected_after_the_same_grace(monitor):
    assert monitor.state_of(51, 200.0) == UNKNOWN
    assert monitor.state_of(51, 300.0) == DISCONNECTED
    assert monitor.summary(51, 300.0) == "disconnected · not heard"


def test_an_unanswered_probe_settles_it_in_seconds(monitor):
    monitor.heard(51, -80, now=0.0)
    monitor.probe_sent(51, seq=4, now=100.0)
    assert monitor.state_of(51, 100.0 + linkcheck.PROBE_TIMEOUT - 1) == IN_RANGE
    assert monitor.state_of(51, 100.0 + linkcheck.PROBE_TIMEOUT + 1) == DISCONNECTED


def test_an_answer_gives_the_round_trip_and_how_we_are_heard(monitor):
    monitor.probe_sent(51, seq=4, now=100.0)
    assert monitor.pong(51, 4, rssi_at_them=-95, rssi=-90, now=100.4) == pytest.approx(0.4)
    link = monitor.peers[51]
    assert (link.down, link.up, link.answers) == (-90, -95, 1)
    assert monitor.state_of(51, 110.0) == IN_RANGE
    assert monitor.pong(51, 4, -95, -90, now=101.0) is None   # a duplicate


def test_update_reports_each_change_once(monitor):
    monitor.heard(51, -80, now=0.0)
    assert monitor.update(1.0) == [(51, UNKNOWN, IN_RANGE)]
    assert monitor.update(2.0) == []
    assert monitor.update(1000.0) == [(51, IN_RANGE, DISCONNECTED)]


def test_next_change_is_when_the_grace_runs_out(monitor):
    monitor.heard(51, -80, now=0.0)
    monitor.update(0.0)
    assert monitor.next_change(10.0) == pytest.approx(2 * 120 + 30 - 10)


def test_unpaired_radios_stop_being_watched(monitor):
    monitor.watch([], now=5.0)
    assert monitor.peers == {}


# --- over the air -------------------------------------------------------------
def paired_links(monkeypatch, tmp_path, left=None, right=None):
    left, right = (left, right) if left else FakeModule.pair()
    ours, theirs = Keyring(tmp_path / "a"), Keyring(tmp_path / "b")
    ours.add_peer(2, theirs.public, theirs.broadcast_key)
    theirs.add_peer(1, ours.public, ours.broadcast_key)
    links = []
    for port, addr, keys in ((left, 1, ours), (right, 2, theirs)):
        monkeypatch.setattr("serial.Serial", lambda *a, _p=port, **k: _p)
        radio = SX126x(port="fake", addr=addr, freq_mhz=868, mode_pins=None)
        links.append(LoraLink(radio, duty_cycle_percent=100.0, keyring=keys))
    return links


def test_a_ping_that_asks_is_answered_with_its_signal(monkeypatch, tmp_path):
    alice, bob = paired_links(monkeypatch, tmp_path)
    inbox, theirs = Collector(), Collector()
    alice.on_message(inbox)
    bob.on_message(theirs)
    alice.start()
    bob.start()
    try:
        alice.send_ping(2, protocol.ping_body(7, reply=True))
        pong = next(m for m, _p in inbox.wait(timeout=5.0) if m.type == protocol.PONG)
        seq, rssi_at_bob, heard = protocol.parse_pong(pong.body)
        assert (seq, rssi_at_bob, heard) == (7, -91, 1)      # the fake's -91 dBm
        assert pong.rssi_dbm == -91
        assert theirs.wait()[0][0].type == protocol.PING     # the app sees it too
        assert bob.stats.pongs_tx == 1
    finally:
        alice.stop()
        bob.stop()


def test_a_regular_ping_is_not_answered(monkeypatch, tmp_path):
    alice, bob = paired_links(monkeypatch, tmp_path)
    theirs = Collector()
    bob.on_message(theirs)
    alice.start()
    bob.start()
    try:
        alice.send_ping(protocol.BROADCAST, protocol.ping_body(1))
        assert theirs.wait()[0][0].type == protocol.PING
        time.sleep(0.3)
        assert bob.stats.pongs_tx == 0
    finally:
        alice.stop()
        bob.stop()


def test_no_answer_when_it_would_wait_for_the_duty_cycle(monkeypatch, tmp_path):
    alice, bob = paired_links(monkeypatch, tmp_path)
    bob.budget.duty_cycle = 0.01
    while bob.budget.remaining_seconds() > 0.01:
        bob.budget.record(200)
    theirs = Collector()
    bob.on_message(theirs)
    alice.start()
    bob.start()
    try:
        alice.send_ping(2, protocol.ping_body(1, reply=True))
        theirs.wait()
        time.sleep(0.2)
        assert bob.stats.pongs_skipped == 1 and bob.stats.pongs_tx == 0
    finally:
        alice.stop()
        bob.stop()


def test_a_packet_that_fails_to_open_marks_its_radio(monkeypatch, tmp_path):
    """A paired radio that was reset: its packets come, and cannot be read."""
    alice, bob = paired_links(monkeypatch, tmp_path)
    alice.keyring.reset()                       # new keys; Bob still has the old
    alice.keyring.add_peer(2, bob.keyring.public, bob.keyring.broadcast_key)
    bob.start()
    alice.start()
    try:
        alice.send_ping(2, protocol.ping_body(1))
        deadline = time.monotonic() + 3
        while 1 not in bob.unreadable and time.monotonic() < deadline:
            time.sleep(0.05)
        assert 1 in bob.unreadable
    finally:
        alice.stop()
        bob.stop()


class LateRssiModule(FakeModule):
    """The module's RSSI byte, a few milliseconds behind its packet."""

    def _receive(self, payload: bytes):
        with self._cond:
            self._buffer.extend(payload)
            self._cond.notify_all()
        threading.Timer(0.005, self._late).start()

    def _late(self):
        with self._cond:
            self._buffer.append(self.rssi_byte)
            self._cond.notify_all()


def test_the_rssi_byte_that_trails_its_packet_is_still_its_own(monkeypatch, tmp_path):
    """Without waiting for it, a lone packet was delivered with no signal
    reading at all, and the byte was put down to the next one."""
    left = FakeModule("left", addr=1)
    right = LateRssiModule("right", addr=2)
    left.peers, right.peers = [right], [left]
    alice, bob = paired_links(monkeypatch, tmp_path, left, right)
    theirs = Collector()
    bob.on_message(theirs)
    alice.start()
    bob.start()
    try:
        alice.send_ping(2, protocol.ping_body(1))
        message = theirs.wait()[0][0]
        assert message.rssi_dbm == -91
        assert bob.stats.late_rssi == 1
    finally:
        alice.stop()
        bob.stop()


def test_packet_seconds_is_the_airtime_of_one_small_message(monkeypatch, tmp_path):
    alice, _bob = paired_links(monkeypatch, tmp_path)
    seconds = alice.packet_seconds(protocol.PING, 2, 9)
    assert 0.12 < seconds < 0.2           # ~46 bytes at 9.6k, plus overhead
