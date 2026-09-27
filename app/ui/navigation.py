"""One table describing what every gesture does on every screen.

This exists because the inbox screen printed "2 clicks back" in its
footer while the handler routed two clicks to *play*, which returned
silently when there was nothing to play. On an empty inbox -- the
normal state of a radio that has not received anything yet -- one click
was a guarded no-op, two clicks did nothing, and only three clicks went
back, which the screen never mentioned. The operator was stuck on a
screen that was telling them the wrong way out.

The footer and the dispatcher had been written separately, so they were
free to disagree. Here they cannot: `actions()` drives the handler and
`hints()` renders the labels, both from `SCREEN_ACTIONS`.

The invariant worth keeping is that **two clicks always means back**.
It held on Talk and Status, and the inbox was the one screen that broke
it -- which is exactly where a user gets lost. Play moved to three
clicks, where it now matches Talk's "three clicks replays the last
message": three clicks means play on every screen that has anything to
play.
"""

from __future__ import annotations

from app.input.button import DOUBLE, QUAD, SINGLE, TRIPLE
from app.ui.screens import (CONTACTS, EDIT, HOME, INBOX, PAIR, SETTINGS, START,
                            STATUS, TALK)

# Actions the app implements. Names, not callables, so this module stays
# free of app state and can be imported by the screens.
NEXT_ITEM = "next_item"          # Home and Start
OPEN_ITEM = "open_item"
NEXT_CONTACT = "next_contact"
OPEN_TALK = "open_talk"
OPEN_INBOX = "open_inbox"
OPEN_STATUS = "open_status"
NEXT_MESSAGE = "next_message"
PLAY_SELECTED = "play_selected"
REPLAY_LAST = "replay_last"
OPEN_SETTINGS = "open_settings"
NEXT_SETTING = "next_setting"
OPEN_SETTING = "open_setting"
NEXT_FOUND = "next_found"
PAIR_SELECTED = "pair_selected"
# Back to wherever this screen was opened from. One action rather than a
# "back to X" per screen, because Talk, Receive and Settings can each be
# reached from more than one place.
GO_BACK = "go_back"
EXIT_APP = "exit_app"

# Screens you pick from, rather than screens you are in. Two clicks opens
# a row here and leaves everywhere else.
MENU_SCREENS = (HOME, START, CONTACTS, SETTINGS, PAIR)

# Actions that move to a different screen. Used to check that no screen
# can strand the operator.
LEAVING_ACTIONS = {
    OPEN_ITEM, OPEN_TALK, OPEN_INBOX, OPEN_STATUS, OPEN_SETTINGS,
    GO_BACK, EXIT_APP,
}

# gesture -> (action, short label for the on-screen hint)
SCREEN_ACTIONS = {
    # The app opens here. There is nowhere to go back to, so three clicks
    # shows Status, as it did from the old contacts screen.
    HOME: {
        SINGLE: (NEXT_ITEM, "next"),
        DOUBLE: (OPEN_ITEM, "open"),
        TRIPLE: (OPEN_STATUS, "status"),
    },
    START: {
        SINGLE: (NEXT_ITEM, "next"),
        DOUBLE: (OPEN_ITEM, "open"),
        TRIPLE: (GO_BACK, "back"),
    },
    CONTACTS: {
        SINGLE: (NEXT_CONTACT, "next"),
        DOUBLE: (OPEN_TALK, "talk"),
        TRIPLE: (GO_BACK, "back"),
    },
    TALK: {
        SINGLE: (OPEN_INBOX, "receive"),
        DOUBLE: (GO_BACK, "back"),
        TRIPLE: (REPLAY_LAST, "replay"),
    },
    INBOX: {
        SINGLE: (NEXT_MESSAGE, "next"),
        DOUBLE: (GO_BACK, "back"),
        TRIPLE: (PLAY_SELECTED, "play"),
    },
    STATUS: {
        SINGLE: (GO_BACK, "back"),
        DOUBLE: (GO_BACK, "back"),
        TRIPLE: (OPEN_SETTINGS, "settings"),
    },
    SETTINGS: {
        SINGLE: (NEXT_SETTING, "next"),
        DOUBLE: (OPEN_SETTING, "open"),
        TRIPLE: (GO_BACK, "back"),
    },
    # A menu of the radios heard pairing: two clicks picks one.
    PAIR: {
        SINGLE: (NEXT_FOUND, "next"),
        DOUBLE: (PAIR_SELECTED, "pair"),
        TRIPLE: (GO_BACK, "back"),
    },
}

# An empty inbox has nothing to step through and nothing to play, so
# every click leaves rather than silently doing nothing.
EMPTY_INBOX_ACTIONS = {
    SINGLE: (GO_BACK, "back"),
    DOUBLE: (GO_BACK, "back"),
    TRIPLE: (GO_BACK, "back"),
}

# Four clicks exits from anywhere, and hold always talks. Neither is
# remapped per screen: the way out and the way to transmit must not
# depend on where you happen to be.
#
# Exiting closes the serial port, so the radio stops listening until the
# app is opened again. That is the deliberate trade: the app is a thing
# you open when you want it, not a service running behind the desktop.
GLOBAL_ACTIONS = {QUAD: (EXIT_APP, "exit")}


def actions(screen: str, inbox_empty: bool = False) -> dict:
    """The gesture -> (action, label) map in force for this screen.

    EDIT is absent on purpose: a modal editor routes gestures to itself
    rather than through this table, so asking for its actions is a bug.
    """
    if screen == EDIT:
        return dict(GLOBAL_ACTIONS)
    if screen == INBOX and inbox_empty:
        table = dict(EMPTY_INBOX_ACTIONS)
    else:
        table = dict(SCREEN_ACTIONS.get(screen, SCREEN_ACTIONS[HOME]))
    table.update(GLOBAL_ACTIONS)
    return table


def route(screen: str, gesture: str, inbox_empty: bool = False):
    """The action a gesture triggers here, or None if it does nothing."""
    entry = actions(screen, inbox_empty).get(gesture)
    return entry[0] if entry else None


_CLICK_WORD = {SINGLE: "1 click", DOUBLE: "2 clicks", TRIPLE: "3 clicks",
               QUAD: "4 clicks"}


def hints(screen: str, inbox_empty: bool = False) -> list:
    """Footer lines describing this screen's gestures.

    Generated from the same table the dispatcher uses, so the screen
    cannot advertise a gesture the app does not implement.
    """
    table = actions(screen, inbox_empty)
    parts = [
        f"{_CLICK_WORD[g]} {table[g][1]}"
        for g in (SINGLE, DOUBLE, TRIPLE) if g in table
    ]
    # Collapse "1 click back · 2 clicks back · 3 clicks back".
    labels = {table[g][1] for g in (SINGLE, DOUBLE, TRIPLE) if g in table}
    if len(labels) == 1:
        parts = [f"any click {labels.pop()}"]

    # The second line carries three items, so the trailing two are
    # abbreviated. Spelled out ("hold to talk · 4 clicks exit") it runs to
    # 272 px against a 236 px panel and is clipped at both ends -- which
    # is exactly how it shipped until someone looked at a screenshot.
    # Read the global label from the table rather than repeating it: a
    # hardcoded "4 exit" survived four clicks being changed to hide the
    # app, which is exactly the drift this module exists to prevent.
    global_label = table[QUAD][1] if QUAD in table else "hide"
    first = "  ·  ".join(parts[:2])
    second = "  ·  ".join(parts[2:] + ["hold talk", f"4 {global_label}"])
    return [first, second]
