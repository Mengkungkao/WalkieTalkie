"""Every screen, in every radio state, rendered without a Pi.

Screens are pure functions of ViewState, so the whole UI can be
exercised on a development machine. This catches the failure mode that
is most painful to debug on the device itself: a layout that raises only
when some field is empty, None, or longer than the panel is wide.
"""

from __future__ import annotations

import pytest

from app.store.inbox import Item
from app.store.roster import Entry
from app.ui import screens, theme
from app.ui.editors import ChoiceEditor, ClockEditor, ConfirmEditor, DigitEditor
from app.ui.screens import (CONTACTS, EDIT, IDLE, INBOX, PLAYING, RECEIVING,
                            RECORDING, SENDING, SETTINGS, STATUS, TALK,
                            ViewState)


class FakeBoard:
    foreground_ready = True

    def __init__(self):
        self.frames = []

    def draw_image(self, x, y, width, height, pixels):
        self.frames.append(pixels)

    def set_backlight(self, brightness): pass
    def set_rgb(self, r, g, b): pass
    def set_rgb_fade(self, r, g, b, duration_ms=100): pass


@pytest.fixture
def display():
    from app.config.settings import Settings
    from app.ui.display import Display

    return Display(FakeBoard(), Settings())


def populated_state(**overrides) -> ViewState:
    state = ViewState(
        callsign="Rover", address=5, frequency_mhz=868,
        entries=[
            Entry("ALL STATIONS", 0xFFFF, True),
            Entry("Base", 1, True, last_heard=1.0, last_rssi=-72),
            Entry("A station with a very long name indeed", 2, False,
                  last_heard=1.0, last_rssi=-118),
        ],
        inbox=[
            Item("a", "voice", 1, "Base", 1.0, -80, duration=4.2),
            Item("b", "text", 2, "Hilltop", 1.0, -104,
                 text="a long message that will certainly not fit on one line"),
            Item("c", "voice", 1, "Base", 1.0, None, duration=9.9,
                 incomplete=True, played=True),
        ],
        target_name="Base", last_rssi=-88, duty_fraction=0.42,
        duty_remaining=20.9, codec_name="700C",
        stats={"packets_tx": 12, "packets_rx": 34, "frames_dropped": 2},
    )
    for key, value in overrides.items():
        setattr(state, key, value)
    return state


SETTINGS_ITEMS = [
    {"key": "device_id", "label": "Device ID", "value": "5  (Rover)"},
    {"key": "base", "label": "Base station", "value": "Base"},
    {"key": "add", "label": "Add device", "value": "pair another radio by address"},
    {"key": "clock", "label": "Date & time", "value": "2026-09-04 14:30  ·  system clock"},
    {"key": "reset", "label": "Reset all data", "value": "3 message(s), roster, settings",
     "destructive": True},
]


@pytest.mark.parametrize("index", range(len(SETTINGS_ITEMS)))
def test_settings_screen_renders_with_each_row_selected(display, index):
    state = populated_state(screen=SETTINGS, settings_items=SETTINGS_ITEMS,
                            settings_index=index)
    assert screens.render(display, state) is True


@pytest.mark.parametrize("editor,title", [
    (DigitEditor(65534, digits=5), "DEVICE ID"),
    (DigitEditor(0, digits=5), "ADD DEVICE"),
    (ChoiceEditor([("Base", 1), ("Rover", 5)]), "BASE STATION"),
    (ClockEditor(__import__("datetime").datetime(2026, 9, 4, 14, 30)), "DATE & TIME"),
    (ConfirmEditor("Erase everything?", "messages, voice clips\nand settings"), "RESET"),
])
def test_every_editor_renders(display, editor, title):
    state = populated_state(screen=EDIT, editor=editor, editor_title=title)
    assert screens.render(display, state) is True


def test_editor_renders_at_every_cursor_position(display):
    """The cursor underline is positioned by hand; check it never overflows."""
    editor = DigitEditor(65534, digits=5)
    for cursor in range(editor.digits):
        editor.cursor = cursor
        display.invalidate()
        state = populated_state(screen=EDIT, editor=editor, editor_title="DEVICE ID")
        assert screens.render(display, state) is True


def test_editor_screen_survives_a_missing_editor(display):
    """Focus can be revoked mid-edit; rendering must not raise."""
    state = populated_state(screen=EDIT, editor=None, editor_title="DEVICE ID")
    assert screens.render(display, state) is True


@pytest.mark.parametrize("screen", [CONTACTS, INBOX, STATUS])
def test_list_screens_render(display, screen):
    state = populated_state(screen=screen)
    assert screens.render(display, state) is True


@pytest.mark.parametrize("radio_state", [IDLE, RECORDING, SENDING, RECEIVING, PLAYING])
def test_talk_screen_renders_in_every_state(display, radio_state):
    state = populated_state(
        screen="talk", radio_state=radio_state, record_level=0.62,
        record_seconds=3.4, tx_sent=2, tx_total=6,
    )
    assert screens.render(display, state) is True


@pytest.mark.parametrize("screen", [CONTACTS, "talk", INBOX, STATUS, SETTINGS, EDIT])
def test_screens_render_with_nothing_in_them(display, screen):
    """First boot: no contacts, no messages, no signal, no audio."""
    state = ViewState(screen=screen, audio_ok=False, audio_note="no audio hardware")
    assert screens.render(display, state) is True


def test_identical_frames_are_not_pushed_twice(display):
    """The dedupe is what keeps an idle radio from burning a core."""
    state = populated_state(screen=CONTACTS)
    assert screens.render(display, state) is True
    assert screens.render(display, state) is False
    assert display.frames_skipped == 1


def test_a_changed_frame_is_pushed(display):
    state = populated_state(screen=CONTACTS)
    screens.render(display, state)
    state.selected_index = 1
    assert screens.render(display, state) is True


def test_banner_expires(display):
    state = populated_state(screen="talk")
    state.flash("sent", seconds=0.0)
    assert state.active_banner == ""


@pytest.mark.parametrize("screen", [CONTACTS, TALK, INBOX, STATUS, SETTINGS])
def test_footer_hints_fit_the_panel(screen):
    """Text that overflows is clipped at both ends and reads as gibberish.

    Every hint line was 250-272 px wide against a 240 px panel until a
    screenshot showed it; rendering without raising is not the same as
    fitting.
    """
    from PIL import Image, ImageDraw

    from app.ui import navigation

    draw = ImageDraw.Draw(Image.new("RGB", (theme.SCREEN_WIDTH, theme.SCREEN_HEIGHT)))
    font = theme.font(12)
    margin = 4
    for line in navigation.hints(screen):
        width = draw.textlength(line, font=font)
        assert width <= theme.SCREEN_WIDTH - margin, (
            f"{screen}: {line!r} is {width:.0f}px, panel is "
            f"{theme.SCREEN_WIDTH}px"
        )


def test_empty_inbox_hint_fits_too():
    from PIL import Image, ImageDraw

    from app.ui import navigation

    draw = ImageDraw.Draw(Image.new("RGB", (theme.SCREEN_WIDTH, theme.SCREEN_HEIGHT)))
    for line in navigation.hints(INBOX, inbox_empty=True):
        assert draw.textlength(line, font=theme.font(12)) <= theme.SCREEN_WIDTH - 4


def test_frame_is_the_size_the_daemon_expects(display):
    """240x280 RGB565: 134,400 bytes, and the daemon rejects anything else."""
    screens.render(display, populated_state(screen=CONTACTS))
    frame = display.board.frames[-1]
    assert len(frame) == theme.SCREEN_WIDTH * theme.SCREEN_HEIGHT * 2 == 134_400


# --- layout: nothing may reach the footer -----------------------------
def _bottom_of_drawn_content(image):
    """Lowest row with any non-background pixel, ignoring the footer band."""
    import numpy as np

    from app.ui import screens as scr

    arr = np.asarray(image.convert("RGB"))
    background = np.array(theme.BG, dtype=arr.dtype)
    painted = (arr != background).any(axis=2)
    rows = np.where(painted[:scr.FOOTER_Y - 2].any(axis=1))[0]
    return int(rows[-1]) if rows.size else 0


@pytest.mark.parametrize("screen", [CONTACTS, INBOX, STATUS, SETTINGS])
def test_content_never_reaches_the_footer(display, screen):
    """The status screen used to print three rows through the hints."""
    from app.ui import screens as scr

    state = populated_state(screen=screen, settings_items=SETTINGS_ITEMS,
                            battery_present=True, battery_percent=93.9,
                            battery_summary="94%  ~2.3h left",
                            radio_note="transparent mode",
                            audio_note="whisplaysound")
    image, draw = display.new_canvas()
    scr.RENDERERS[screen](draw, state)
    bottom = _bottom_of_drawn_content(image)
    assert bottom < scr.CONTENT_BOTTOM + 4, (
        f"{screen} draws down to y={bottom}, past CONTENT_BOTTOM="
        f"{scr.CONTENT_BOTTOM}")


def test_the_broadcast_entry_is_short_enough_for_its_row():
    """"ALL STATIONS" crowded out the address and last-heard line."""
    from PIL import Image, ImageDraw

    from app.store.roster import BROADCAST_NAME

    draw = ImageDraw.Draw(Image.new("RGB", (240, 280)))
    width = draw.textlength(BROADCAST_NAME, font=theme.font(15, "bold"))
    assert width <= 90, f"{BROADCAST_NAME!r} is {width:.0f}px"


def test_mains_power_is_shown_rather_than_left_blank(display):
    """A base station on mains should say so, not show an empty gap."""
    state = populated_state(battery_present=False)
    image, draw = display.new_canvas()
    screens.draw_header(draw, state, "MengPi")
    arr = image.crop((196, 6, 232, 24)).convert("RGB").getcolors(4096)
    assert any(colour != theme.SURFACE and count < 600 for count, colour in arr), \
        "nothing drawn where the power indicator belongs"


# --- signal scale ------------------------------------------------------
@pytest.mark.parametrize("rssi,level", [
    (-60, 4), (-80, 4), (-81, 3), (-95, 3), (-96, 2),
    (-105, 2), (-106, 1), (-115, 1), (-116, 0), (None, 0),
])
def test_signal_level_matches_the_published_bands(rssi, level):
    assert theme.signal_level(rssi) == level


def test_bars_and_colour_cannot_disagree():
    """They were written separately: -86 dBm gave four bars in amber."""
    for rssi in range(-130, -50):
        level = theme.signal_level(rssi)
        colour = theme.signal_colour(level)
        if level >= 3:
            assert colour == theme.OK
        elif level == 2:
            assert colour == theme.WARN
        elif level == 1:
            assert colour == theme.DANGER
        assert theme.rssi_colour(rssi) == colour
        assert theme.rssi_bars(rssi) == level


def test_the_scale_is_monotonic():
    """A stronger signal must never show fewer bars."""
    levels = [theme.signal_level(r) for r in range(-130, -50)]
    assert levels == sorted(levels)


def test_every_level_has_a_word_for_it():
    for level in range(5):
        assert theme.SIGNAL_LABELS[level]


def test_stronger_signals_paint_more_bars(display):
    """Height and colour are the whole indicator now, so they must move."""
    from PIL import Image, ImageDraw

    from app.ui.widgets import signal_bars

    painted = []
    for rssi in (-120, -100, -60):
        image = Image.new("RGB", (240, 30), theme.BG)
        signal_bars(ImageDraw.Draw(image), 20, 9, rssi)
        lit = sum(1 for p in image.getdata()
                  if p not in (theme.BG, theme.SURFACE_HI))
        painted.append(lit)
    assert painted[0] < painted[1] < painted[2]


# --- rows: text must stay inside its own selection frame ---------------
@pytest.mark.parametrize("primary,secondary", [
    (theme.font(15, "bold"), theme.font(11)),
    (theme.font(13, "bold"), theme.font(11)),
    (theme.font(14, "bold"), theme.font(11)),
    (theme.font(12, "bold"), theme.font(10)),
])
def test_two_line_rows_fit_their_panel(primary, secondary):
    """The detail line used to be drawn across the bottom border."""
    from PIL import Image, ImageDraw

    from app.ui.widgets import two_line_row

    pad = 5
    height, first_y, second_y = two_line_row(primary, secondary, pad=pad, gap=2)
    draw = ImageDraw.Draw(Image.new("RGB", (240, 80)))

    top = draw.textbbox((0, first_y), "Ag", font=primary)[1]
    bottom = draw.textbbox((0, second_y), "Ag", font=secondary)[3]
    assert top >= pad - 1, f"first line starts at {top}, above the padding"
    assert bottom <= height - pad + 1, (
        f"second line ends at {bottom}, panel is {height} tall")


@pytest.mark.parametrize("screen", [CONTACTS, INBOX, SETTINGS])
def test_row_text_does_not_cross_the_panel_border(display, screen):
    """Render for real and check no text sits on a selection outline."""
    import numpy as np

    from app.ui import screens as scr

    state = populated_state(screen=screen, settings_items=SETTINGS_ITEMS,
                            selected_index=0, inbox_index=0, settings_index=0)
    image, draw = display.new_canvas()
    scr.RENDERERS[screen](draw, state)
    arr = np.asarray(image.convert("RGB"))

    panel_height = {CONTACTS: scr.CONTACT_PANEL, INBOX: scr.INBOX_PANEL,
                    SETTINGS: scr.SETTING_PANEL}[screen]
    row_height = {CONTACTS: scr.CONTACT_ROW, INBOX: scr.INBOX_ROW,
                  SETTINGS: scr.SETTING_ROW}[screen]

    # The gap between one panel's bottom and the next panel's top must be
    # background: anything else is a row bleeding out of its frame.
    for index in range(2):
        gap_top = scr.CONTENT_TOP + index * row_height + panel_height + 2
        gap_bottom = scr.CONTENT_TOP + (index + 1) * row_height - 1
        if gap_bottom <= gap_top or gap_bottom >= scr.CONTENT_BOTTOM:
            continue
        band = arr[gap_top:gap_bottom, 12:theme.SCREEN_WIDTH - 12]
        assert (band == np.array(theme.BG, dtype=arr.dtype)).all(), (
            f"{screen}: something is drawn between rows {index} and "
            f"{index + 1} (y {gap_top}-{gap_bottom})")
