"""Codec2 speech codec, bound directly to libcodec2 via ctypes.

Codec2 is what makes voice over LoRa possible at all. Ordinary speech
codecs start around 6 kbps; Codec2's 700C mode encodes intelligible
speech at **700 bps** -- 87.5 bytes per second, or roughly 875 bytes for
a ten-second message. Against a 1% duty cycle that is about 1.3 seconds
of airtime, so the hour's legal budget holds a couple of dozen messages
instead of none. Opus, MP3 or raw PCM are all off by one to two orders
of magnitude here.

ctypes rather than the `pycodec2` wheel or the `c2enc` CLI: the wheel
needs a compiler and headers on the Pi, and spawning a process per clip
costs more wall time and power than the encode itself. `libcodec2.so.1.2`
already ships in Debian's `libcodec2-1.2`, which is installed, so this
binds the shared object as-is with no build step and no `-dev` package.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import threading

from app.utils.logger import get_logger

log = get_logger("codec2")

SAMPLE_RATE = 8000  # Codec2 is defined at 8 kHz mono, always.

# Mode id -> bitrate. The id is what codec2_create() takes and what we
# put in the packet's flags byte so the receiver decodes at the same rate.
MODES = {
    0: 3200, 1: 2400, 2: 1600, 3: 1400, 4: 1300, 5: 1200, 8: 700,
}
MODE_BY_NAME = {
    "3200": 0, "2400": 1, "1600": 2, "1400": 3, "1300": 4, "1200": 5, "700C": 8,
}
NAME_BY_MODE = {v: k for k, v in MODE_BY_NAME.items()}

DEFAULT_MODE = MODE_BY_NAME["700C"]

_SONAMES = ("libcodec2.so.1.2", "libcodec2.so.1.0", "libcodec2.so.1", "libcodec2.so")

_lib = None
_lib_lock = threading.Lock()


class Codec2Unavailable(RuntimeError):
    """libcodec2 is not installed, or is too old to bind."""


def _load():
    global _lib
    with _lib_lock:
        if _lib is not None:
            return _lib
        candidates = list(_SONAMES)
        found = ctypes.util.find_library("codec2")
        if found:
            candidates.insert(0, found)
        for name in candidates:
            try:
                lib = ctypes.CDLL(name)
            except OSError:
                continue
            lib.codec2_create.argtypes = [ctypes.c_int]
            lib.codec2_create.restype = ctypes.c_void_p
            lib.codec2_destroy.argtypes = [ctypes.c_void_p]
            lib.codec2_destroy.restype = None
            lib.codec2_encode.argtypes = [
                ctypes.c_void_p, ctypes.POINTER(ctypes.c_ubyte),
                ctypes.POINTER(ctypes.c_short),
            ]
            lib.codec2_encode.restype = None
            lib.codec2_decode.argtypes = [
                ctypes.c_void_p, ctypes.POINTER(ctypes.c_short),
                ctypes.POINTER(ctypes.c_ubyte),
            ]
            lib.codec2_decode.restype = None
            for fn in ("codec2_samples_per_frame", "codec2_bits_per_frame",
                       "codec2_bytes_per_frame"):
                getattr(lib, fn).argtypes = [ctypes.c_void_p]
                getattr(lib, fn).restype = ctypes.c_int
            _lib = lib
            log.info("libcodec2 loaded from %s", name)
            return _lib
        raise Codec2Unavailable(
            "libcodec2 not found -- install it with: sudo apt install libcodec2-1.2"
        )


def available() -> bool:
    try:
        _load()
        return True
    except Codec2Unavailable:
        return False


class Codec2:
    """One codec instance. Not thread-safe; encode and decode own theirs."""

    def __init__(self, mode: int = DEFAULT_MODE):
        if mode not in MODES:
            raise ValueError(f"unknown codec2 mode {mode}; have {sorted(MODES)}")
        self.lib = _load()
        self.mode = mode
        self.bitrate = MODES[mode]
        self._state = self.lib.codec2_create(mode)
        if not self._state:
            raise Codec2Unavailable(f"codec2_create({mode}) returned NULL")
        self.samples_per_frame = self.lib.codec2_samples_per_frame(self._state)
        self.bits_per_frame = self.lib.codec2_bits_per_frame(self._state)
        self.bytes_per_frame = self.lib.codec2_bytes_per_frame(self._state)
        self.frame_ms = self.samples_per_frame * 1000 // SAMPLE_RATE

    # --- lifecycle -----------------------------------------------------
    def close(self):
        if getattr(self, "_state", None):
            self.lib.codec2_destroy(self._state)
            self._state = None

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        self.close()

    def __del__(self):
        try:
            self.close()
        except Exception:
            pass

    # --- rate helpers --------------------------------------------------
    def bytes_for_seconds(self, seconds: float) -> int:
        frames = int(seconds * 1000 / self.frame_ms)
        return frames * self.bytes_per_frame

    def seconds_for_bytes(self, size: int) -> float:
        return (size // self.bytes_per_frame) * self.frame_ms / 1000.0

    # --- codec ---------------------------------------------------------
    def encode(self, pcm: bytes) -> bytes:
        """16-bit signed mono PCM at 8 kHz -> codec2 bitstream.

        A trailing partial frame is zero-padded rather than dropped, so a
        clip never loses its last syllable to frame alignment.
        """
        frame_bytes = self.samples_per_frame * 2
        if len(pcm) % frame_bytes:
            pcm = pcm + bytes(frame_bytes - (len(pcm) % frame_bytes))

        speech = (ctypes.c_short * self.samples_per_frame)()
        bits = (ctypes.c_ubyte * self.bytes_per_frame)()
        out = bytearray()
        for offset in range(0, len(pcm), frame_bytes):
            ctypes.memmove(speech, pcm[offset:offset + frame_bytes], frame_bytes)
            self.lib.codec2_encode(self._state, bits, speech)
            out.extend(bytes(bits))
        return bytes(out)

    def decode(self, data: bytes) -> bytes:
        """Codec2 bitstream -> 16-bit signed mono PCM at 8 kHz."""
        speech = (ctypes.c_short * self.samples_per_frame)()
        bits = (ctypes.c_ubyte * self.bytes_per_frame)()
        out = bytearray()
        usable = len(data) - (len(data) % self.bytes_per_frame)
        for offset in range(0, usable, self.bytes_per_frame):
            ctypes.memmove(bits, data[offset:offset + self.bytes_per_frame],
                           self.bytes_per_frame)
            self.lib.codec2_decode(self._state, speech, bits)
            out.extend(bytes(speech))
        return bytes(out)

    def silence(self, seconds: float) -> bytes:
        """PCM silence, for substituting fragments that never arrived."""
        return bytes(int(seconds * SAMPLE_RATE) * 2)
