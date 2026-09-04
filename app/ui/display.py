"""Canvas in, framebuffer out -- plus the backlight power policy.

Two things here are deliberately about power rather than pixels.

**Frames are only pushed when something changed.** The daemon samples
the shared framebuffer continuously; converting and writing an identical
frame costs a full 240x280 RGB565 conversion for no visible effect. The
app renders to a PIL image, hashes it, and skips the write when the hash
matches the last one. An idle walkie-talkie therefore does no work at
all between events.

**The backlight steps down on its own.** It is the largest single
consumer on the HAT -- far more than the CPU this app uses -- so it dims
after `idle_dim_seconds` and switches off after `idle_off_seconds`, and
any button press or received packet brings it straight back. `poke()` is
what the rest of the app calls to mean "the operator is here".
"""

from __future__ import annotations

import threading
import time

from PIL import Image, ImageDraw

from app.ui import theme
from app.utils.logger import get_logger

log = get_logger("display")

try:
    import numpy as _np
except ImportError:  # pragma: no cover - optional accelerator
    _np = None


def image_to_rgb565(image: Image.Image) -> bytes:
    """RGB image -> big-endian RGB565, the daemon's framebuffer format."""
    if _np is not None:
        arr = _np.asarray(image.convert("RGB"), dtype=_np.uint16)
        packed = (
            ((arr[:, :, 0] & 0xF8) << 8)
            | ((arr[:, :, 1] & 0xFC) << 3)
            | (arr[:, :, 2] >> 3)
        )
        return packed.astype(">u2").tobytes()

    pixels = image.convert("RGB").tobytes()
    out = bytearray(len(pixels) // 3 * 2)
    for index in range(len(pixels) // 3):
        offset = index * 3
        value = (
            ((pixels[offset] & 0xF8) << 8)
            | ((pixels[offset + 1] & 0xFC) << 3)
            | (pixels[offset + 2] >> 3)
        )
        out[index * 2] = (value >> 8) & 0xFF
        out[index * 2 + 1] = value & 0xFF
    return bytes(out)


class Display:
    def __init__(self, board, settings):
        self.board = board
        self.settings = settings.ui
        self.width = theme.SCREEN_WIDTH
        self.height = theme.SCREEN_HEIGHT

        self._lock = threading.Lock()
        self._last_hash = None
        self._last_led = None
        self._backlight = None
        self._last_activity = time.monotonic()
        self.frames_pushed = 0
        self.frames_skipped = 0

        self.set_backlight(self.settings.brightness)

    # --- canvas --------------------------------------------------------
    def new_canvas(self, background=theme.BG):
        image = Image.new("RGB", (self.width, self.height), background)
        return image, ImageDraw.Draw(image)

    def present(self, image: Image.Image, force: bool = False) -> bool:
        if not getattr(self.board, "foreground_ready", True):
            return False
        raw = image.tobytes()
        digest = hash(raw)
        if not force and digest == self._last_hash:
            self.frames_skipped += 1
            return False
        try:
            frame = image_to_rgb565(image)
        except Exception:
            log.exception("RGB565 conversion failed")
            return False
        with self._lock:
            try:
                self.board.draw_image(0, 0, self.width, self.height, frame)
            except Exception:
                log.warning("framebuffer write failed", exc_info=True)
                return False
        self._last_hash = digest
        self.frames_pushed += 1
        return True

    def invalidate(self):
        """Force the next present() through, e.g. after regaining focus."""
        self._last_hash = None

    # --- LED -----------------------------------------------------------
    def set_led(self, colour, fade_ms: int = 0):
        if not self.settings.led_enabled:
            return
        if not getattr(self.board, "foreground_ready", True):
            return
        colour = tuple(int(c) for c in colour)
        if colour == self._last_led:
            return
        self._last_led = colour
        try:
            if fade_ms and hasattr(self.board, "set_rgb_fade"):
                self.board.set_rgb_fade(*colour, fade_ms)
            else:
                self.board.set_rgb(*colour)
        except Exception:
            log.debug("LED write failed", exc_info=True)

    # --- backlight / idle policy ---------------------------------------
    def set_backlight(self, brightness: int):
        brightness = max(0, min(100, int(brightness)))
        if brightness == self._backlight:
            return
        self._backlight = brightness
        try:
            self.board.set_backlight(brightness)
        except Exception:
            log.debug("backlight write failed", exc_info=True)

    def restore_backlight(self):
        """Return the panel to normal brightness.

        The daemon sets the backlight exactly once, when it starts, and
        `_release_focus` does not touch it -- the desktop simply inherits
        whatever the last foreground app left behind. So an app that dims
        or blanks the screen owns the job of handing it back lit, or the
        user returns to a desktop that is being drawn correctly onto a
        dark panel and looks like dead hardware.
        """
        self._last_activity = time.monotonic()
        self.set_backlight(self.settings.brightness)
        self.invalidate()

    def poke(self):
        """The operator did something, or a packet arrived: wake the screen."""
        self._last_activity = time.monotonic()
        if self._backlight != self.settings.brightness:
            self.set_backlight(self.settings.brightness)
            self.invalidate()

    def idle_seconds(self) -> float:
        return time.monotonic() - self._last_activity

    @property
    def screen_off(self) -> bool:
        return self._backlight == 0

    def apply_idle_policy(self, keep_awake: bool = False):
        """Step the backlight down as idle time accumulates.

        `keep_awake` holds full brightness while transmitting, recording
        or playing -- the operator is mid-action even if not pressing
        anything.
        """
        if keep_awake:
            self.poke()
            return
        idle = self.idle_seconds()
        if self.settings.idle_off_seconds and idle >= self.settings.idle_off_seconds:
            self.set_backlight(0)
        elif self.settings.idle_dim_seconds and idle >= self.settings.idle_dim_seconds:
            self.set_backlight(self.settings.idle_dim_brightness)

    def next_idle_deadline(self) -> float:
        """Seconds until the backlight next needs changing. inf when settled."""
        idle = self.idle_seconds()
        for threshold in (self.settings.idle_dim_seconds,
                          self.settings.idle_off_seconds):
            if threshold and idle < threshold:
                return threshold - idle
        return float("inf")
