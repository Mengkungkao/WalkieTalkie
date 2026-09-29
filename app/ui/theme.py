"""Palette, fonts and metrics for the 240x280 panel (MFruit OS's look).

Colours are picked for a small, often outdoor-viewed LCD: strongly
separated hues at high value, no mid-grey text, and one unmistakable
colour per radio state -- red is transmitting, green is receiving,
amber is waiting on the duty cycle. On a walkie-talkie the operator
reads the screen at arm's length in a glance, so state is carried by
fill colour and size, never by a thin outline.
"""

from __future__ import annotations

from mfruit_sdk.ui import fonts as _fonts
from mfruit_sdk.ui import theme as _mfruit
from PIL import ImageFont

SCREEN_WIDTH = 240
SCREEN_HEIGHT = 280

# The panel's corners are physically rounded -- whisplay.py records it as
# `CornerHeight = 20` and then never uses it, so compensating is the
# application's job. A square-cornered fill drawn to the edge has its
# corners swallowed by the bezel, and anything printed in them is simply
# not there.
CORNER_RADIUS = 20


def corner_inset(y: int) -> int:
    """Horizontal margin needed at this row to stay inside the curve.

    Zero through the straight middle of the panel, rising to the full
    radius at the very top and bottom rows.
    """
    depth = min(y, SCREEN_HEIGHT - 1 - y)
    if depth >= CORNER_RADIUS:
        return 0
    offset = CORNER_RADIUS - depth
    return int(round(CORNER_RADIUS - (CORNER_RADIUS ** 2 - offset ** 2) ** 0.5))

# MFruit OS's palette (mfruit_sdk), so the app looks like the rest of the
# device. The state colours keep their meaning: red is transmitting, green
# receiving, amber waiting on the duty cycle, violet voice.
MFRUIT = _mfruit.DARK

BG = MFRUIT.bg
SURFACE = MFRUIT.surface
SURFACE_HI = MFRUIT.surface_hi
SELECTED = MFRUIT.accent_dim    # a selected row, as in MFruit OS's lists
BORDER = (58, 66, 82)

TEXT = MFRUIT.text
TEXT_DIM = MFRUIT.text_muted
TEXT_FAINT = MFRUIT.text_faint

ACCENT = MFRUIT.accent      # selection, links
OK = MFRUIT.success         # receiving, online
WARN = MFRUIT.warning       # duty cycle pressure, incomplete
DANGER = MFRUIT.error       # transmitting, errors
VOICE = (188, 132, 255)     # voice messages

LED_IDLE = (0, 6, 10)
LED_TX = (60, 0, 0)
LED_RX = (0, 40, 16)
LED_REC = (60, 20, 0)

_MONO_PATHS = (
    "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationMono-Regular.ttf",
)

_cache = {}


def font(size: int, weight: str = "regular"):
    """MFruit OS's font (Inter, or DejaVu without MFruit OS); "mono" is DejaVu Mono."""
    if weight != "mono":
        return _fonts.font(size, "bold" if weight == "bold" else weight)
    key = (size, weight)
    if key in _cache:
        return _cache[key]
    for path in _MONO_PATHS:
        try:
            _cache[key] = ImageFont.truetype(path, size)
            return _cache[key]
        except OSError:
            continue
    _cache[key] = ImageFont.load_default()
    return _cache[key]


# Signal strength, in dBm, at the bottom of each bar. One table, so the
# bar count and the colour cannot disagree -- they used to: at -86 dBm
# the meter showed four bars in amber, and at -90 three bars in amber,
# because the colour thresholds and the bar thresholds were written
# separately and did not line up.
#
# The numbers suit LoRa rather than WiFi. A 868 MHz link is still solid
# at -100 dBm, where WiFi would have given up, so the bands are shifted
# down accordingly.
SIGNAL_FLOORS = (-115, -105, -95, -80)   # 1, 2, 3, 4 bars

SIGNAL_LABELS = {0: "no signal", 1: "weak", 2: "fair", 3: "good", 4: "strong"}


def signal_level(rssi) -> int:
    """0-4 bars from a dBm reading. 0 means nothing heard yet."""
    if rssi is None:
        return 0
    return sum(1 for floor in SIGNAL_FLOORS if rssi >= floor)


def signal_colour(level: int):
    """Green is comfortable, amber is workable, red is about to fail."""
    if level >= 3:
        return OK
    if level == 2:
        return WARN
    if level == 1:
        return DANGER
    return TEXT_FAINT


def rssi_colour(rssi):
    return signal_colour(signal_level(rssi))


def rssi_bars(rssi) -> int:
    return signal_level(rssi)
