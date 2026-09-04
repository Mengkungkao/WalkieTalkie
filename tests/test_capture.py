"""Push-to-talk capture -- and the lost first word that prompted it.

Spawning `arecord` at the hold threshold lost the start of every
transmission: the WM8960 needs ~690 ms to power its ADC on a cold open,
on top of the 350 ms hold threshold. Presses shorter than that captured
nothing at all, which the logs recorded as `recorded 0.00s (0 B PCM)`.

These tests drive a fake `arecord` so the behaviour can be pinned
without a codec: that PTT keeps audio from before the press, that a very
short press still yields something once armed, and that arming and
disarming actually open and close the device.
"""

from __future__ import annotations

import struct
import subprocess
import time

import pytest

from app.audio import capture
from app.audio.capture import BYTES_PER_SECOND, Recorder


class FakeArecord:
    """Stands in for the arecord process, emitting a steady byte stream."""

    instances = []

    def __init__(self, *_args, **_kwargs):
        self.terminated = False
        self._closed = False
        FakeArecord.instances.append(self)
        self.stdout = self

    # -- the pipe --
    def read(self, size):
        if self._closed:
            return b""
        time.sleep(0.01)
        # A ramp, so tests can tell one chunk from another.
        return struct.pack("<h", 4000) * (size // 2)

    # -- the process --
    def terminate(self):
        self.terminated = True
        self._closed = True

    def kill(self):
        self.terminate()

    def wait(self, timeout=None):
        return 0


@pytest.fixture
def recorder(monkeypatch):
    FakeArecord.instances = []
    monkeypatch.setattr(subprocess, "Popen", FakeArecord)
    monkeypatch.setattr(capture.shutil, "which", lambda _name: "/usr/bin/arecord")
    rec = Recorder("plughw:1", max_seconds=5.0, preroll_seconds=0.5)
    yield rec
    rec.close()


def test_arming_opens_the_device_once(recorder):
    assert recorder.arm() is True
    assert recorder.armed
    assert len(FakeArecord.instances) == 1
    recorder.arm()  # idempotent
    assert len(FakeArecord.instances) == 1


def test_disarming_releases_the_device(recorder):
    recorder.arm()
    recorder.disarm()
    assert not recorder.armed
    assert FakeArecord.instances[0].terminated


def test_a_short_press_still_captures_audio_when_armed(recorder):
    """The regression: 'recorded 0.00s (0 B PCM)' on a real press."""
    recorder.arm()
    time.sleep(0.4)          # the codec has been warm for a while
    assert recorder.start() is True
    time.sleep(0.15)         # a brief press
    pcm = recorder.stop()
    assert len(pcm) > 0, "a short press must not come back empty"


def test_preroll_keeps_audio_from_before_the_press(recorder):
    """A user who talks as they press must keep their first syllable."""
    recorder.arm()
    time.sleep(0.6)          # fill the ring
    recorder.start()
    pcm = recorder.stop()
    # Everything captured here predates start(), so it can only be pre-roll.
    assert len(pcm) > 0
    assert len(pcm) <= recorder.preroll_bytes + BYTES_PER_SECOND


def test_the_ring_is_bounded_while_armed(recorder):
    """Idling armed for minutes must not grow memory without limit."""
    recorder.arm()
    time.sleep(1.2)          # well past the 0.5 s pre-roll
    assert recorder._ring_bytes <= recorder.preroll_bytes + capture.CHUNK


def test_recording_survives_being_started_cold(recorder):
    """Disarmed PTT must still work, just with the codec power-up cost."""
    assert not recorder.armed
    assert recorder.start() is True
    time.sleep(0.1)
    assert len(recorder.stop()) > 0


def test_staying_armed_across_a_transmission(recorder):
    """The device is not reopened between presses, so the second is instant."""
    recorder.arm()
    recorder.start(); time.sleep(0.05); recorder.stop()
    recorder.start(); time.sleep(0.05); recorder.stop()
    assert len(FakeArecord.instances) == 1
    assert recorder.armed


def test_a_cold_recording_closes_the_device_afterwards(recorder):
    """Unarmed capture must not leave the codec powered."""
    recorder.start(); time.sleep(0.05); recorder.stop()
    assert FakeArecord.instances[0].terminated
    assert not recorder.armed


def test_no_audio_device_means_no_crash():
    rec = Recorder(None)
    assert rec.available is False
    assert rec.arm() is False
    assert rec.start() is False
    assert rec.stop() == b""
