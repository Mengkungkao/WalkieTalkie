"""Playing received voice, and the local confirmation beeps.

Like recording, `aplay` runs only for the duration of a clip. Playback
is serialised behind a lock: two messages arriving together should queue
rather than fight over the codec and come out as noise.
"""

from __future__ import annotations

import math
import shutil
import struct
import subprocess
import threading

from app.audio import dsp
from app.audio.codec2 import SAMPLE_RATE
from app.utils.logger import get_logger

log = get_logger("playback")


def tone(frequency: float, seconds: float, volume: float = 0.25) -> bytes:
    """A short sine burst with raised-cosine edges, so it does not click."""
    count = int(seconds * SAMPLE_RATE)
    edge = max(1, count // 20)
    out = bytearray()
    for index in range(count):
        envelope = min(1.0, index / edge, (count - index) / edge)
        sample = math.sin(2 * math.pi * frequency * index / SAMPLE_RATE)
        out += struct.pack("<h", int(sample * envelope * volume * 32767))
    return bytes(out)


# Each station gets its own pitch, so the beeps say *who* as well as
# *what*. With two identical Pis on a desk, "was that mine or theirs?" is
# a real question, and the answer should not require looking at a screen.
#
# The pitches are a pentatonic ladder: any two are clearly different by
# ear, and no pair beats against the other. Eight is plenty -- more would
# start to sound alike, which defeats the point.
VOICES = (523, 587, 659, 784, 880, 1047, 1175, 1319)


def voice_for(address: int) -> int:
    """The pitch that identifies this station."""
    return VOICES[int(address) % len(VOICES)]


class CueSet:
    """The four sounds one station makes, all built from its own pitch.

    Patterns carry the meaning and pitch carries the identity, so a
    listener learns "rising means I am transmitting" once and then hears
    which radio did it without relearning anything.
    """

    def __init__(self, address: int, name: str = ""):
        self.address = int(address)
        self.name = name
        self.pitch = voice_for(address)
        high = int(self.pitch * 1.5)      # a fifth above

        self.tx_start = tone(self.pitch, 0.07)
        self.tx_done = tone(self.pitch, 0.06) + bytes(320) + tone(high, 0.08)
        # Descending, so an incoming call never sounds like your own
        # transmission finishing.
        self.rx = tone(high, 0.07) + bytes(320) + tone(self.pitch, 0.09)
        # Deliberately not pitched: an error is an error whoever made it.
        self.error = tone(300, 0.18)

    def __repr__(self):
        return f"<CueSet {self.name or self.address} at {self.pitch} Hz>"


_cue_cache = {}


def cues_for(address: int, name: str = "") -> CueSet:
    """Cached, because building the tones costs a few milliseconds."""
    if address not in _cue_cache:
        _cue_cache[address] = CueSet(address, name)
    return _cue_cache[address]


# Kept for callers with no station in hand -- an error before the radio
# is even open, say.
CUE_ERROR = tone(300, 0.18)


class Player:
    """Serialised PCM playback through ALSA."""

    def __init__(self, device: str | None):
        self.device = device
        self._lock = threading.Lock()
        self._process = None

    @property
    def available(self) -> bool:
        return bool(self.device) and shutil.which("aplay") is not None

    def play(self, pcm: bytes, blocking: bool = True) -> bool:
        if not pcm or not self.available:
            return False
        if blocking:
            return self._play(pcm)
        threading.Thread(
            target=self._play, args=(pcm,), name="playback", daemon=True
        ).start()
        return True

    def _play(self, pcm: bytes) -> bool:
        # Everything here is 8 kHz -- decoded speech and the cues alike --
        # and goes to the card at its own rate, converted with a proper
        # filter rather than by ALSA's, which leaves a metallic edge.
        pcm = dsp.upsample(pcm)
        command = [
            "aplay", "-q", "-D", self.device, "-t", "raw",
            "-f", "S16_LE", "-r", str(dsp.HARDWARE_RATE), "-c", "1", "-",
        ]
        with self._lock:
            try:
                self._process = subprocess.Popen(
                    command, stdin=subprocess.PIPE, stderr=subprocess.DEVNULL
                )
                self._process.communicate(pcm, timeout=len(pcm) / 2 / dsp.HARDWARE_RATE + 10)
                return self._process.returncode == 0
            except Exception:
                log.warning("playback failed", exc_info=True)
                return False
            finally:
                self._process = None

    def cue(self, pcm: bytes):
        """Fire-and-forget UI feedback; never blocks the caller."""
        self.play(pcm, blocking=False)

    def stop(self):
        with self._lock:
            if self._process:
                try:
                    self._process.kill()
                except Exception:
                    pass
