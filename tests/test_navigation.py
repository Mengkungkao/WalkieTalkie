"""Gesture routing -- and the dead end that prompted it.

The inbox screen used to print "2 clicks back" while routing two clicks
to *play*, which returned silently when there was nothing to play. On an
empty inbox that left one click a guarded no-op, two clicks doing
nothing, and only three clicks going back -- a way out the screen never
mentioned. These tests pin the invariants that prevent it recurring.
"""

from __future__ import annotations

import pytest

from app.input.button import DOUBLE, QUAD, SINGLE, TRIPLE
from app.ui import navigation as nav
from app.ui.screens import CONTACTS, EDIT, INBOX, SETTINGS, STATUS, TALK

ALL_SCREENS = (CONTACTS, TALK, INBOX, STATUS, SETTINGS)
CLICKS = (SINGLE, DOUBLE, TRIPLE)


@pytest.mark.parametrize("screen", (TALK, INBOX, STATUS))
def test_two_clicks_leaves_a_view(screen):
    """Views are places you are already in, so two clicks gets you out."""
    assert nav.route(screen, DOUBLE) in (nav.BACK_TALK, nav.BACK_CONTACTS)


@pytest.mark.parametrize("screen", nav.MENU_SCREENS)
def test_two_clicks_opens_a_row_in_a_menu(screen):
    """Menus are lists you pick from, so two clicks goes in, not out."""
    assert nav.route(screen, DOUBLE) in (nav.OPEN_TALK, nav.OPEN_SETTING)


@pytest.mark.parametrize("screen", ALL_SCREENS)
def test_every_screen_has_an_advertised_way_out(screen):
    """The property the inbox actually broke: an escape the footer names."""
    table = nav.actions(screen)
    escapes = [(g, label) for g, (action, label) in table.items()
               if action in nav.LEAVING_ACTIONS and g in CLICKS]
    assert escapes, f"{screen} has no way out short of exiting the app"
    shown = " ".join(nav.hints(screen))
    assert any(label in shown for _g, label in escapes), \
        f"{screen} does not tell the operator how to leave"


@pytest.mark.parametrize("screen", ALL_SCREENS)
def test_four_clicks_always_exits(screen):
    assert nav.route(screen, QUAD) == nav.EXIT_APP


@pytest.mark.parametrize("screen", ALL_SCREENS)
@pytest.mark.parametrize("gesture", CLICKS)
def test_no_screen_has_a_gesture_that_does_nothing(screen, gesture):
    """A click that silently does nothing reads as a frozen app."""
    assert nav.route(screen, gesture) is not None


def test_empty_inbox_is_not_a_dead_end():
    """The regression: every click must leave a screen with no content."""
    for gesture in CLICKS:
        assert nav.route(INBOX, gesture, inbox_empty=True) == nav.BACK_TALK


def test_empty_inbox_still_exits_on_four_clicks():
    assert nav.route(INBOX, QUAD, inbox_empty=True) == nav.EXIT_APP


@pytest.mark.parametrize("screen", ALL_SCREENS)
def test_hints_describe_exactly_what_the_gestures_do(screen):
    """The footer is generated from the dispatch table, so it cannot lie."""
    text = " ".join(nav.hints(screen))
    for gesture in CLICKS:
        label = nav.actions(screen)[gesture][1]
        assert label in text, f"{screen}: {gesture} labelled {label!r} is not shown"


def test_empty_inbox_hint_tells_the_truth():
    text = " ".join(nav.hints(INBOX, inbox_empty=True))
    assert "back" in text
    assert "play" not in text, "an empty inbox must not advertise play"


@pytest.mark.parametrize("screen", ALL_SCREENS)
def test_every_hint_mentions_talk_and_exit(screen):
    """Wording is abbreviated to fit the panel; the meaning must survive."""
    text = " ".join(nav.hints(screen)).lower()
    assert "talk" in text, "every screen must say how to transmit"
    assert "exit" in text, "every screen must say how to leave the app"


def test_navigation_reaches_every_screen():
    """No screen may be strandable: all four stay reachable from any one."""
    reachable = {CONTACTS}
    frontier = [CONTACTS]
    targets = {
        nav.OPEN_TALK: TALK, nav.OPEN_INBOX: INBOX, nav.OPEN_STATUS: STATUS,
        nav.BACK_TALK: TALK, nav.BACK_CONTACTS: CONTACTS,
        nav.OPEN_SETTINGS: SETTINGS,
    }
    while frontier:
        screen = frontier.pop()
        for gesture in CLICKS:
            destination = targets.get(nav.route(screen, gesture))
            if destination and destination not in reachable:
                reachable.add(destination)
                frontier.append(destination)
    assert reachable == set(ALL_SCREENS)


def test_an_open_editor_owns_every_click_but_exit():
    """Routing an editor's clicks through the screen table would navigate
    away mid-edit instead of changing the value under the cursor."""
    table = nav.actions(EDIT)
    assert nav.route(EDIT, QUAD) == nav.EXIT_APP
    for gesture in CLICKS:
        assert gesture not in table


def test_app_implements_every_action_in_the_table():
    """A table entry with no handler would be a click that does nothing."""
    from app.main import WalkieApp

    app = WalkieApp.__new__(WalkieApp)  # no hardware needed for the mapping
    implemented = set(WalkieApp._build_actions(app).keys()) | {nav.EXIT_APP}

    declared = set()
    for screen in ALL_SCREENS:
        for empty in (False, True):
            declared |= {a for a, _ in nav.actions(screen, empty).values()}
    assert declared <= implemented, f"unhandled: {declared - implemented}"
