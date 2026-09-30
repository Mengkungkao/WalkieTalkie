"""One table describing what every action does on every screen.

This exists because the inbox screen once printed "2 clicks back" in its
footer while the handler routed two clicks to *play*, which returned
silently when there was nothing to play. The footer and the dispatcher
had been written separately, so they were free to disagree. Here they
cannot: `route()` drives the handler and `hints()` renders the footer,
both from `SCREEN_ACTIONS`.

The actions are MFruit OS's (mfruit_sdk.input), the same in every MFruit
app, so nothing is relearned between apps:

    next      tap              Down / Right / Tab
    previous  2 clicks         Up / Left
    select    hold, release    Enter
    extra     3 clicks         (a letter; see CHAR_ACTIONS)
    back      4 clicks         Esc

Menus and lists follow it exactly: tap next, 2 clicks previous, hold
opens, 4 clicks back. **Talk screens** (`TALK_SCREENS`) are where holding
the button -- or Space -- talks, so there three clicks opens the selected
row instead. A selected Back row always uses hold and release, even on
a talk screen. Back from Home leaves the app.
"""

from __future__ import annotations

from mfruit_sdk.input import BACK, EXTRA, NEXT, PREVIOUS, SELECT

from app.ui.screens import (CONTACTS, EDIT, HOME, INBOX, PAIR, RANGE, SETTINGS,
                            START, STATUS, TALK)

# Actions the app implements. Names, not callables, so this module stays
# free of app state and can be imported by the screens.
NEXT_ITEM = "next_item"          # Home and Start
PREVIOUS_ITEM = "previous_item"
OPEN_ITEM = "open_item"
NEXT_CONTACT = "next_contact"
PREVIOUS_CONTACT = "previous_contact"
OPEN_TALK = "open_talk"
OPEN_INBOX = "open_inbox"
OPEN_STATUS = "open_status"
NEXT_MESSAGE = "next_message"
PREVIOUS_MESSAGE = "previous_message"
PLAY_SELECTED = "play_selected"
REPLAY_LAST = "replay_last"
OPEN_SETTINGS = "open_settings"
NEXT_SETTING = "next_setting"
PREVIOUS_SETTING = "previous_setting"
OPEN_SETTING = "open_setting"
NEXT_FOUND = "next_found"
PREVIOUS_FOUND = "previous_found"
PAIR_SELECTED = "pair_selected"
MARK_SPOT = "mark_spot"          # range test: note where you are, in the log
PROBE_NOW = "probe_now"          # range test: don't wait for the timer
# Back to wherever this screen was opened from. One action rather than a
# "back to X" per screen, because Talk, Receive and Settings can each be
# reached from more than one place.
GO_BACK = "go_back"
EXIT_APP = "exit_app"

# Screens you pick from, rather than screens you are in.
MENU_SCREENS = (HOME, START, CONTACTS, SETTINGS, PAIR)

# Actions that move to a different screen. Used to check that no screen
# can strand the operator.
LEAVING_ACTIONS = {
    OPEN_ITEM, OPEN_TALK, OPEN_INBOX, OPEN_STATUS, OPEN_SETTINGS,
    GO_BACK, EXIT_APP,
}

# action -> (what it does here, short label for the footer)
SCREEN_ACTIONS = {
    # The app opens here; back leaves the app.
    HOME: {
        NEXT: (NEXT_ITEM, "next"),
        PREVIOUS: (PREVIOUS_ITEM, "previous"),
        SELECT: (OPEN_ITEM, "open"),
        BACK: (EXIT_APP, "exit"),
    },
    START: {
        NEXT: (NEXT_ITEM, "next"),
        PREVIOUS: (PREVIOUS_ITEM, "previous"),
        SELECT: (OPEN_ITEM, "open"),
        EXTRA: (OPEN_ITEM, "open"),
        BACK: (GO_BACK, "back"),
    },
    CONTACTS: {
        NEXT: (NEXT_CONTACT, "next"),
        PREVIOUS: (PREVIOUS_CONTACT, "previous"),
        SELECT: (OPEN_TALK, "talk to"),
        EXTRA: (OPEN_TALK, "talk to"),
        BACK: (GO_BACK, "back"),
    },
    TALK: {
        NEXT: (OPEN_INBOX, "receive"),
        SELECT: (OPEN_INBOX, "receive"),
        EXTRA: (REPLAY_LAST, "replay"),
        BACK: (GO_BACK, "back"),
    },
    INBOX: {
        NEXT: (NEXT_MESSAGE, "next"),
        PREVIOUS: (PREVIOUS_MESSAGE, "previous"),
        SELECT: (PLAY_SELECTED, "play"),
        EXTRA: (PLAY_SELECTED, "play"),
        BACK: (GO_BACK, "back"),
    },
    STATUS: {
        NEXT: (NEXT_ITEM, "next"),
        PREVIOUS: (PREVIOUS_ITEM, "previous"),
        SELECT: (GO_BACK, "back"),
        BACK: (GO_BACK, "back"),
    },
    SETTINGS: {
        NEXT: (NEXT_SETTING, "next"),
        PREVIOUS: (PREVIOUS_SETTING, "previous"),
        SELECT: (OPEN_SETTING, "open"),
        BACK: (GO_BACK, "back"),
    },
    # A menu of the radios heard pairing: hold pairs with one.
    PAIR: {
        NEXT: (NEXT_FOUND, "next"),
        PREVIOUS: (PREVIOUS_FOUND, "previous"),
        SELECT: (PAIR_SELECTED, "pair"),
        BACK: (GO_BACK, "back"),
    },
    # Carried on a walk: one click marks the spot, the easiest gesture to
    # make with the radio in a pocket. Back ends the test.
    RANGE: {
        NEXT: (MARK_SPOT, "mark"),
        SELECT: (MARK_SPOT, "mark"),
        EXTRA: (PROBE_NOW, "probe"),
        BACK: (GO_BACK, "stop"),
    },
}

# Keyboard shortcuts, including Status without a multi-click gesture.
CHAR_ACTIONS = {
    HOME: {"s": OPEN_STATUS},
    TALK: {"r": REPLAY_LAST},
    INBOX: {"p": PLAY_SELECTED},
    RANGE: {"p": PROBE_NOW, "m": MARK_SPOT},
}

# Where holding the button (or Space) talks: inside Start -- choosing who
# to talk to, and talking -- and on the range test, to the radio under
# test, so voice can be tried at each spot. Everywhere else a hold opens
# the selected row. Menus are for choosing, and a hold that transmitted
# while you were looking for a setting went out to whoever was last
# chosen, unasked. Receive is for listening back to what came in.
TALK_SCREENS = frozenset({START, CONTACTS, TALK, RANGE})


def can_talk(screen: str, back_selected: bool = False) -> bool:
    return screen in TALK_SCREENS and not back_selected


def actions(screen: str, inbox_empty: bool = False,
            back_selected: bool = False) -> dict:
    """The action -> (what it does, label) map in force for this screen.

    EDIT is absent on purpose: a modal editor routes input to itself
    rather than through this table, so it has no actions here.
    """
    if screen == EDIT:
        return {}
    table = dict(SCREEN_ACTIONS.get(screen, SCREEN_ACTIONS[HOME]))
    if back_selected or (screen == INBOX and inbox_empty):
        # Back is an ordinary selectable row. Taps only move through the
        # list, including an empty inbox whose sole row is Back. A partial
        # four-click exit must never select this row or start playback.
        table[SELECT] = (EXIT_APP, "exit") if screen == HOME else (GO_BACK, "back")
        table.pop(EXTRA, None)
    return table


def route(screen: str, action: str, inbox_empty: bool = False,
          back_selected: bool = False):
    """What an input action does here, or None if it does nothing."""
    entry = actions(screen, inbox_empty, back_selected).get(action)
    return entry[0] if entry else None


def route_char(screen: str, char: str):
    """What a typed letter does here, or None."""
    return CHAR_ACTIONS.get(screen, {}).get(char.lower())


_GESTURE = {NEXT: "tap", PREVIOUS: "2×", SELECT: "hold", EXTRA: "3×", BACK: "4×"}


def hints(screen: str, inbox_empty: bool = False, armed: bool = False,
          back_selected: bool = False) -> list:
    """Footer hints, [(gesture, label)], from the same table as `route`.

    Most important first, because the footer drops what does not fit
    from the end: on a talk screen "hold talk" leads; the way back is
    always within the first three.
    """
    table = actions(screen, inbox_empty, back_selected)
    if armed and SELECT in table:
        return [("release", f"to {table[SELECT][1]}")]
    talk = can_talk(screen, back_selected)
    order = [SELECT, NEXT, EXTRA, BACK, PREVIOUS] if talk else [NEXT, SELECT, BACK, EXTRA, PREVIOUS]
    shown, labels = [], set()
    for action in order:
        if action == SELECT and talk:
            shown.append(("hold", "talk"))
            continue
        if action not in table:
            continue
        label = table[action][1]
        if talk and action == NEXT and label == "next":
            continue                     # plain list stepping, as everywhere
        if label in labels and action != BACK:
            continue
        labels.add(label)
        shown.append((_GESTURE[action], label))
    back = [hint for hint in shown if hint[0] == "4×"]
    if back and shown.index(back[0]) > 2:
        shown.remove(back[0])
        shown.insert(2, back[0])
    return shown
