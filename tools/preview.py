#!/usr/bin/env python3
"""Render every screen to PNG without a Pi, from sample data.

    python3 tools/preview.py                     # -> /tmp/walkie-preview
    python3 tools/preview.py --out DIR

Writes one PNG per screen and state, plus all-screens.png, a contact sheet.
On a machine without MFruit OS installed, set
MFRUIT_FONT_DIR=~/MFruitOS/assets/fonts to preview with MFruit OS's font.
"""

from __future__ import annotations

import argparse
import datetime
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PIL import Image, ImageDraw  # noqa: E402

from app.store.inbox import Item  # noqa: E402
from app.store.roster import Entry  # noqa: E402
from app.ui import screens, theme  # noqa: E402
from app.ui.editors import ClockEditor, ConfirmEditor, DigitEditor  # noqa: E402
from app.ui.screens import (CONTACTS, EDIT, HOME, INBOX, PAIR, RANGE, RECORDING,  # noqa: E402
                            SENDING, SETTINGS, START, STATUS, TALK, ViewState)


def sample(**overrides) -> ViewState:
    state = ViewState(
        callsign="Rover", address=5, frequency_mhz=868, channel=3,
        home_items=[
            {"key": "start", "label": "Start", "value": "now talking to Base"},
            {"key": "receive", "label": "Receive", "value": "2 new  ·  14 in all"},
            {"key": "pair", "label": "Pair devices", "value": "3 paired  ·  Base in range"},
            {"key": "settings", "label": "Settings", "value": "name, ID, privacy channel"},
            {"key": "status", "label": "Status", "value": "radio, signal, audio and power"},
            {"key": "range", "label": "Range test", "value": "probe a paired radio"},
            {"key": "back", "label": "Back to MFruit OS"},
        ],
        start_items=[
            {"key": "all", "label": "To ALL", "value": "every paired radio on channel 3"},
            {"key": "device", "label": "To a paired device", "value": "3 paired"},
            {"key": "back", "label": "Back"},
        ],
        settings_items=[
            {"key": "name", "label": "Name", "value": "Rover  ·  what other radios see"},
            {"key": "device_id", "label": "Device ID", "value": "5  ·  unique to this radio"},
            {"key": "channel", "label": "Privacy channel", "value": "3  ·  others are ignored"},
            {"key": "reset", "label": "Reset all data", "value": "3 message(s), keys",
             "destructive": True},
            {"key": "back", "label": "Back"},
        ],
        entries=[
            Entry("Base", 1, True, last_heard=1.0, last_rssi=-72),
            Entry("Hilltop", 9, False, last_heard=1.0, last_rssi=-104),
            Entry("Ridge", 12, True, last_heard=1.0, last_rssi=-92),
        ],
        inbox=[
            Item("a", "voice", 1, "Base", 1.0, -80, duration=4.2),
            Item("b", "text", 9, "Hilltop", 1.0, -104, text="on my way back to the car"),
            Item("c", "voice", 1, "Base", 1.0, None, duration=9.9, played=True),
        ],
        unread=2, target_name="Base", target_address=1, last_rssi=-88,
        duty_fraction=0.42, codec_name="700C", battery_present=True,
        battery_percent=76.0, battery_summary="76%  ·  about 5 h", wifi_level=3,
        stats={"packets_tx": 12, "packets_rx": 34, "frames_dropped": 2, "air": "2.4k"},
        pair_found=[(1234, "jarvis", -72, False), (8, "hilltop", None, True)],
        pair_status="looking for radios",
        range_view={"name": "Base", "interval": 30, "air": "2.4k", "success": 0.83,
                    "window": (5, 6), "down": -84, "up": -91, "answered": 10, "sent": 12,
                    "marks": 2, "elapsed": 754, "last_result": "answered in 1.2 s"},
    )
    for key, value in overrides.items():
        setattr(state, key, value)
    return state


SHOTS = [
    ("1-home", dict(screen=HOME)),
    ("2-home-armed", dict(screen=HOME, home_index=1, armed=True)),
    ("3-start", dict(screen=START)),
    ("4-contacts", dict(screen=CONTACTS, selected_index=1)),
    ("5-talk", dict(screen=TALK)),
    ("6-talk-recording", dict(screen=TALK, radio_state=RECORDING, record_level=0.6,
                              record_seconds=3.4)),
    ("7-talk-sending", dict(screen=TALK, radio_state=SENDING, tx_sent=3, tx_total=8)),
    ("8-inbox", dict(screen=INBOX, inbox_index=1)),
    ("9-status", dict(screen=STATUS)),
    ("10-settings", dict(screen=SETTINGS, settings_index=3)),
    ("11-edit-id", dict(screen=EDIT, editor=DigitEditor(5, digits=5),
                        editor_title="DEVICE ID", editor_hint="must be unique on the channel")),
    ("12-edit-clock", dict(screen=EDIT, editor=ClockEditor(datetime.datetime(2026, 9, 29, 14, 30)),
                           editor_title="DATE & TIME")),
    ("13-confirm", dict(screen=EDIT, editor=ConfirmEditor("Erase everything?",
                                                          "messages, voice clips, paired\nradios, keys and settings"),
                        editor_title="RESET")),
    ("14-pair", dict(screen=PAIR)),
    ("15-range", dict(screen=RANGE)),
    ("16-home-status", dict(screen=HOME, home_index=4)),
    ("17-home-back", dict(screen=HOME, home_index=6)),
    ("18-start-back", dict(screen=START, start_index=2)),
    ("19-contacts-back", dict(screen=CONTACTS, contacts_back=True)),
    ("20-inbox-back", dict(screen=INBOX, inbox_back=True)),
    ("21-pair-back", dict(screen=PAIR, pair_back=True)),
    ("22-contacts-empty", dict(screen=CONTACTS, entries=[], contacts_back=True)),
    ("23-inbox-empty", dict(screen=INBOX, inbox=[], inbox_back=True, unread=0)),
    ("24-pair-empty", dict(screen=PAIR, pair_found=[], pair_back=True)),
    ("25-settings-back", dict(screen=SETTINGS, settings_index=4)),
]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--out", default="/tmp/walkie-preview")
    args = parser.parse_args()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    tiles = []
    for name, overrides in SHOTS:
        state = sample(**overrides)
        image = Image.new("RGB", (theme.SCREEN_WIDTH, theme.SCREEN_HEIGHT), theme.BG)
        screens.RENDERERS[state.screen](ImageDraw.Draw(image), state)
        image.save(out / f"{name}.png")
        tiles.append(image)
        print(f"  wrote {out / name}.png")

    columns = 5
    rows = (len(tiles) + columns - 1) // columns
    sheet = Image.new("RGB", (8 + columns * (theme.SCREEN_WIDTH + 8),
                              8 + rows * (theme.SCREEN_HEIGHT + 8)), (24, 26, 34))
    for index, tile in enumerate(tiles):
        row, column = divmod(index, columns)
        sheet.paste(tile, (8 + column * (theme.SCREEN_WIDTH + 8),
                           8 + row * (theme.SCREEN_HEIGHT + 8)))
    sheet.save(out / "all-screens.png")
    print(f"  wrote {out / 'all-screens.png'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
