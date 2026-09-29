"""What the operator sees of the link check, and when the checks go out.

The paired list and the Talk screen say whether each radio is in range,
with the signal both ways; a radio dropping out, or coming back, is said
out loud with a banner and a cue. The regular check is one broadcast
ping per interval, held back when the hour's airtime is running short.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.config.settings import Contact
from app.radio import protocol
from app.radio.linkcheck import DISCONNECTED, IN_RANGE, LinkMonitor
from app.store.inbox import Inbox
from app.store.keyring import Keyring
from app.store.roster import Entry, Roster
from app.ui import screens
from app.ui.screens import CONTACTS, RANGE, RECORDING, TALK
from tests.test_retrieve import RetrieveLink
from tests.test_screens import display, populated_state  # noqa: F401
from tests.test_settings_flow import app  # noqa: F401

RANGE_VIEW = {"name": "a radio with a very long name", "interval": 30, "air": "9.6k",
              "success": 0.7, "window": (7, 10), "down": -97, "up": -112,
              "sent": 124, "answered": 101, "marks": 12, "elapsed": 3725.0,
              "last_result": "answered in 312 ms",
              "log": "range-20260929-101500.csv"}


# --- the screens ----------------------------------------------------------------
@pytest.mark.parametrize("view,radio_state", [
    ({}, "idle"), (RANGE_VIEW, "idle"), (RANGE_VIEW, RECORDING), (RANGE_VIEW, "sending"),
    (dict(RANGE_VIEW, success=None, down=None, up=None, window=(0, 0),
          last_result="no answer"), "idle"),
])
def test_the_range_test_renders(display, view, radio_state):
    state = populated_state(screen=RANGE, range_view=view, radio_state=radio_state,
                            tx_sent=3, tx_total=12, record_seconds=4.2)
    assert screens.render(display, state) is True


def test_the_range_test_stays_above_the_footer(display):
    from tests.test_screens import _bottom_of_drawn_content

    state = populated_state(screen=RANGE, range_view=RANGE_VIEW)
    image, draw = display.new_canvas()
    screens.draw_range(draw, state)
    assert _bottom_of_drawn_content(image) < screens.CONTENT_BOTTOM + 4


def test_the_paired_list_shows_the_link_check(display):
    state = populated_state(screen=CONTACTS, link_status={
        1: (IN_RANGE, "in range · -72/-80 dBm"),
        2: (DISCONNECTED, "disconnected · 6m ago")})
    assert screens.render(display, state) is True


def test_talk_warns_before_talking_to_a_radio_out_of_range(display):
    state = populated_state(screen=TALK, target_address=1, target_linked=True,
                            link_status={1: (DISCONNECTED, "disconnected · 6m ago")})
    assert screens.render(display, state) is True
    state.link_status = {1: ("keys changed", "keys changed: pair again")}
    assert screens.render(display, state) is True


# --- the app ---------------------------------------------------------------------
class Cues:
    error, tx_done = "error", "done"


class Player:
    def __init__(self):
        self.cues = []

    def cue(self, sound):
        self.cues.append(sound)

    def stop(self):
        pass


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
    app.player = Player()
    app.cues = Cues()
    app.board = SimpleNamespace(foreground_ready=True)
    app._parents = {}
    app._fetch_state()
    app._refresh_entries()
    return app


def ping_from_base(radio, rssi=-80, reports=None):
    body = protocol.ping_body(1, 120, reports=reports or {radio.settings.radio.address: -85})
    radio._on_radio_message(
        protocol.Message(type=protocol.PING, src=1, msg_id=1, body=body, flags=0,
                         missing=[], rssi_dbm=rssi, received_at=0.0),
        SimpleNamespace(name="Base"))


def test_a_ping_puts_the_radio_in_range_with_both_signals(radio):
    ping_from_base(radio)
    radio._update_link_status()
    assert radio.state.link_status[1] == (IN_RANGE, "in range · -80/-85 dBm")
    radio._refresh_menus()
    assert "Base in range" in radio.state.home_items[2]["value"]


def test_dropping_out_and_coming_back_are_said_out_loud(radio):
    ping_from_base(radio)
    radio._update_link_status()
    radio.monitor.peers[1].last_heard -= 1000          # a quarter of an hour passes
    radio._update_link_status()
    assert radio.state.link_status[1][0] == DISCONNECTED
    assert "Base disconnected" in radio.state.active_banner
    assert radio.player.cues == ["error"]
    ping_from_base(radio)
    radio._update_link_status()
    assert "Base back in range" in radio.state.active_banner
    assert radio.player.cues == ["error", "done"]


def test_a_radio_whose_packets_cannot_be_opened_needs_pairing_again(radio):
    radio.link.unreadable[1] = 1.0
    radio._update_link_status()
    assert radio.state.link_status[1] == ("keys changed", "keys changed: pair again")


def test_the_regular_check_is_one_broadcast_ping(radio):
    radio._check_due = 0.0
    radio._link_check_tick()
    (dst, ping), = radio.link.pings
    assert dst == protocol.BROADCAST and not ping.reply
    assert ping.interval == radio.settings.radio.link_check_seconds
    radio._link_check_tick()                           # not due again yet
    assert len(radio.link.pings) == 1


def test_no_check_while_the_hour_is_nearly_spent(radio):
    budget = radio.link.budget
    while budget.remaining_seconds() > 0.2 * budget.limit_seconds:
        budget.record(200)
    radio._check_due = 0.0
    radio._link_check_tick()
    assert radio.link.pings == []


def test_no_check_with_nothing_paired(radio):
    radio.keyring.remove_peer(1)
    radio._check_due = 0.0
    radio._link_check_tick()
    assert radio.link.pings == []


def test_opening_talk_on_a_quiet_radio_probes_it(radio):
    radio._talk_to(1, "Base")
    assert radio.link.pings[-1][0] == 1 and radio.link.pings[-1][1].reply


def test_opening_talk_on_a_radio_just_heard_does_not(radio):
    ping_from_base(radio)
    radio._talk_to(1, "Base")
    assert radio.link.pings == []


def test_pings_do_not_light_the_screen(radio):
    pokes = []
    radio.display.poke = lambda: pokes.append(1)
    ping_from_base(radio)
    assert pokes == []
