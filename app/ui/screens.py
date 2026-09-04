"""The four screens, and the state object they render from.

Screens are pure functions of `ViewState`: they read, they draw, they
never touch the radio or the audio devices. That keeps rendering
testable off-device (see tests/test_screens.py, which renders every
screen in every radio state to PNG without a Pi anywhere) and keeps the
draw path free of anything that could block the UI thread.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from app.ui import theme, widgets
from app.ui.widgets import centred, ellipsise, meter, panel, signal_bars, vu_meter

CONTACTS = "contacts"
TALK = "talk"
INBOX = "inbox"
STATUS = "status"
SETTINGS = "settings"
# An editor is modal: it owns every gesture while it is open, so it is a
# screen rather than an overlay on one.
EDIT = "edit"
SCREEN_ORDER = (CONTACTS, TALK, INBOX, STATUS, SETTINGS)

# Radio states, in the order they occur during one exchange.
IDLE = "idle"
RECORDING = "recording"
SENDING = "sending"
RECEIVING = "receiving"
PLAYING = "playing"

HEADER_HEIGHT = 30
FOOTER_Y = 244


@dataclass
class ViewState:
    """Everything the screens are allowed to know."""

    screen: str = CONTACTS
    radio_state: str = IDLE
    callsign: str = ""
    address: int = 0
    frequency_mhz: int = 868

    entries: list = field(default_factory=list)
    selected_index: int = 0
    target_name: str = ""

    record_level: float = 0.0
    record_seconds: float = 0.0
    max_record_seconds: float = 20.0

    tx_sent: int = 0
    tx_total: int = 0

    last_rssi: int | None = None
    duty_fraction: float = 0.0
    duty_remaining: float = 0.0
    queued: int = 0

    inbox: list = field(default_factory=list)
    inbox_index: int = 0
    unread: int = 0

    settings_items: list = field(default_factory=list)
    settings_index: int = 0
    editor: object = None
    editor_title: str = ""
    editor_hint: str = ""

    link_states: dict = field(default_factory=dict)
    target_linked: bool = False

    radio_deaf: bool = False
    radio_note: str = ""

    audio_ok: bool = True
    audio_note: str = ""
    codec_name: str = "700C"
    banner: str = ""
    banner_until: float = 0.0
    stats: dict = field(default_factory=dict)

    @property
    def selected(self):
        return self.entries[self.selected_index] if self.entries else None

    @property
    def busy(self) -> bool:
        return self.radio_state in (RECORDING, SENDING, PLAYING)

    def flash(self, message: str, seconds: float = 2.5):
        self.banner = message
        self.banner_until = time.monotonic() + seconds

    @property
    def active_banner(self) -> str:
        return self.banner if time.monotonic() < self.banner_until else ""


STATE_COLOUR = {
    IDLE: theme.TEXT_DIM, RECORDING: theme.DANGER, SENDING: theme.WARN,
    RECEIVING: theme.OK, PLAYING: theme.VOICE,
}
STATE_LABEL = {
    IDLE: "READY", RECORDING: "RECORDING", SENDING: "SENDING",
    RECEIVING: "RECEIVING", PLAYING: "PLAYING",
}


# --- chrome ------------------------------------------------------------
def draw_header(draw, state: ViewState, title: str):
    draw.rectangle([0, 0, theme.SCREEN_WIDTH, HEADER_HEIGHT], fill=theme.SURFACE)
    draw.line([0, HEADER_HEIGHT, theme.SCREEN_WIDTH, HEADER_HEIGHT],
              fill=theme.BORDER)
    draw.text((10, 8), ellipsise(draw, title, theme.font(14, "bold"), 140),
              font=theme.font(14, "bold"), fill=theme.TEXT)

    signal_bars(draw, 196, 9, state.last_rssi)

    # Duty-cycle pressure: a thin bar that only earns attention when high.
    if state.duty_fraction > 0.01:
        colour = theme.OK if state.duty_fraction < 0.6 else (
            theme.WARN if state.duty_fraction < 0.9 else theme.DANGER
        )
        meter(draw, [166, 12, 188, 18], state.duty_fraction, colour, radius=2)


def draw_footer(draw, state: ViewState, lines: list):
    banner = state.active_banner
    if banner:
        panel(draw, [8, FOOTER_Y - 4, theme.SCREEN_WIDTH - 8, FOOTER_Y + 26],
              fill=theme.SURFACE_HI)
        centred(draw, FOOTER_Y + 3,
                ellipsise(draw, banner, theme.font(13, "bold"), 210),
                theme.font(13, "bold"), theme.TEXT)
        return
    widgets.hint(draw, FOOTER_Y, lines)


# --- contacts ----------------------------------------------------------
def draw_contacts(draw, state: ViewState):
    draw_header(draw, state, "CONTACTS")

    if not state.entries:
        centred(draw, 130, "no contacts", theme.font(15), theme.TEXT_DIM)
        return

    visible = 5
    row_height = 38
    first = max(0, min(state.selected_index - visible // 2,
                       len(state.entries) - visible))
    first = max(0, first)

    for offset, entry in enumerate(state.entries[first:first + visible]):
        index = first + offset
        top = HEADER_HEIGHT + 8 + offset * row_height
        chosen = index == state.selected_index
        panel(draw, [6, top, theme.SCREEN_WIDTH - 6, top + row_height - 6],
              fill=theme.SURFACE_HI if chosen else theme.SURFACE,
              outline=theme.ACCENT if chosen else None)

        # Hearing a station does not prove it hears you, and a one-way
        # link is the classic radio failure -- you talk for a minute
        # before finding out nobody received a word. So the dot means
        # "completed a handshake", and presence alone is only a ring.
        link = state.link_states.get(entry.address)
        if entry.is_broadcast:
            dot, filled = theme.ACCENT, True
        elif link == "linked":
            dot, filled = theme.OK, True
        elif link == "calling":
            dot, filled = theme.WARN, False
        elif link == "rejected":
            dot, filled = theme.DANGER, True
        elif link == "stale" or entry.online:
            dot, filled = theme.WARN, True
        else:
            dot, filled = theme.TEXT_FAINT, False
        box = [16, top + 13, 24, top + 21]
        if filled:
            draw.ellipse(box, fill=dot)
        else:
            draw.ellipse(box, outline=dot, width=2)

        name_font = theme.font(15, "bold" if chosen else "regular")
        draw.text((32, top + 5),
                  ellipsise(draw, entry.name, name_font, 150),
                  font=name_font, fill=theme.TEXT if chosen else theme.TEXT_DIM)
        detail = entry.status if entry.is_broadcast else \
            f"{entry.address} · {entry.status}"
        draw.text((32, top + 20), detail, font=theme.font(11),
                  fill=theme.TEXT_FAINT)

        if entry.last_rssi is not None:
            draw.text((theme.SCREEN_WIDTH - 52, top + 11),
                      f"{entry.last_rssi}", font=theme.font(11),
                      fill=theme.rssi_colour(entry.last_rssi))

    if len(state.entries) > visible:
        centred(draw, HEADER_HEIGHT + 8 + visible * row_height - 2,
                f"{state.selected_index + 1} / {len(state.entries)}",
                theme.font(11), theme.TEXT_FAINT)

    draw_footer(draw, state, _hints(CONTACTS))


# --- talk --------------------------------------------------------------
def draw_talk(draw, state: ViewState):
    draw_header(draw, state, state.target_name or "ALL STATIONS")

    colour = STATE_COLOUR[state.radio_state]
    label = STATE_LABEL[state.radio_state]

    # The state disc: the one element readable at arm's length.
    cx, cy, radius = theme.SCREEN_WIDTH // 2, 108, 52
    draw.ellipse([cx - radius, cy - radius, cx + radius, cy + radius],
                 fill=theme.SURFACE, outline=colour, width=4)

    if state.radio_state == RECORDING:
        # Inner disc grows with voice level: instant proof the mic is live.
        inner = int(12 + state.record_level * (radius - 20))
        draw.ellipse([cx - inner, cy - inner, cx + inner, cy + inner], fill=colour)
        centred(draw, cy + radius + 10, f"{state.record_seconds:.1f}s",
                theme.font(20, "bold"), theme.TEXT)
    elif state.radio_state == SENDING:
        fraction = state.tx_sent / state.tx_total if state.tx_total else 0.0
        centred(draw, cy - 14, f"{int(fraction * 100)}%",
                theme.font(28, "bold"), theme.TEXT)
        centred(draw, cy + radius + 10,
                f"packet {state.tx_sent}/{state.tx_total}",
                theme.font(13), theme.TEXT_DIM)
    elif state.radio_state == PLAYING:
        draw.polygon([(cx - 14, cy - 20), (cx - 14, cy + 20), (cx + 20, cy)],
                     fill=colour)
    elif state.radio_state == RECEIVING:
        for ring in range(3):
            size = 18 + ring * 12
            draw.arc([cx - size, cy - size, cx + size, cy + size],
                     start=300, end=60, fill=colour, width=3)
    else:
        centred(draw, cy - 12, "HOLD", theme.font(24, "bold"), theme.TEXT_DIM)
        centred(draw, cy + 14, "to talk", theme.font(13), theme.TEXT_FAINT)

    centred(draw, 42, label, theme.font(14, "bold"), colour)


    # Level or progress bar under the disc.
    bar = [24, 186, theme.SCREEN_WIDTH - 24, 198]
    if state.radio_state == RECORDING:
        vu_meter(draw, bar, state.record_level)
        remaining = state.max_record_seconds - state.record_seconds
        if remaining <= 5:
            centred(draw, 204, f"{remaining:.0f}s left",
                    theme.font(12, "bold"), theme.WARN)
    elif state.radio_state == SENDING:
        meter(draw, bar,
              state.tx_sent / state.tx_total if state.tx_total else 0.0, theme.WARN)
    else:
        detail = f"codec2 {state.codec_name}  ·  {state.frequency_mhz} MHz"
        colour = theme.TEXT_FAINT
        if state.radio_deaf:
            # Worth shouting about: everything else looks like it works.
            detail = "RADIO DEAF — check M0/M1 jumpers"
            colour = theme.DANGER
        elif not state.audio_ok:
            detail = state.audio_note or "no audio device"
            colour = theme.DANGER
        elif not state.target_linked:
            # Shown here rather than beside the disc, where it collided
            # with the ring at this font size.
            link = None
            if state.entries and state.selected_index < len(state.entries):
                link = state.link_states.get(
                    state.entries[state.selected_index].address)
            detail, colour = {
                "calling": ("calling…", theme.WARN),
                "rejected": ("refused the link", theme.DANGER),
                "stale": ("not heard recently", theme.WARN),
            }.get(link, ("not connected — open Talk to call", theme.TEXT_FAINT))
        centred(draw, 188, detail, theme.font(12), colour)

    if state.queued:
        centred(draw, 220, f"{state.queued} queued", theme.font(11), theme.WARN)
    elif state.duty_remaining < 5 and state.duty_fraction > 0:
        centred(draw, 220, f"duty cycle: {state.duty_remaining:.0f}s left",
                theme.font(11), theme.WARN)

    draw_footer(draw, state, _hints(TALK))


# --- inbox -------------------------------------------------------------
def draw_inbox(draw, state: ViewState):
    draw_header(draw, state, f"INBOX{f'  ({state.unread})' if state.unread else ''}")

    if not state.inbox:
        centred(draw, 120, "nothing received yet", theme.font(14), theme.TEXT_DIM)
        centred(draw, 142, "the radio is listening", theme.font(12), theme.TEXT_FAINT)
        draw_footer(draw, state, _hints(INBOX, inbox_empty=True))
        return

    visible = 5
    row_height = 40
    first = max(0, min(state.inbox_index - visible // 2, len(state.inbox) - visible))
    first = max(0, first)

    for offset, item in enumerate(state.inbox[first:first + visible]):
        index = first + offset
        top = HEADER_HEIGHT + 6 + offset * row_height
        chosen = index == state.inbox_index
        panel(draw, [6, top, theme.SCREEN_WIDTH - 6, top + row_height - 6],
              fill=theme.SURFACE_HI if chosen else theme.SURFACE,
              outline=theme.ACCENT if chosen else None)

        tint = theme.VOICE if item.kind == "voice" else theme.ACCENT
        draw.rectangle([6, top, 10, top + row_height - 6], fill=tint)
        if not item.played and not item.outgoing:
            draw.ellipse([theme.SCREEN_WIDTH - 22, top + 8,
                          theme.SCREEN_WIDTH - 14, top + 16], fill=theme.OK)

        who = ("to " if item.outgoing else "") + (item.peer_name or f"node {item.src}")
        draw.text((18, top + 4), ellipsise(draw, who, theme.font(13, "bold"), 150),
                  font=theme.font(13, "bold"), fill=theme.TEXT)
        draw.text((18, top + 20),
                  ellipsise(draw, item.summary, theme.font(11), 150),
                  font=theme.font(11),
                  fill=theme.WARN if item.incomplete else theme.TEXT_DIM)
        draw.text((theme.SCREEN_WIDTH - 52, top + 21), item.when,
                  font=theme.font(10), fill=theme.TEXT_FAINT)

    draw_footer(draw, state, _hints(INBOX))


# --- status ------------------------------------------------------------
def draw_status(draw, state: ViewState):
    draw_header(draw, state, "STATUS")

    rows = [
        ("callsign", state.callsign or "-"),
        ("address", str(state.address)),
        ("frequency", f"{state.frequency_mhz} MHz"),
        ("codec", f"codec2 {state.codec_name}"),
        ("audio", state.audio_note or ("ok" if state.audio_ok else "unavailable")),
        ("link", "connected" if state.target_linked else "not connected"),
        ("mode pins", state.radio_note or "not checked"),
        ("last rssi", f"{state.last_rssi} dBm" if state.last_rssi is not None else "-"),
        ("duty cycle", f"{state.duty_fraction * 100:.0f}% used"
                       f"  ({state.duty_remaining:.0f}s left)"),
    ]
    stats = state.stats or {}
    rows.append(("packets", f"tx {stats.get('packets_tx', 0)}  "
                            f"rx {stats.get('packets_rx', 0)}"))
    rows.append(("dropped", str(stats.get('frames_dropped', 0))))

    top = HEADER_HEIGHT + 8
    for index, (label, value) in enumerate(rows):
        y = top + index * 21
        draw.text((12, y), label, font=theme.font(11), fill=theme.TEXT_FAINT)
        colour = theme.TEXT
        if label == "audio" and not state.audio_ok:
            colour = theme.DANGER
        elif label == "mode pins" and state.radio_deaf:
            colour = theme.DANGER
        elif label == "link":
            colour = theme.OK if state.target_linked else theme.TEXT_DIM
        draw.text((96, y - 1), ellipsise(draw, value, theme.font(12), 134),
                  font=theme.font(12), fill=colour)

    draw_footer(draw, state, _hints(STATUS))


def _hints(screen: str, inbox_empty: bool = False) -> list:
    """Footer text, generated from the gesture table the app dispatches on.

    Imported lazily: navigation imports this module for the screen names,
    so a module-level import would be circular.
    """
    from app.ui import navigation

    return navigation.hints(screen, inbox_empty)


# --- settings ----------------------------------------------------------
def draw_settings(draw, state: ViewState):
    draw_header(draw, state, "SETTINGS")

    if not state.settings_items:
        centred(draw, 130, "no settings", theme.font(15), theme.TEXT_DIM)
        return

    visible = 5
    row_height = 38
    first = max(0, min(state.settings_index - visible // 2,
                       len(state.settings_items) - visible))
    first = max(0, first)

    for offset, item in enumerate(state.settings_items[first:first + visible]):
        index = first + offset
        top = HEADER_HEIGHT + 8 + offset * row_height
        chosen = index == state.settings_index
        # Destructive entries are tinted so they are never opened by reflex.
        accent = theme.DANGER if item.get("destructive") else theme.ACCENT
        panel(draw, [6, top, theme.SCREEN_WIDTH - 6, top + row_height - 6],
              fill=theme.SURFACE_HI if chosen else theme.SURFACE,
              outline=accent if chosen else None)

        name_font = theme.font(14, "bold" if chosen else "regular")
        draw.text((16, top + 5),
                  ellipsise(draw, item["label"], name_font, 200),
                  font=name_font,
                  fill=(theme.DANGER if item.get("destructive")
                        else (theme.TEXT if chosen else theme.TEXT_DIM)))
        value = str(item.get("value", ""))
        if value:
            draw.text((16, top + 20),
                      ellipsise(draw, value, theme.font(11), 200),
                      font=theme.font(11), fill=theme.TEXT_FAINT)

    if len(state.settings_items) > visible:
        centred(draw, HEADER_HEIGHT + 8 + visible * row_height - 2,
                f"{state.settings_index + 1} / {len(state.settings_items)}",
                theme.font(11), theme.TEXT_FAINT)

    draw_footer(draw, state, _hints(SETTINGS))


def draw_editor(draw, state: ViewState):
    """A modal value editor: one big value, and what the clicks do to it."""
    editor = state.editor
    draw_header(draw, state, state.editor_title or "EDIT")
    if editor is None:
        return

    confirming = hasattr(editor, "prompt")
    accent = theme.DANGER if confirming else theme.ACCENT

    if confirming:
        centred(draw, 66, editor.prompt, theme.font(15, "bold"), theme.TEXT)
        if editor.detail:
            for index, line in enumerate(editor.detail.split("\n")[:3]):
                centred(draw, 92 + index * 16, line, theme.font(12), theme.TEXT_DIM)
        chosen = theme.DANGER if editor.yes else theme.OK
        panel(draw, [60, 148, theme.SCREEN_WIDTH - 60, 194],
              fill=theme.SURFACE_HI, outline=chosen)
        centred(draw, 158, editor.text, theme.font(26, "bold"), chosen)
    else:
        panel(draw, [12, 96, theme.SCREEN_WIDTH - 12, 168],
              fill=theme.SURFACE, outline=accent)
        text = editor.text
        size = 40 if len(text) <= 6 else (22 if len(text) <= 12 else 17)
        centred(draw, 96 + (72 - size) // 2 - 4, text,
                theme.font(size, "bold"), theme.TEXT)

        # Underline the field being edited, so the cursor is unmistakable.
        cursor_label = None
        if hasattr(editor, "cursor") and hasattr(editor, "digits"):
            font = theme.font(size, "bold")
            width = int(draw.textlength(text, font=font))
            per = width / max(1, len(text))
            left = (theme.SCREEN_WIDTH - width) / 2 + editor.cursor * per
            y = 96 + (72 - size) // 2 - 4 + size + 2
            draw.rectangle([left + 1, y, left + per - 1, y + 3], fill=accent)
            cursor_label = f"digit {editor.cursor + 1} of {editor.digits}"
        elif hasattr(editor, "field_name"):
            cursor_label = f"editing {editor.field_name}"
        if cursor_label:
            centred(draw, 174, cursor_label, theme.font(11), theme.TEXT_FAINT)

    if state.editor_hint:
        centred(draw, 200, ellipsise(draw, state.editor_hint, theme.font(11), 220),
                theme.font(11), theme.WARN)

    commit = "confirm" if confirming else (
        "save" if getattr(editor, "cursor", 0) >= getattr(editor, "digits", 1) - 1
        and hasattr(editor, "digits") else "next")
    draw_footer(draw, state, [
        f"1 click change  ·  2 clicks {commit}",
        "3 clicks cancel  ·  4 clicks exit",
    ])


RENDERERS = {
    CONTACTS: draw_contacts, TALK: draw_talk, INBOX: draw_inbox,
    STATUS: draw_status, SETTINGS: draw_settings, EDIT: draw_editor,
}


def render(display, state: ViewState):
    """Draw the current screen and push it if it changed."""
    image, draw = display.new_canvas()
    RENDERERS.get(state.screen, draw_contacts)(draw, state)
    return display.present(image)
