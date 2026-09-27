"""Choosing sound cards: HDMI is never a walkie-talkie's mic or speaker.

The listings are real `arecord -l` / `aplay -l` output. The Orange Pi's
HDMI card even lists itself as a capture device, and it comes before the
Whisplay codec, so a first-card fallback would record from it.
"""

from __future__ import annotations

import subprocess

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
