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
from app.ui.widgets import (centred, ellipsise, meter, panel, signal_bars,
                            two_line_row, vu_meter)

CONTACTS = "contacts"
TALK = "talk"
INBOX = "inbox"
STATUS = "status"
SETTINGS = "settings"
PAIR = "pair"
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

# The usable band between header and footer. Everything draws inside it:
# the status screen used to run 25 px past the footer and print its last
# three rows straight through the gesture hints.
CONTENT_TOP = HEADER_HEIGHT + 6
CONTENT_BOTTOM = FOOTER_Y - 6
CONTENT_HEIGHT = CONTENT_BOTTOM - CONTENT_TOP

# Shared margins, so columns line up between screens.
MARGIN = 8

# Row geometry, measured from the fonts each list actually uses. These
# were hardcoded, and the detail line was drawn 1 px past the bottom of
# its own selection frame -- visible as the text crossing the border.
ROW_PAD, ROW_GAP, ROW_SPACING = 5, 2, 6

CONTACT_PANEL, CONTACT_NAME_Y, CONTACT_DETAIL_Y = two_line_row(
    theme.font(15, "bold"), theme.font(11), ROW_PAD, ROW_GAP)
CONTACT_ROW = CONTACT_PANEL + ROW_SPACING

INBOX_PANEL, INBOX_NAME_Y, INBOX_DETAIL_Y = two_line_row(
    theme.font(13, "bold"), theme.font(11), ROW_PAD, ROW_GAP)
INBOX_ROW = INBOX_PANEL + ROW_SPACING

SETTING_PANEL, SETTING_NAME_Y, SETTING_DETAIL_Y = two_line_row(
    theme.font(14, "bold"), theme.font(11), ROW_PAD, ROW_GAP)
SETTING_ROW = SETTING_PANEL + ROW_SPACING


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

    # Radios heard pairing: (address, name, rssi, already a contact).
    pair_found: list = field(default_factory=list)
    pair_index: int = 0
    pair_status: str = ""

    link_states: dict = field(default_factory=dict)
    target_linked: bool = False

    battery_present: bool = False
    battery_summary: str = ""
    battery_detail: str = ""
    battery_percent: float | None = None
    battery_low: bool = False

    brightness_locked: bool = False

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
    # Rounded at the top to follow the panel, square at the bottom where
    # it meets the content. A square fill here loses its corners to the
    # bezel and looks like a rendering fault.
    # Round the top, square the bottom. Drawn as a rounded rectangle plus
    # a patch rather than with `corners=`, which needs Pillow 9.4 and is
    # not worth a version floor for two corners.
    radius = theme.CORNER_RADIUS
    draw.rounded_rectangle([0, 0, theme.SCREEN_WIDTH - 1, HEADER_HEIGHT],
                           radius=radius, fill=theme.SURFACE)
    draw.rectangle([0, radius, theme.SCREEN_WIDTH - 1, HEADER_HEIGHT],
                   fill=theme.SURFACE)
    draw.line([0, HEADER_HEIGHT, theme.SCREEN_WIDTH, HEADER_HEIGHT],
              fill=theme.BORDER)
    draw.text((10, 8), ellipsise(draw, title, theme.font(14, "bold"), 96),
              font=theme.font(14, "bold"), fill=theme.TEXT)

    # Right-hand status, ordered like a phone's: how far you can reach,
    # then how long you can keep reaching.
    signal_bars(draw, 144, 9, state.last_rssi)

    x, y, width, height = 172, 10, 22, 11
    draw.rounded_rectangle([x, y, x + width, y + height], radius=2,
                           outline=theme.BORDER)
    draw.rectangle([x + width + 1, y + 3, x + width + 3, y + height - 3],
                   fill=theme.BORDER)

    if state.battery_present and state.battery_percent is not None:
        fraction = max(0.0, min(1.0, state.battery_percent / 100.0))
        colour = (theme.DANGER if state.battery_low else
                  theme.WARN if fraction < 0.4 else theme.OK)
        if fraction > 0.02:
            draw.rectangle([x + 2, y + 2,
                            x + 2 + int((width - 4) * fraction), y + height - 2],
                           fill=colour)
        # A bar answers "roughly?"; the number answers "will this last
        # the walk back?".
        draw.text((200, 9), f"{state.battery_percent:.0f}%",
                  font=theme.font(11), fill=colour)
    else:
        # No battery is not nothing to say -- it means mains, which is
        # what you want to know about a base station. Blank space would
        # read as a missing reading instead.
        draw.polygon([(x + 13, y + 2), (x + 8, y + 6), (x + 11, y + 6),
                      (x + 9, y + 10), (x + 16, y + 5), (x + 12, y + 5)],
                     fill=theme.ACCENT)
        draw.text((200, 9), "EXT", font=theme.font(11), fill=theme.ACCENT)

    # Duty-cycle pressure: a thin bar that only earns attention when high.
    if state.duty_fraction > 0.01:
        colour = theme.OK if state.duty_fraction < 0.6 else (
            theme.WARN if state.duty_fraction < 0.9 else theme.DANGER
        )
        meter(draw, [112, 12, 134, 18], state.duty_fraction, colour, radius=2)


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

    row_height = CONTACT_ROW
    # One row of headroom is kept for the "n / m" counter when the list
    # is longer than the screen.
    visible = min(len(state.entries), (CONTENT_HEIGHT - 14) // row_height)
    visible = max(1, visible)
    first = max(0, min(state.selected_index - visible // 2,
                       len(state.entries) - visible))
    first = max(0, first)

    for offset, entry in enumerate(state.entries[first:first + visible]):
        index = first + offset
        top = CONTENT_TOP + offset * row_height
        chosen = index == state.selected_index
        panel(draw, [6, top, theme.SCREEN_WIDTH - 6, top + CONTACT_PANEL],
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
        box = [16, top + CONTACT_PANEL // 2 - 4, 24, top + CONTACT_PANEL // 2 + 4]
        if filled:
            draw.ellipse(box, fill=dot)
        else:
            draw.ellipse(box, outline=dot, width=2)

        # The name shares the row with the signal reading on the right,
        # so it gets the width that is actually left over.
        rssi_text = "" if entry.last_rssi is None else f"{entry.last_rssi}"
        small = theme.font(11)
        rssi_width = (int(draw.textlength(rssi_text, font=small)) + 10
                      if rssi_text else 0)
        text_width = theme.SCREEN_WIDTH - 32 - MARGIN - 6 - rssi_width

        name_font = theme.font(15, "bold" if chosen else "regular")
        draw.text((32, top + CONTACT_NAME_Y),
                  ellipsise(draw, entry.name, name_font, text_width),
                  font=name_font, fill=theme.TEXT if chosen else theme.TEXT_DIM)
        detail = entry.status if entry.is_broadcast else \
            f"{entry.address} · {entry.status}"
        draw.text((32, top + CONTACT_DETAIL_Y),
                  ellipsise(draw, detail, small, text_width),
                  font=small, fill=theme.TEXT_FAINT)

        if rssi_text:
            draw.text((theme.SCREEN_WIDTH - MARGIN - 6 - rssi_width + 10,
                       top + CONTACT_PANEL // 2 - 7), rssi_text, font=small,
                      fill=theme.rssi_colour(entry.last_rssi))

    if len(state.entries) > visible:
        centred(draw, CONTENT_TOP + visible * row_height,
                f"{state.selected_index + 1} / {len(state.entries)}",
                theme.font(11), theme.TEXT_FAINT)

    draw_footer(draw, state, _hints(CONTACTS))


# --- talk --------------------------------------------------------------
def draw_talk(draw, state: ViewState):
    draw_header(draw, state, state.target_name or "ALL STATIONS")

    colour = STATE_COLOUR[state.radio_state]
    label = STATE_LABEL[state.radio_state]

    # The state disc: the one element readable at arm's length. Sits
    # lower than the label rather than under it -- at 52 px radius from
    # y=108 the ring reached y=56 and collided with the text above.
    cx, cy, radius = theme.SCREEN_WIDTH // 2, 122, 50
    draw.ellipse([cx - radius, cy - radius, cx + radius, cy + radius],
                 fill=theme.SURFACE, outline=colour, width=4)

    if state.radio_state == RECORDING:
        # Inner disc grows with voice level: instant proof the mic is live.
        inner = int(12 + state.record_level * (radius - 20))
        draw.ellipse([cx - inner, cy - inner, cx + inner, cy + inner], fill=colour)
        # Inside the ring, not under it: at 20 px this used to be drawn
        # straight through the level meter below.
        centred(draw, cy + radius + 4, f"{state.record_seconds:.1f}s",
                theme.font(16, "bold"), theme.TEXT)
    elif state.radio_state == SENDING:
        fraction = state.tx_sent / state.tx_total if state.tx_total else 0.0
        centred(draw, cy - 14, f"{int(fraction * 100)}%",
                theme.font(28, "bold"), theme.TEXT)
        centred(draw, cy + radius + 6,
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
        centred(draw, cy - 14, "HOLD", theme.font(24, "bold"), theme.TEXT_DIM)
        centred(draw, cy + 12, "to talk", theme.font(13), theme.TEXT_FAINT)

    centred(draw, 46, label, theme.font(14, "bold"), colour)


    # Level or progress bar under the disc, clear of the timer above it.
    bar = [24, 200, theme.SCREEN_WIDTH - 24, 210]
    if state.radio_state == RECORDING:
        vu_meter(draw, bar, state.record_level)
        remaining = state.max_record_seconds - state.record_seconds
        if remaining <= 5:
            centred(draw, 216, f"{remaining:.0f}s left",
                    theme.font(12, "bold"), theme.WARN)
    elif state.radio_state == SENDING:
        meter(draw, bar,
              state.tx_sent / state.tx_total if state.tx_total else 0.0, theme.WARN)
    else:
        detail = f"codec2 {state.codec_name}  ·  {state.frequency_mhz} MHz"
        colour = theme.TEXT_FAINT
        if state.entries and state.selected_index < len(state.entries):
            entry = state.entries[state.selected_index]
            if (state.link_states.get(entry.address) == "stale"
                    and not entry.is_broadcast):
                detail = f"connected  ·  last heard {entry.status}"
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
            }.get(link, ("not connected — calling…", theme.TEXT_FAINT))
        centred(draw, 202, detail, theme.font(12), colour)

    if state.queued:
        centred(draw, 228, f"{state.queued} queued", theme.font(11), theme.WARN)
    elif state.duty_fraction > 0.85:
        # Only when the hour's airtime is nearly spent. The old test fired
        # whenever duty_remaining happened to be zero, which is its
        # starting value -- so a fresh radio warned that it was out of
        # airtime before it had sent anything.
        centred(draw, 228, f"airtime {state.duty_fraction * 100:.0f}% used",
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

    row_height = INBOX_ROW
    visible = max(1, min(len(state.inbox), CONTENT_HEIGHT // row_height))
    first = max(0, min(state.inbox_index - visible // 2, len(state.inbox) - visible))
    first = max(0, first)

    for offset, item in enumerate(state.inbox[first:first + visible]):
        index = first + offset
        top = CONTENT_TOP + offset * row_height
        chosen = index == state.inbox_index
        panel(draw, [6, top, theme.SCREEN_WIDTH - 6, top + INBOX_PANEL],
              fill=theme.SURFACE_HI if chosen else theme.SURFACE,
              outline=theme.ACCENT if chosen else None)

        tint = theme.VOICE if item.kind == "voice" else theme.ACCENT
        draw.rectangle([6, top, 10, top + INBOX_PANEL], fill=tint)
        if not item.played and not item.outgoing:
            draw.ellipse([theme.SCREEN_WIDTH - 22, top + 7,
                          theme.SCREEN_WIDTH - 14, top + 15], fill=theme.OK)

        who = ("to " if item.outgoing else "") + (item.peer_name or f"node {item.src}")
        small = theme.font(10)
        when_width = int(draw.textlength(item.when, font=small)) + 8
        text_width = theme.SCREEN_WIDTH - 18 - MARGIN - when_width
        draw.text((18, top + INBOX_NAME_Y),
                  ellipsise(draw, who, theme.font(13, "bold"), text_width),
                  font=theme.font(13, "bold"), fill=theme.TEXT)
        draw.text((18, top + INBOX_DETAIL_Y),
                  ellipsise(draw, item.summary, theme.font(11), text_width),
                  font=theme.font(11),
                  fill=theme.WARN if item.incomplete else theme.TEXT_DIM)
        draw.text((theme.SCREEN_WIDTH - MARGIN - when_width + 8,
                   top + INBOX_DETAIL_Y + 1),
                  item.when, font=small, fill=theme.TEXT_FAINT)

    draw_footer(draw, state, _hints(INBOX))


# --- status ------------------------------------------------------------
def draw_status(draw, state: ViewState):
    draw_header(draw, state, "STATUS")

    stats = state.stats or {}
    power = state.battery_summary if state.battery_present else "external power"

    # Grouped, because eleven flat rows read as a wall. The group headings
    # cost a line each and make the screen scannable instead.
    groups = [
        ("STATION", [
            ("call", state.callsign or "-"),
            ("addr", str(state.address)),
            ("freq", f"{state.frequency_mhz} MHz  ·  codec2 {state.codec_name}"),
        ]),
        ("LINK", [
            ("peer", "connected" if state.target_linked else "not connected"),
            ("rssi", (f"{state.last_rssi} dBm  ·  "
                      f"{theme.SIGNAL_LABELS[theme.signal_level(state.last_rssi)]}")
                     if state.last_rssi is not None else "nothing heard yet"),
            ("duty", f"{state.duty_fraction * 100:.0f}% of the hour used"),
            ("pkts", f"tx {stats.get('packets_tx', 0)}   "
                     f"rx {stats.get('packets_rx', 0)}   "
                     f"lost {stats.get('frames_dropped', 0)}"),
        ]),
        ("HARDWARE", [
            ("mode", state.radio_note or "not checked"),
            ("audio", state.audio_note or ("ok" if state.audio_ok else "unavailable")),
            ("power", power),
        ]),
    ]

    label_font = theme.font(10)
    value_font = theme.font(11)
    head_font = theme.font(9, "bold")
    value_x = 58
    value_width = theme.SCREEN_WIDTH - value_x - MARGIN

    y = CONTENT_TOP
    for heading, rows in groups:
        if y + 12 > CONTENT_BOTTOM:
            break
        draw.text((MARGIN, y), heading, font=head_font, fill=theme.TEXT_FAINT)
        draw.line([MARGIN + 62, y + 4, theme.SCREEN_WIDTH - MARGIN, y + 4],
                  fill=theme.SURFACE_HI)
        y += 13
        for label, value in rows:
            if y + 13 > CONTENT_BOTTOM:
                break
            colour = theme.TEXT
            if label == "audio" and not state.audio_ok:
                colour = theme.DANGER
            elif label == "mode" and state.radio_deaf:
                colour = theme.DANGER
            elif label == "peer":
                colour = theme.OK if state.target_linked else theme.TEXT_DIM
            elif label == "power" and state.battery_low:
                colour = theme.DANGER
            draw.text((MARGIN + 4, y + 1), label, font=label_font,
                      fill=theme.TEXT_FAINT)
            draw.text((value_x, y), ellipsise(draw, value, value_font, value_width),
                      font=value_font, fill=colour)
            y += 14
        y += 4

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

    row_height = SETTING_ROW
    visible = max(1, min(len(state.settings_items),
                         (CONTENT_HEIGHT - 14) // row_height))
    first = max(0, min(state.settings_index - visible // 2,
                       len(state.settings_items) - visible))
    first = max(0, first)

    for offset, item in enumerate(state.settings_items[first:first + visible]):
        index = first + offset
        top = CONTENT_TOP + offset * row_height
        chosen = index == state.settings_index
        # Destructive entries are tinted so they are never opened by reflex.
        accent = theme.DANGER if item.get("destructive") else theme.ACCENT
        panel(draw, [6, top, theme.SCREEN_WIDTH - 6, top + SETTING_PANEL],
              fill=theme.SURFACE_HI if chosen else theme.SURFACE,
              outline=accent if chosen else None)

        text_width = theme.SCREEN_WIDTH - 16 - MARGIN - 8
        name_font = theme.font(14, "bold" if chosen else "regular")
        draw.text((16, top + SETTING_NAME_Y),
                  ellipsise(draw, item["label"], name_font, text_width),
                  font=name_font,
                  fill=(theme.DANGER if item.get("destructive")
                        else (theme.TEXT if chosen else theme.TEXT_DIM)))
        value = str(item.get("value", ""))
        if value:
            draw.text((16, top + SETTING_DETAIL_Y),
                      ellipsise(draw, value, theme.font(11), text_width),
                      font=theme.font(11), fill=theme.TEXT_FAINT)

    if len(state.settings_items) > visible:
        centred(draw, CONTENT_TOP + visible * row_height,
                f"{state.settings_index + 1} / {len(state.settings_items)}",
                theme.font(11), theme.TEXT_FAINT)

    draw_footer(draw, state, _hints(SETTINGS))


# --- pairing -----------------------------------------------------------
def draw_pair(draw, state: ViewState):
    """Radios heard pairing, and who this one is."""
    draw_header(draw, state, "PAIR")
    width = theme.SCREEN_WIDTH - 2 * MARGIN
    small, status_font = theme.font(11), theme.font(12, "bold")
    me = f"this radio: {state.callsign or '?'} · ID {state.address}"
    centred(draw, CONTENT_TOP, ellipsise(draw, me, small, width), small,
            theme.TEXT_DIM)
    status = state.pair_status or "looking for radios"
    centred(draw, CONTENT_TOP + 16, ellipsise(draw, status, status_font, width),
            status_font, theme.ACCENT)

    list_top = CONTENT_TOP + 38
    if not state.pair_found:
        for index, line in enumerate(("On the other radio, open",
                                      "Settings > Pair device too.")):
            centred(draw, list_top + 34 + index * 18, line, theme.font(12),
                    theme.TEXT_FAINT)
        draw_footer(draw, state, _hints(PAIR))
        return

    row_height = SETTING_ROW
    count = len(state.pair_found)
    selected = state.pair_index % count
    visible = max(1, min(count, (CONTENT_BOTTOM - list_top - 14) // row_height))
    first = max(0, min(selected - visible // 2, count - visible))

    text_width = theme.SCREEN_WIDTH - 16 - MARGIN - 8
    for offset, (addr, name, rssi, known) in enumerate(
            state.pair_found[first:first + visible]):
        top = list_top + offset * row_height
        chosen = first + offset == selected
        panel(draw, [6, top, theme.SCREEN_WIDTH - 6, top + SETTING_PANEL],
              fill=theme.SURFACE_HI if chosen else theme.SURFACE,
              outline=theme.ACCENT if chosen else None)
        name_font = theme.font(14, "bold" if chosen else "regular")
        draw.text((16, top + SETTING_NAME_Y),
                  ellipsise(draw, name, name_font, text_width), font=name_font,
                  fill=theme.TEXT if chosen else theme.TEXT_DIM)
        detail = [f"ID {addr}"]
        if rssi is not None:
            detail.append(theme.SIGNAL_LABELS[theme.signal_level(rssi)])
        if known:
            detail.append("already a contact")
        draw.text((16, top + SETTING_DETAIL_Y),
                  ellipsise(draw, "  ·  ".join(detail), small, text_width),
                  font=small, fill=theme.TEXT_FAINT)

    if count > visible:
        centred(draw, list_top + visible * row_height, f"{selected + 1} / {count}",
                small, theme.TEXT_FAINT)

    draw_footer(draw, state, _hints(PAIR))


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
    PAIR: draw_pair,
}


def render(display, state: ViewState):
    """Draw the current screen and push it if it changed."""
    image, draw = display.new_canvas()
    RENDERERS.get(state.screen, draw_contacts)(draw, state)
    return display.present(image)
