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


# Distinct cues so the operator can work the radio without watching it.
CUE_TX_START = tone(880, 0.08)
CUE_TX_DONE = tone(1320, 0.10)
CUE_RX = tone(660, 0.07) + bytes(400) + tone(990, 0.09)
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
        command = [
            "aplay", "-q", "-D", self.device, "-t", "raw",
            "-f", "S16_LE", "-r", str(SAMPLE_RATE), "-c", "1", "-",
        ]
        with self._lock:
            try:
                self._process = subprocess.Popen(
                    command, stdin=subprocess.PIPE, stderr=subprocess.DEVNULL
                )
                self._process.communicate(pcm, timeout=len(pcm) / 2 / SAMPLE_RATE + 10)
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
