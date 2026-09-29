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

from mfruit_sdk.status import Status
from mfruit_sdk.ui import Canvas, Row, draw_list, footer, status_bar, toast
from mfruit_sdk.ui import theme as mfruit_layout

from app.ui import theme
from app.ui.widgets import (centred, ellipsise, meter, panel, signal_bars,
                            two_line_row, vu_meter)

CONTACTS = "contacts"
TALK = "talk"
INBOX = "inbox"
STATUS = "status"
SETTINGS = "settings"
PAIR = "pair"
# The menu the app opens on, and the one that picks who to talk to.
HOME = "home"
START = "start"
# Home > Range test: probes a paired radio and logs the answers.
RANGE = "range"
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

# MFruit OS's layout: status bar (page name, WiFi, battery) at the top,
# gesture hints at the bottom.
FOOTER_Y = mfruit_layout.FOOTER_Y
HEADER_HEIGHT = mfruit_layout.CONTENT_TOP - 6

# The usable band between status bar and footer. Everything draws inside
# it: the status screen used to run 25 px past the footer and print its
# last three rows straight through the gesture hints.
CONTENT_TOP = mfruit_layout.CONTENT_TOP
CONTENT_BOTTOM = mfruit_layout.CONTENT_BOTTOM
CONTENT_HEIGHT = CONTENT_BOTTOM - CONTENT_TOP

# Room in the status bar for the LoRa signal meter, left of WiFi/battery.
SIGNAL_SLOT = 20

# Editor titles are identifiers in app.main; this is how they read on screen.
# Page names share the status bar with the signal meter, WiFi and battery,
# so they stay short (tests/test_screens.py checks every one fits).
EDITOR_TITLES = {
    "DEVICE ID": "Device ID", "NAME": "Name", "CHANNEL": "Channel", "VOICE": "Voice",
    "BASE STATION": "Base", "DATE & TIME": "Clock", "RESET": "Reset",
    "PAIRING": "Pairing",
}
PAGE_TITLES = {HOME: "Walkie", START: "Start", CONTACTS: "Paired", INBOX: "Receive",
               STATUS: "Status", SETTINGS: "Settings", PAIR: "Pair", RANGE: "Range"}

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

    screen: str = HOME
    radio_state: str = IDLE
    callsign: str = ""
    address: int = 0
    channel: int = 1
    frequency_mhz: int = 868

    home_items: list = field(default_factory=list)
    home_index: int = 0
    start_items: list = field(default_factory=list)
    start_index: int = 0

    # Paired devices, and which of them have no keys (legacy contacts).
    entries: list = field(default_factory=list)
    selected_index: int = 0
    unpaired: set = field(default_factory=set)

    # Who holding the button talks to.
    target_name: str = ""
    target_address: int = 0xFFFF
    target_heard: str = ""

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
    pair_channels: dict = field(default_factory=dict)

    link_states: dict = field(default_factory=dict)
    target_linked: bool = False
    # From the link check: addr -> (state, "in range · -85/-91 dBm").
    link_status: dict = field(default_factory=dict)
    # Home > Range test, while it runs (see app.rangetest).
    range_view: dict = field(default_factory=dict)

    battery_present: bool = False
    battery_summary: str = ""
    battery_detail: str = ""
    battery_percent: float | None = None
    battery_low: bool = False
    battery_charging: bool = False
    wifi_level: int | None = None

    # A hold passed the threshold on a menu: the footer says what release does.
    armed: bool = False

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

# The link check's states (app.radio.linkcheck), as a dot: colour, filled.
RANGE_DOT = {
    "in range": (theme.OK, True),
    "weak signal": (theme.WARN, True),
    "disconnected": (theme.DANGER, False),
    "not checked yet": (theme.TEXT_FAINT, False),
    "keys changed": (theme.DANGER, True),
}
RANGE_COLOUR = {state: colour for state, (colour, _filled) in RANGE_DOT.items()}
STATE_LABEL = {
    IDLE: "READY", RECORDING: "RECORDING", SENDING: "SENDING",
    RECEIVING: "RECEIVING", PLAYING: "PLAYING",
}


# --- chrome ------------------------------------------------------------
def _status(state: ViewState) -> Status:
    battery = None
    if state.battery_present and state.battery_percent is not None:
        battery = int(round(state.battery_percent))
    return Status(state.wifi_level, battery, state.battery_charging)


def draw_header(draw, state: ViewState, title: str):
    """MFruit OS's status bar: page name, then LoRa signal, WiFi, battery.

    The signal meter answers "how far can I reach"; it sits in a slot
    the status bar keeps free for it.
    """
    canvas = Canvas.over(draw, theme.MFRUIT)
    slot = status_bar(canvas, title, _status(state), reserve=SIGNAL_SLOT)
    signal_bars(draw, slot, mfruit_layout.STATUS_Y + 2, state.last_rssi)


def draw_footer(draw, state: ViewState, hints: list):
    """Gesture hints, from the same table the app dispatches on; a banner
    (a flash message) shows above them."""
    canvas = Canvas.over(draw, theme.MFRUIT)
    if state.armed:
        from app.ui import navigation

        hints = navigation.hints(state.screen, inbox_empty=not state.inbox,
                                 armed=True) or hints
    footer(canvas, hints)
    banner = state.active_banner
    if banner:
        toast(canvas, banner)


# --- contacts ----------------------------------------------------------
def draw_contacts(draw, state: ViewState):
    draw_header(draw, state, PAGE_TITLES[CONTACTS])

    if not state.entries:
        centred(draw, 110, "no paired radios yet", theme.font(15), theme.TEXT_DIM)
        centred(draw, 134, "Home > Pair devices, on", theme.font(12), theme.TEXT_FAINT)
        centred(draw, 152, "both radios", theme.font(12), theme.TEXT_FAINT)
        draw_footer(draw, state, _hints(CONTACTS))
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
              fill=theme.SELECTED if chosen else theme.SURFACE)

        # Hearing a station does not prove it hears you, and a one-way
        # link is the classic radio failure -- you talk for a minute
        # before finding out nobody received a word. So the dot means
        # "completed a handshake", and presence alone is only a ring.
        link = state.link_states.get(entry.address)
        reach, reach_detail = state.link_status.get(entry.address, ("", ""))
        if entry.is_broadcast:
            dot, filled = theme.ACCENT, True
        elif reach:
            # The link check is live, so it outranks the handshake: a radio
            # that paired an hour ago and is now out of range is not green.
            dot, filled = RANGE_DOT.get(reach, (theme.TEXT_FAINT, False))
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
        if entry.is_broadcast:
            detail = entry.status
        elif entry.address in state.unpaired:
            detail = f"{entry.address} · not paired: pair again"
        elif reach_detail:
            detail = f"{entry.address} · {reach_detail}"
        else:
            detail = f"{entry.address} · {entry.status}"
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
    draw_header(draw, state, state.target_name or "All stations")

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
        detail = f"channel {state.channel}  ·  encrypted"
        colour = theme.TEXT_FAINT
        link = state.link_states.get(state.target_address)
        reach, reach_detail = state.link_status.get(state.target_address, ("", ""))
        if link == "stale" and state.target_address != 0xFFFF:
            detail = f"connected  ·  last heard {state.target_heard}"
        use_reach = (reach and reach != "not checked yet"
                     and state.target_address != 0xFFFF)
        if use_reach:
            # Whether they will hear this, before a word is spent on it.
            detail, colour = reach_detail, RANGE_COLOUR.get(reach, theme.TEXT_FAINT)
        if state.radio_deaf:
            # Worth shouting about: everything else looks like it works.
            detail = "RADIO DEAF — check M0/M1 jumpers"
            colour = theme.DANGER
        elif not state.audio_ok:
            detail = state.audio_note or "no audio device"
            colour = theme.DANGER
        elif not state.target_linked and not use_reach:
            # Shown here rather than beside the disc, where it collided
            # with the ring at this font size.
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
    # How many are new shows on the rows (green dots) and on Home.
    draw_header(draw, state, PAGE_TITLES[INBOX])

    if not state.inbox:
        centred(draw, 120, "nothing received yet", theme.font(14), theme.TEXT_DIM)
        centred(draw, 142, f"listening on channel {state.channel}",
                theme.font(12), theme.TEXT_FAINT)
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
              fill=theme.SELECTED if chosen else theme.SURFACE)

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
    draw_header(draw, state, PAGE_TITLES[STATUS])

    stats = state.stats or {}
    power = state.battery_summary if state.battery_present else "external power"

    # Grouped, because eleven flat rows read as a wall. The group headings
    # cost a line each and make the screen scannable instead.
    groups = [
        ("STATION", [
            ("name", state.callsign or "-"),
            ("id", f"{state.address}  ·  channel {state.channel}  ·  encrypted"),
            ("freq", f"{state.frequency_mhz} MHz  ·  air {stats.get('air', '?')}"
                     f"  ·  codec2 {state.codec_name}"),
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


# --- menus -------------------------------------------------------------
def draw_menu(draw, state: ViewState, screen: str, title: str, items: list,
              selected: int, top_offset: int = 0):
    """A list of rows to pick from: Home, Start and Settings, as MFruit OS draws lists."""
    draw_header(draw, state, title)
    rows = [Row(item["label"], subtitle=str(item.get("value", "")) or None,
                kind="danger" if item.get("destructive") else "action")
            for item in items]
    draw_list(Canvas.over(draw, theme.MFRUIT), rows, selected % len(rows) if rows else 0,
              top=CONTENT_TOP + top_offset, empty="Nothing here")
    draw_footer(draw, state, _hints(screen))


def draw_home(draw, state: ViewState):
    small = theme.font(11)
    me = f"{state.callsign or 'this radio'}  ·  ID {state.address}  ·  ch {state.channel}"
    draw_menu(draw, state, HOME, PAGE_TITLES[HOME], state.home_items, state.home_index,
              top_offset=18)
    centred(draw, CONTENT_TOP, ellipsise(draw, me, small, theme.SCREEN_WIDTH - 2 * MARGIN),
            small, theme.TEXT_DIM)


def draw_start(draw, state: ViewState):
    draw_menu(draw, state, START, PAGE_TITLES[START], state.start_items, state.start_index)


def draw_settings(draw, state: ViewState):
    draw_menu(draw, state, SETTINGS, PAGE_TITLES[SETTINGS], state.settings_items,
              state.settings_index)


# --- pairing -----------------------------------------------------------
def draw_pair(draw, state: ViewState):
    """Radios heard pairing, and who this one is."""
    draw_header(draw, state, PAGE_TITLES[PAIR])
    width = theme.SCREEN_WIDTH - 2 * MARGIN
    small, status_font = theme.font(11), theme.font(12, "bold")
    me = f"this radio: {state.callsign or '?'} · ID {state.address} · ch {state.channel}"
    centred(draw, CONTENT_TOP, ellipsise(draw, me, small, width), small,
            theme.TEXT_DIM)
    status = state.pair_status or "looking for radios"
    centred(draw, CONTENT_TOP + 16, ellipsise(draw, status, status_font, width),
            status_font, theme.ACCENT)

    list_top = CONTENT_TOP + 38
    if not state.pair_found:
        for index, line in enumerate(("On the other radio, open",
                                      "Home > Pair devices too.")):
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
              fill=theme.SELECTED if chosen else theme.SURFACE)
        name_font = theme.font(14, "bold" if chosen else "regular")
        draw.text((16, top + SETTING_NAME_Y),
                  ellipsise(draw, name, name_font, text_width), font=name_font,
                  fill=theme.TEXT if chosen else theme.TEXT_DIM)
        detail = [f"ID {addr}"]
        channel = state.pair_channels.get(addr)
        if channel is not None and channel != state.channel:
            detail.append(f"ch {channel}")
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


# --- range test ----------------------------------------------------------
def _success_colour(success):
    if success is None:
        return theme.TEXT_DIM
    return theme.OK if success >= 0.8 else theme.WARN if success >= 0.4 else theme.DANGER


def _duration(seconds: float) -> str:
    seconds = int(seconds)
    return f"{seconds // 3600}:{seconds // 60 % 60:02d}:{seconds % 60:02d}" \
        if seconds >= 3600 else f"{seconds // 60}:{seconds % 60:02d}"


def draw_range(draw, state: ViewState):
    """Home > Range test: how the link is doing, readable at arm's length."""
    draw_header(draw, state, PAGE_TITLES[RANGE])
    view = state.range_view or {}
    small, tiny = theme.font(11), theme.font(10)
    width = theme.SCREEN_WIDTH - 2 * MARGIN
    if not view:
        centred(draw, 110, "no paired radio to test", theme.font(14), theme.TEXT_DIM)
        centred(draw, 132, "Home > Pair devices first", theme.font(12), theme.TEXT_FAINT)
        draw_footer(draw, state, _hints(RANGE))
        return

    top = f"to {view.get('name', '?')}  ·  every {view.get('interval', 0):.0f}s" \
          f"  ·  air {view.get('air', '?')}"
    centred(draw, CONTENT_TOP, ellipsise(draw, top, small, width), small, theme.TEXT_DIM)

    success = view.get("success")
    centred(draw, CONTENT_TOP + 16, "--" if success is None else f"{success * 100:.0f}%",
            theme.font(36, "bold"), _success_colour(success))
    window = view.get("window", (0, 0))
    centred(draw, CONTENT_TOP + 60,
            f"{window[0]} of the last {window[1]} answered" if window[1]
            else "waiting for the first answer", small, theme.TEXT_FAINT)

    # How we hear them, and how they hear us: one-way links are the ones
    # that fool you, so both are shown side by side.
    box_top = CONTENT_TOP + 80
    half = theme.SCREEN_WIDTH // 2
    for index, (label, rssi) in enumerate((("heard here", view.get("down")),
                                           ("heard there", view.get("up")))):
        left = 6 if index == 0 else half + 3
        right = half - 3 if index == 0 else theme.SCREEN_WIDTH - 6
        panel(draw, [left, box_top, right, box_top + 52], fill=theme.SURFACE)
        draw.text((left + 8, box_top + 5), label, font=tiny, fill=theme.TEXT_FAINT)
        value = "--" if rssi is None else f"{rssi}"
        draw.text((left + 8, box_top + 20), value, font=theme.font(20, "bold"),
                  fill=theme.rssi_colour(rssi) if rssi is not None else theme.TEXT_DIM)
        draw.text((left + 8 + int(draw.textlength(value, font=theme.font(20, "bold"))) + 4,
                   box_top + 29), "dBm", font=tiny, fill=theme.TEXT_FAINT)
        signal_bars(draw, right - 28, box_top + 6, rssi, height=10)

    line_y = box_top + 60
    if state.radio_state == RECORDING:
        result, colour = f"recording  {state.record_seconds:.1f}s", theme.DANGER
    elif state.radio_state == SENDING:
        result, colour = f"sending voice  {state.tx_sent}/{state.tx_total}", theme.WARN
    elif state.radio_state == PLAYING:
        result, colour = "playing", theme.VOICE
    else:
        result = view.get("last_result", "")
        colour = (theme.OK if result.startswith("answered") else
                  theme.DANGER if result.startswith("no answer") else
                  theme.WARN if result.startswith("skipped") else theme.TEXT_DIM)
    centred(draw, line_y, ellipsise(draw, result, theme.font(13, "bold"), width),
            theme.font(13, "bold"), colour)

    counts = (f"{view.get('answered', 0)}/{view.get('sent', 0)} answered"
              f" · {view.get('marks', 0)} marks · {_duration(view.get('elapsed', 0))}")
    centred(draw, line_y + 20, ellipsise(draw, counts, small, width), small, theme.TEXT_DIM)
    if view.get("log"):
        centred(draw, line_y + 36, ellipsise(draw, view["log"], tiny, width), tiny,
                theme.TEXT_FAINT)
    draw_footer(draw, state, _hints(RANGE))


def draw_editor(draw, state: ViewState):
    """A modal value editor: one big value, and what the clicks do to it."""
    editor = state.editor
    title = state.editor_title or "Edit"
    draw_header(draw, state, EDITOR_TITLES.get(title, title.capitalize()))
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

    draw_footer(draw, state, editor_hints(editor))


def editor_hints(editor) -> list:
    """What the button does in an editor (a keyboard types, Enter saves, Esc cancels)."""
    if hasattr(editor, "prompt"):
        # "4× back", as in MFruit OS's own dialogs: "cancel" does not fit here.
        return [("tap", "change"), ("hold", "confirm"), ("4×", "back")]
    last = (not hasattr(editor, "digits")
            or getattr(editor, "cursor", 0) >= editor.digits - 1)
    if hasattr(editor, "field"):
        last = editor.field >= len(editor.FIELDS) - 1
    return [("tap", "change"), ("hold", "save" if last else "next"), ("4×", "cancel")]


RENDERERS = {
    CONTACTS: draw_contacts, TALK: draw_talk, INBOX: draw_inbox,
    STATUS: draw_status, SETTINGS: draw_settings, EDIT: draw_editor,
    PAIR: draw_pair, HOME: draw_home, START: draw_start, RANGE: draw_range,
}


def render(display, state: ViewState):
    """Draw the current screen and push it if it changed."""
    image, draw = display.new_canvas()
    RENDERERS.get(state.screen, draw_home)(draw, state)
    return display.present(image)
