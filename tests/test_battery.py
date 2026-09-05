"""Battery state, and the fact that a running server proves nothing.

pisugar-server answers even with no battery wired up, reporting
"I2C not connected" -- so the readings have to be checked rather than
merely fetched. That exact reply appeared on this hardware for hours
before the battery was fitted.
"""

from __future__ import annotations

import pytest

from app.utils import battery
from app.utils.battery import Battery, Monitor


def fake_server(monkeypatch, replies):
    monkeypatch.setattr(battery, "_ask", lambda cmd, timeout=2.0: replies.get(cmd))


HEALTHY = {
    "get battery": "93.9",
    "get battery_v": "4.05",
    "get battery_i": "-0.566",
    "get battery_charging": "false",
    "get battery_power_plugged": "false",
}


def test_a_fitted_battery_reads(monkeypatch):
    fake_server(monkeypatch, HEALTHY)
    state = battery.read()
    assert state.present
    assert state.percent == pytest.approx(93.9)
    assert state.milliamps == pytest.approx(-566.0)


def test_a_server_with_no_battery_is_not_a_battery(monkeypatch):
    """The regression: 'I2C not connected' must not read as 0%."""
    monkeypatch.setattr(battery, "_ask", lambda cmd, timeout=2.0: None)
    assert battery.read().present is False


def test_no_server_at_all_is_handled(monkeypatch):
    monkeypatch.setattr(battery, "_ask", lambda cmd, timeout=2.0: None)
    state = battery.read()
    assert not state.present and not state.low and not state.critical


@pytest.mark.parametrize("percent,low,critical", [
    (100.0, False, False), (50.0, False, False),
    (20.0, True, False), (12.0, True, False),
    (8.0, True, True), (2.0, True, True),
])
def test_low_and_critical_thresholds(percent, low, critical):
    state = Battery(present=True, percent=percent, amps=-0.5)
    assert state.low is low
    assert state.critical is critical


def test_charging_is_never_low(monkeypatch):
    """Plugged in and climbing is not a problem to warn about."""
    state = Battery(present=True, percent=5.0, amps=0.4, charging=True)
    assert not state.low and not state.critical


def test_runtime_estimate_uses_the_actual_draw():
    state = Battery(present=True, percent=100.0, amps=-0.6)
    assert state.hours_left(capacity_mah=1200) == pytest.approx(2.0, abs=0.05)


def test_no_runtime_estimate_while_charging():
    assert Battery(present=True, percent=50.0, amps=0.5).hours_left() is None


def test_summary_is_readable(monkeypatch):
    fake_server(monkeypatch, HEALTHY)
    text = battery.read().summary()
    assert "94%" in text            # 93.9 rounds for display
    assert "4.05V" in text and "mA" in text and "h left" in text


def test_summary_when_absent():
    assert Battery().summary() == "no battery"


def test_the_monitor_caches_between_polls(monkeypatch):
    calls = {"n": 0}

    def counting(_cmd, timeout=2.0):
        calls["n"] += 1
        return HEALTHY.get(_cmd)

    monkeypatch.setattr(battery, "_ask", counting)
    monitor = Monitor(poll_seconds=60)
    monitor.poll()
    first = calls["n"]
    for _ in range(5):
        monitor.poll()
    assert calls["n"] == first, "cached reads must not hit the socket"


def test_the_monitor_can_be_forced(monkeypatch):
    fake_server(monkeypatch, HEALTHY)
    monitor = Monitor(poll_seconds=600)
    monitor.poll()
    monitor.poll(force=True)
    assert monitor.state.present
