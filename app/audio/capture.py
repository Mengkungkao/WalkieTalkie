"""Push-to-talk recording.

`arecord` is spawned when the button goes down and killed when it comes
up. Spawning a process per transmission sounds wasteful, but the
alternative -- holding an ALSA capture stream open for the life of the
app -- keeps the codec powered and the DMA engine running around the
clock for a device that transmits a few seconds a minute. The process
costs about 20 ms at the start of a press; the always-open stream costs
milliwatts continuously. On a battery-powered HAT that trade is not
close.

Audio is read from the pipe in a reader thread that also tracks a
running RMS level, which the Talk screen draws as a VU meter -- the only
feedback the operator has that the microphone is actually live.
"""

from __future__ import annotations

import shutil
import subprocess
import threading
import time

from app.audio.codec2 import SAMPLE_RATE
from app.utils.logger import get_logger

log = get_logger("capture")

CHUNK = 1024  # bytes per pipe read: ~64 ms at 8 kHz mono 16-bit

try:
    import numpy as _np
except ImportError:  # pragma: no cover
    _np = None


def _rms(pcm: bytes) -> float:
    """Normalised 0..1 loudness of a PCM chunk."""
    if not pcm or len(pcm) < 2:
        return 0.0
    if _np is not None:
        samples = _np.frombuffer(pcm[: len(pcm) & ~1], dtype="<i2").astype("f4")
        if not samples.size:
            return 0.0
        return float(min(1.0, (_np.sqrt((samples ** 2).mean()) / 32768.0) * 4.0))
    total = 0
    count = len(pcm) // 2
    for i in range(0, count * 2, 2):
        value = int.from_bytes(pcm[i:i + 2], "little", signed=True)
        total += value * value
    return min(1.0, ((total / count) ** 0.5 / 32768.0) * 4.0)


class Recorder:
    """Records 8 kHz mono PCM for as long as `start()`..`stop()` spans."""

    def __init__(self, device: str | None, max_seconds: float = 30.0):
        self.device = device
        self.max_seconds = max_seconds
        self.level = 0.0
        self.started_at = 0.0
        self._process = None
        self._thread = None
        self._chunks = []
        self._lock = threading.Lock()
        self._stop = threading.Event()

    @property
    def available(self) -> bool:
        return bool(self.device) and shutil.which("arecord") is not None

    @property
    def recording(self) -> bool:
        return self._process is not None

    @property
    def elapsed(self) -> float:
        return time.monotonic() - self.started_at if self.started_at else 0.0

    def start(self) -> bool:
        if self.recording or not self.available:
            return False
        command = [
            "arecord", "-q", "-D", self.device, "-t", "raw",
            "-f", "S16_LE", "-r", str(SAMPLE_RATE), "-c", "1",
        ]
        try:
            self._process = subprocess.Popen(
                command, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                bufsize=0,
            )
        except OSError:
            log.exception("could not start arecord")
            self._process = None
            return False

        self._chunks = []
        self._stop.clear()
        self.level = 0.0
        self.started_at = time.monotonic()
        self._thread = threading.Thread(
            target=self._drain, name="capture", daemon=True
        )
        self._thread.start()
        return True

    def _drain(self):
        stream = self._process.stdout
        limit = int(self.max_seconds * SAMPLE_RATE * 2)
        size = 0
        while not self._stop.is_set():
            data = stream.read(CHUNK)
            if not data:
                break
            with self._lock:
                self._chunks.append(data)
                size += len(data)
            self.level = _rms(data)
            if size >= limit:
                log.info("recording hit the %.0fs cap", self.max_seconds)
                break
        self.level = 0.0

    def stop(self) -> bytes:
        """Stop and return everything captured as raw PCM."""
        if self._process is None:
            return b""
        self._stop.set()
        process = self._process
        self._process = None
        try:
            process.terminate()
            process.wait(timeout=1.0)
        except Exception:
            process.kill()
        if self._thread:
            self._thread.join(timeout=1.0)
        with self._lock:
            pcm = b"".join(self._chunks)
            self._chunks = []
        self.started_at = 0.0
        self.level = 0.0
        log.info("recorded %.2fs (%d B PCM)", len(pcm) / 2 / SAMPLE_RATE, len(pcm))
        return pcm

    def cancel(self):
        self.stop()
