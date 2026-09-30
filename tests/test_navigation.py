"""Input routing -- and the dead end that prompted it.

The inbox screen used to print "2 clicks back" while routing two clicks
to *play*, which returned silently when there was nothing to play. On an
empty inbox that left no advertised way out. These tests pin the
invariants that prevent it recurring, now in MFruit OS's terms (the same
controls in every MFruit app):

    tap next · 2 clicks previous · hold open · 4 clicks back
    on talk screens a hold talks, and 3 clicks opens
"""

from __future__ import annotations

import pytest
from mfruit_sdk.input import BACK, EXTRA, NEXT, PREVIOUS, SELECT

from app.ui import navigation as nav
from app.ui.screens import (CONTACTS, EDIT, HOME, INBOX, PAIR, RANGE, SETTINGS,
                            START, STATUS, TALK)

ALL_SCREENS = (HOME, START, CONTACTS, TALK, INBOX, STATUS, SETTINGS, PAIR, RANGE)
ACTIONS = (NEXT, PREVIOUS, SELECT, EXTRA, BACK)
LISTS = (HOME, START, CONTACTS, INBOX, SETTINGS, PAIR)


@pytest.mark.parametrize("screen", LISTS)
def test_lists_step_with_tap_and_two_clicks(screen):
    """Every list moves the same way as MFruit OS's own lists."""
    assert nav.route(screen, NEXT).startswith("next_")
    assert nav.route(screen, PREVIOUS).startswith("previous_")


@pytest.mark.parametrize("screen", [s for s in LISTS if not nav.can_talk(s)])
def test_a_hold_opens_the_row_in_a_menu(screen):
    assert nav.route(screen, SELECT) in (nav.OPEN_ITEM, nav.OPEN_SETTING,
                                         nav.PAIR_SELECTED, nav.PLAY_SELECTED)


@pytest.mark.parametrize("screen", [s for s in LISTS if nav.can_talk(s)])
def test_three_clicks_opens_where_a_hold_talks(screen):
    """The hold is taken by talking, so 3 clicks (and Enter) open instead."""
    assert nav.route(screen, EXTRA) == nav.route(screen, SELECT)
    assert nav.route(screen, EXTRA) in (nav.OPEN_ITEM, nav.OPEN_TALK)


@pytest.mark.parametrize("screen", [s for s in ALL_SCREENS if s != HOME])
def test_four_clicks_goes_back(screen):
    assert nav.route(screen, BACK) == nav.GO_BACK


def test_four_clicks_on_home_leaves_the_app():
    assert nav.route(HOME, BACK) == nav.EXIT_APP


def test_three_clicks_on_home_do_not_open_status():
    """An incomplete four-click exit must not enter an unrelated screen."""
    assert nav.route(HOME, EXTRA) is None


def test_status_has_one_back_selection_and_taps_stay_on_the_page():
    assert nav.route(STATUS, NEXT) == nav.NEXT_ITEM
    assert nav.route(STATUS, PREVIOUS) == nav.PREVIOUS_ITEM
    assert nav.route(STATUS, SELECT) == nav.GO_BACK
    assert nav.route(STATUS, EXTRA) is None


@pytest.mark.parametrize("screen", ALL_SCREENS)
def test_every_screen_has_an_advertised_way_out(screen):
    """The property the inbox actually broke: an escape the footer names."""
    hints = dict(nav.hints(screen))
    assert "4×" in hints, f"{screen} does not say how to go back"
    assert nav.route(screen, BACK) in nav.LEAVING_ACTIONS


@pytest.mark.parametrize("screen", ALL_SCREENS)
def test_the_way_back_survives_a_narrow_footer(screen):
    """The footer drops hints from the end; 4 clicks is never among them."""
    gestures = [gesture for gesture, _label in nav.hints(screen)]
    assert "4×" in gestures[:3]


@pytest.mark.parametrize("screen", ALL_SCREENS)
def test_tap_and_hold_and_back_do_something_everywhere(screen):
    """A press that silently does nothing reads as a frozen app."""
    for action in (NEXT, SELECT, BACK):
        assert nav.route(screen, action) is not None, (screen, action)


def test_empty_inbox_is_not_a_dead_end():
    """The sole Back row leaves deliberately; moving does not leave."""
    assert nav.route(INBOX, NEXT, inbox_empty=True) == nav.NEXT_MESSAGE
    assert nav.route(INBOX, PREVIOUS, inbox_empty=True) == nav.PREVIOUS_MESSAGE
    assert nav.route(INBOX, SELECT, inbox_empty=True) == nav.GO_BACK
    assert nav.route(INBOX, BACK, inbox_empty=True) == nav.GO_BACK
    assert nav.route(INBOX, EXTRA, inbox_empty=True) is None


@pytest.mark.parametrize("screen", ALL_SCREENS)
def test_hints_describe_exactly_what_the_actions_do(screen):
    """The footer is generated from the dispatch table, so it cannot lie."""
    table = nav.actions(screen)
    for gesture, label in nav.hints(screen):
        if gesture == "hold" and nav.can_talk(screen):
            assert label == "talk"
            continue
        action = {"tap": NEXT, "2×": PREVIOUS, "hold": SELECT, "3×": EXTRA,
                  "4×": BACK}[gesture]
        assert table[action][1] == label, (screen, gesture)


def test_empty_inbox_hint_tells_the_truth():
    labels = [label for _gesture, label in nav.hints(INBOX, inbox_empty=True)]
    assert "back" in labels
    assert "play" not in labels, "an empty inbox must not advertise play"


@pytest.mark.parametrize("screen", ALL_SCREENS)
def test_hints_say_where_a_hold_talks(screen):
    hints = nav.hints(screen)
    if nav.can_talk(screen):
        assert hints[0] == ("hold", "talk"), f"{screen} talks on a hold, and must say so"
    else:
        assert ("hold", "talk") not in hints


def test_armed_hold_says_what_release_does():
    assert nav.hints(HOME, armed=True) == [("release", "to open")]
    assert nav.hints(SETTINGS, armed=True) == [("release", "to open")]


@pytest.mark.parametrize("screen", ALL_SCREENS)
def test_selected_back_uses_hold_without_talking_or_extra_actions(screen):
    expected = nav.EXIT_APP if screen == HOME else nav.GO_BACK
    assert nav.route(screen, SELECT, back_selected=True) == expected
    assert nav.route(screen, EXTRA, back_selected=True) is None
    assert nav.route(screen, BACK, back_selected=True) == expected
    assert not nav.can_talk(screen, back_selected=True)


@pytest.mark.parametrize("screen", LISTS)
def test_selected_back_keeps_list_navigation(screen):
    assert nav.route(screen, NEXT, back_selected=True) == nav.route(screen, NEXT)
    assert nav.route(screen, PREVIOUS, back_selected=True) == nav.route(screen, PREVIOUS)


@pytest.mark.parametrize("screen", ALL_SCREENS)
def test_selected_back_footer_describes_hold_and_release(screen):
    label = "exit" if screen == HOME else "back"
    hints = nav.hints(screen, back_selected=True)
    assert ("hold", label) in hints[:3]
    assert ("hold", "talk") not in hints
    assert all(gesture != "3×" for gesture, _label in hints)
    assert nav.hints(screen, armed=True, back_selected=True) == [("release", f"to {label}")]


def test_empty_inbox_armed_hold_says_back():
    assert nav.hints(INBOX, inbox_empty=True, armed=True) == [("release", "to back")]


def test_only_start_the_paired_list_talk_and_range_talk_on_a_hold():
    assert {s for s in ALL_SCREENS if nav.can_talk(s)} == {START, CONTACTS, TALK, RANGE}


def test_keyboard_letters_reach_the_three_click_actions():
    assert nav.route_char(TALK, "R") == nav.REPLAY_LAST
    assert nav.route_char(HOME, "s") == nav.OPEN_STATUS
    assert nav.route_char(RANGE, "p") == nav.PROBE_NOW
    assert nav.route_char(SETTINGS, "x") is None


def test_every_screen_is_reachable_from_home():
    """Menus open their rows' screens; the app decides which. Model that."""
    opens = {
        (HOME, nav.OPEN_ITEM): (START, INBOX, PAIR, SETTINGS, RANGE, STATUS),
        (START, nav.OPEN_ITEM): (TALK, CONTACTS),
        (CONTACTS, nav.OPEN_TALK): (TALK,),
        (TALK, nav.OPEN_INBOX): (INBOX,),
    }
    reachable, frontier = {HOME}, [HOME]
    while frontier:
        screen = frontier.pop()
        for action in ACTIONS:
            for destination in opens.get((screen, nav.route(screen, action)), ()):
                if destination not in reachable:
                    reachable.add(destination)
                    frontier.append(destination)
    assert reachable == set(ALL_SCREENS)


def test_an_open_editor_owns_all_input():
    """Routing an editor's input through the screen table would navigate
    away mid-edit instead of changing the value under the cursor."""
    assert nav.actions(EDIT) == {}
    for action in ACTIONS:
        assert nav.route(EDIT, action) is None


def test_app_implements_every_action_in_the_table():
    """A table entry with no handler would be a press that does nothing."""
    from app.main import WalkieApp

    app = WalkieApp.__new__(WalkieApp)  # no hardware needed for the mapping
    implemented = set(WalkieApp._build_actions(app).keys()) | {nav.EXIT_APP}

    declared = set()
    for screen in ALL_SCREENS:
        for empty in (False, True):
            declared |= {a for a, _ in nav.actions(screen, empty).values()}
    for letters in nav.CHAR_ACTIONS.values():
        declared |= set(letters.values())
    assert declared <= implemented, f"unhandled: {declared - implemented}"
