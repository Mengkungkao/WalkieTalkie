"""Palette, fonts and metrics for the 240x280 panel.

Colours are picked for a small, often outdoor-viewed LCD: strongly
separated hues at high value, no mid-grey text, and one unmistakable
colour per radio state -- red is transmitting, green is receiving,
amber is waiting on the duty cycle. On a walkie-talkie the operator
reads the screen at arm's length in a glance, so state is carried by
fill colour and size, never by a thin outline.
"""

from __future__ import annotations

from PIL import ImageFont

SCREEN_WIDTH = 240
SCREEN_HEIGHT = 280

BG = (10, 12, 16)
SURFACE = (22, 26, 34)
SURFACE_HI = (36, 42, 54)
BORDER = (58, 66, 82)

TEXT = (236, 240, 246)
TEXT_DIM = (140, 150, 166)
TEXT_FAINT = (92, 100, 116)

ACCENT = (86, 168, 255)     # selection, links
OK = (64, 208, 138)         # receiving, online
WARN = (245, 178, 62)       # duty cycle pressure, incomplete
DANGER = (255, 92, 92)      # transmitting, errors
VOICE = (188, 132, 255)     # voice messages

LED_IDLE = (0, 6, 10)
LED_TX = (60, 0, 0)
LED_RX = (0, 40, 16)
LED_REC = (60, 20, 0)

_FONT_PATHS = {
    "regular": (
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
    ),
    "bold": (
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    ),
    "mono": (
        "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationMono-Regular.ttf",
    ),
}

_cache = {}


def font(size: int, weight: str = "regular"):
    """Cached font lookup; falls back to PIL's bitmap font if none exist."""
    key = (size, weight)
    if key in _cache:
        return _cache[key]
    for path in _FONT_PATHS.get(weight, ()):
        try:
            _cache[key] = ImageFont.truetype(path, size)
            return _cache[key]
        except OSError:
            continue
    _cache[key] = ImageFont.load_default()
    return _cache[key]


def rssi_colour(rssi):
    """Signal strength as a colour. Anything under -110 dBm is marginal."""
    if rssi is None:
        return TEXT_FAINT
    if rssi >= -85:
        return OK
    if rssi >= -105:
        return WARN
    return DANGER


def rssi_bars(rssi) -> int:
    """0-4 bars from a dBm reading."""
    if rssi is None:
        return 0
    for index, floor in enumerate((-120, -110, -100, -88)):
        if rssi < floor:
            return index
    return 4
