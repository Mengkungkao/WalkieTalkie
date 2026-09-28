"""Choosing sound cards: HDMI is never a walkie-talkie's mic or speaker.

The listings are real `arecord -l` / `aplay -l` output. The Orange Pi's
HDMI card even lists itself as a capture device, and it comes before the
Whisplay codec, so a first-card fallback would record from it.
"""

from __future__ import annotations

import subprocess

import pytest

from app.audio import devices

ORANGE_PI_CAPTURE = """\
**** List of CAPTURE Hardware Devices ****
card 2: ahubhdmi [ahubhdmi], device 0: ahub_plat-i2s-hifi i2s-hifi-0 [ahub_plat-i2s-hifi i2s-hifi-0]
card 3: whisplaysound [Whisplay Sound], device 0: Whisplay HiFi wm8960-hifi-0 [Whisplay HiFi wm8960-hifi-0]
"""

PI_PLAYBACK = """\
**** List of PLAYBACK Hardware Devices ****
card 0: vc4hdmi [vc4-hdmi], device 0: MAI PCM i2s-hifi-0 [MAI PCM i2s-hifi-0]
"""


def fake_listing(monkeypatch, text):
    monkeypatch.setattr(
        devices.subprocess, "run",
        lambda args, **_: subprocess.CompletedProcess(args, 0, stdout=text),
    )


def test_orange_pi_hdmi_is_not_a_capture_card(monkeypatch):
    fake_listing(monkeypatch, ORANGE_PI_CAPTURE)
    assert devices.capture_cards() == [(3, "whisplaysound")]


def test_hdmi_alone_means_no_microphone(monkeypatch):
    fake_listing(monkeypatch, ORANGE_PI_CAPTURE.splitlines()[0] + "\n"
                 + ORANGE_PI_CAPTURE.splitlines()[1] + "\n")
    assert devices.resolve("auto", "capture") is None


def test_pi_hdmi_is_still_ignored(monkeypatch):
    fake_listing(monkeypatch, PI_PLAYBACK)
    assert devices.playback_cards() == []


# --- the microphone level ----------------------------------------------------
class Amixer:
    """Records amixer calls; `controls` is what `scontrols` lists."""

    def __init__(self, controls="Simple mixer control 'mic',0\nSimple mixer control 'speaker',0\n"):
        self.controls = controls
        self.calls = []

    def __call__(self, args, **_kwargs):
        self.calls.append(args)
        out = self.controls if "scontrols" in args else ""
        return subprocess.CompletedProcess(args, 0, stdout=out, stderr="")


def test_the_mic_level_is_set_on_the_capture_card(monkeypatch):
    amixer = Amixer()
    monkeypatch.setattr(devices.subprocess, "run", amixer)
    assert devices.set_mic_level("plughw:3", 80) is True
    assert amixer.calls[-1] == ["amixer", "-q", "-c", "3", "sset", "mic", "80%"]


@pytest.mark.parametrize("device,level", [("plughw:3", None), ("default", 80), (None, 80)])
def test_nothing_is_set_without_a_level_or_a_card(monkeypatch, device, level):
    amixer = Amixer()
    monkeypatch.setattr(devices.subprocess, "run", amixer)
    assert devices.set_mic_level(device, level) is False
    assert not any("sset" in call for call in amixer.calls)


def test_a_card_without_a_mic_control_is_left_alone(monkeypatch):
    """A USB headset, say: not the Whisplay driver's control to set."""
    amixer = Amixer(controls="Simple mixer control 'Capture',0\n")
    monkeypatch.setattr(devices.subprocess, "run", amixer)
    assert devices.set_mic_level("plughw:2", 80) is False
    assert not any("sset" in call for call in amixer.calls)
