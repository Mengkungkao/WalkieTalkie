"""Home > Range test: probe the other radio, and keep a log of the walk.

The radio being carried away probes the other on a timer; every probe is
a CSV row (answered or not, round trip, signal both ways), a click marks
the spot, and tools/range_report.py turns the file into a summary.
"""

from __future__ import annotations

import csv
import io
from types import SimpleNamespace

import pytest

from app.config.settings import Contact
from app.radio import protocol
from app.radio.linkcheck import PROBE_TIMEOUT, LinkMonitor
from app.rangetest import COLUMNS, RangeTest
from app.store.inbox import Inbox
from app.store.keyring import Keyring
from app.store.roster import Roster
from app.ui import navigation
from app.ui.screens import HOME, RANGE, RECORDING
from tests.test_menu import go_to, press
from tests.test_retrieve import RetrieveLink
from tests.test_settings_flow import DOUBLE, SINGLE, TRIPLE, app  # noqa: F401
from tools import range_report


def rows(test: RangeTest) -> list:
    with open(test.path, newline="") as handle:
        return list(csv.DictReader(handle))


def walk(tmp_path) -> RangeTest:
    """Answered twice near by, a mark, then three lost and one faint answer."""
    test = RangeTest(51, "jarvis", tmp_path, interval=30, air_speed=9600, now=0.0)
    t = 0.0
    for seq, answer in enumerate([(-60, -62), (-70, -71), "mark", None, None, None,
                                  (-112, -115)], start=1):
        if answer == "mark":
            test.mark(now=t)
            continue
        test.probe_sent(seq, now=t)
        if answer is None:
            test.expire(now=t + PROBE_TIMEOUT + 1)
        else:
            test.answer(seq, answer[0], answer[1], heard=seq, now=t + 0.4)
        t += 30
    test.close(now=t)
    return test


def test_every_probe_is_a_row(tmp_path):
    test = walk(tmp_path)
    log = rows(test)
    assert list(log[0]) == list(COLUMNS)
    probes = [r for r in log if r["event"] == "probe"]
    assert [r["answered"] for r in probes] == ["1", "1", "0", "0", "0", "1"]
    assert probes[0]["rssi_down_dbm"] == "-60" and probes[0]["rssi_up_dbm"] == "-62"
    assert probes[0]["rtt_ms"] == "400"
    assert [r["event"] for r in log][0] == "start" and log[-1]["event"] == "stop"
    assert any(r["event"] == "mark" and r["mark"] == "1" for r in log)
    assert test.path.parent.name == "rangetest"


def test_the_success_rate_is_over_recent_probes(tmp_path):
    test = walk(tmp_path)
    assert test.success == pytest.approx(3 / 6)
    assert (test.sent, test.answered, test.marks) == (6, 3, 1)


def test_a_probe_the_duty_cycle_held_back_is_logged_as_skipped(tmp_path):
    test = RangeTest(51, "jarvis", tmp_path, interval=30, air_speed=9600, now=0.0)
    test.probe_skipped("duty cycle", now=0.0)
    assert test.next_due == 30.0 and test.last_result == "skipped: duty cycle"
    test.close(now=1.0)
    assert [r["event"] for r in rows(test)] == ["start", "skipped", "stop"]


def test_the_report_reads_the_walk_back(tmp_path):
    test = walk(tmp_path)
    out = io.StringIO()
    summary = range_report.report(test.path, out=out)
    assert (summary["sent"], summary["answered"], summary["longest_lost"]) == (6, 3, 3)
    assert summary["stretches"] == [("start", 2, 2), ("after mark 1", 1, 4)]
    text = out.getvalue()
    assert "link first gave out" in text and "after mark 1" in text


# --- in the app -------------------------------------------------------------------
@pytest.fixture
def radio(app, tmp_path):
    app.keyring = Keyring(tmp_path / "keys")
    other = Keyring(tmp_path / "jarvis")
    app.keyring.add_peer(51, other.public, other.broadcast_key)
    app.settings.contacts = [Contact("jarvis", 51)]
    app.roster = Roster(app.settings.contacts, tmp_path)
    app.inbox = Inbox(tmp_path / "inbox")
    app.link = RetrieveLink()
    app.monitor = LinkMonitor(120)
    app.board = SimpleNamespace(foreground_ready=True)
    app._parents = {}
    app._fetch_state()
    app._refresh_entries()
    app._refresh_menus()
    return app


def test_range_test_opens_from_home_on_the_paired_radio(radio):
    go_to(radio, "range")
    assert radio.state.screen == RANGE
    assert radio.range_test is not None and radio.range_test.peer == 51
    assert radio._target == (51, "jarvis")
    assert radio.state.range_view["name"] == "jarvis"


def test_it_probes_logs_the_answer_and_stops_on_back(radio):
    go_to(radio, "range")
    radio._range_tick()
    dst, ping = radio.link.pings[-1]
    assert dst == 51 and ping.reply
    radio._on_pong(SimpleNamespace(src=51, rssi_dbm=-88,
                                   body=protocol.pong_body(ping.seq, -93, 1)))
    assert radio.range_test.answered == 1
    assert radio.state.range_view["down"] == -88 and radio.state.range_view["up"] == -93

    press(radio, SINGLE)                       # mark the spot
    assert radio.range_test.marks == 1
    path = radio.range_test.path
    press(radio, DOUBLE)                       # back: the test ends, the log stays
    assert radio.state.screen == HOME and radio.range_test is None
    events = [r["event"] for r in csv.DictReader(open(path, newline=""))]
    assert events == ["start", "probe", "mark", "stop"]


def test_three_clicks_probes_now(radio):
    go_to(radio, "range")
    radio._range_tick()
    press(radio, TRIPLE)
    radio._range_tick()
    assert len(radio.link.pings) == 2


def test_no_probe_when_the_hour_is_spent(radio):
    go_to(radio, "range")
    budget = radio.link.budget
    while budget.remaining_seconds() > 0.1:
        budget.record(200)
    radio._range_tick()
    assert radio.link.pings == []
    assert radio.state.range_view["last_result"] == "skipped: duty cycle"


def test_with_nothing_paired_it_says_so(radio):
    radio.keyring.remove_peer(51)
    radio._refresh_entries()
    go_to(radio, "range")
    assert radio.state.screen == RANGE and radio.range_test is None
    assert radio.state.range_view == {}


class Recorder:
    available = True

    def __init__(self):
        self.started = 0

    def start(self):
        self.started += 1
        return True


def test_a_hold_talks_to_the_radio_under_test_and_stays_on_the_test(radio):
    go_to(radio, "range")
    radio.recorder, radio.codec = Recorder(), object()
    radio._on_talk_start()
    assert radio.recorder.started == 1
    assert radio.state.screen == RANGE and radio.state.radio_state == RECORDING
    assert navigation.can_talk(RANGE)
