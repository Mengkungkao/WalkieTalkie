"""One-button editors.

Every value in Settings is entered with a single button, so each editor
is a small state machine driven by click counts. They hold all their own
state and touch no hardware, which is what makes Settings testable at
all.
"""

from __future__ import annotations

import datetime

import pytest

from app.ui.editors import ChoiceEditor, ClockEditor, ConfirmEditor, DigitEditor

SINGLE, DOUBLE, TRIPLE = "single", "double", "triple"


def play(editor, *gestures):
    for gesture in gestures:
        editor.handle(gesture)
    return editor


# --- digits ------------------------------------------------------------
def test_digits_start_from_the_current_value():
    assert DigitEditor(42, digits=5).text == "00042"


def test_one_click_bumps_the_digit_under_the_cursor():
    editor = DigitEditor(0, digits=3)
    play(editor, SINGLE, SINGLE)
    assert editor.text == "200"


def test_digits_wrap_from_nine_to_zero():
    editor = DigitEditor(0, digits=2)
    play(editor, *[SINGLE] * 10)
    assert editor.text == "00"


def test_two_clicks_walks_the_cursor_then_commits():
    editor = DigitEditor(0, digits=3)
    play(editor, SINGLE, DOUBLE, SINGLE, DOUBLE, SINGLE)
    assert editor.text == "111" and not editor.done
    editor.handle(DOUBLE)
    assert editor.done and not editor.cancelled and editor.value == 111


def test_three_clicks_cancels_from_any_field():
    editor = DigitEditor(7, digits=5)
    play(editor, SINGLE, DOUBLE, TRIPLE)
    assert editor.done and editor.cancelled


def test_a_value_over_the_maximum_is_clamped_not_refused():
    """Refusing to close would read as a frozen screen."""
    editor = DigitEditor(0, digits=5, maximum=65534)
    editor.cells = [9, 9, 9, 9, 9]
    editor.cursor = 4
    editor.handle(DOUBLE)
    assert editor.done and editor.value == 65534


# --- choices -----------------------------------------------------------
def test_choice_cycles_and_commits():
    editor = ChoiceEditor([("Base", 1), ("Rover", 5), ("(none)", None)])
    assert editor.value == 1
    play(editor, SINGLE)
    assert editor.text == "Rover" and editor.value == 5
    editor.handle(DOUBLE)
    assert editor.done and not editor.cancelled


def test_choice_wraps_and_can_be_cancelled():
    editor = ChoiceEditor([("a", 1), ("b", 2)])
    play(editor, SINGLE, SINGLE)
    assert editor.value == 1
    editor.handle(TRIPLE)
    assert editor.cancelled


def test_choice_survives_an_empty_list():
    editor = ChoiceEditor([])
    assert editor.value is None and editor.text == "(none)"


# --- confirm -----------------------------------------------------------
def test_confirm_starts_on_no():
    """Two clicks means 'back' nearly everywhere, so it must not wipe data."""
    editor = ConfirmEditor("Erase everything?")
    assert editor.text == "no"
    editor.handle(DOUBLE)
    assert editor.done and editor.cancelled


def test_confirm_requires_selecting_yes_first():
    editor = ConfirmEditor("Erase everything?")
    play(editor, SINGLE, DOUBLE)
    assert editor.done and not editor.cancelled and editor.yes


def test_confirm_three_clicks_always_cancels():
    editor = ConfirmEditor("Erase everything?")
    play(editor, SINGLE, TRIPLE)
    assert editor.done and editor.cancelled and not editor.yes


# --- clock -------------------------------------------------------------
def test_clock_starts_from_the_given_moment():
    editor = ClockEditor(datetime.datetime(2026, 9, 4, 14, 30))
    assert editor.text == "2026-09-04  14:30"
    assert editor.field_name == "year"


def test_clock_fields_wrap_within_their_own_range():
    editor = ClockEditor(datetime.datetime(2026, 12, 31, 23, 59))
    editor.field = 1                       # month
    editor.handle(SINGLE)
    assert editor.values[1] == 1           # 12 -> 1, not 13
    editor.field = 3                       # hour
    editor.handle(SINGLE)
    assert editor.values[3] == 0           # 23 -> 0


def test_clock_walks_every_field_then_commits():
    editor = ClockEditor(datetime.datetime(2026, 9, 4, 14, 30))
    for _ in range(len(ClockEditor.FIELDS) - 1):
        editor.handle(DOUBLE)
        assert not editor.done
    editor.handle(DOUBLE)
    assert editor.done and not editor.cancelled


def test_an_impossible_day_is_clamped_to_the_month():
    """31 February must not raise on the way out of the editor."""
    editor = ClockEditor(datetime.datetime(2026, 1, 31, 9, 0))
    editor.values[1] = 2                   # February
    assert editor.to_datetime() == datetime.datetime(2026, 2, 28, 9, 0)
