"""Entering values with one button -- or a keyboard.

The HAT has a single button, so every editor here is a small state
machine driven by input actions, with no state anywhere else. They are
plain objects with no display or hardware dependency, which is what lets
the whole of Settings be tested without a Pi.

The actions are MFruit OS's (mfruit_sdk.input), so nothing has to be
relearned inside an editor:

    tap / Down       change the thing under the cursor (next digit value, choice)
    2 clicks / Up    change it back the other way
    hold / Enter     commit -- next field, then save on the last one
                     (Enter on a keyboard saves at once)
    4 clicks / Esc   cancel, discarding every change
    digits           typed straight into a number; Backspace steps back

Push-to-talk is suspended while an editor is open. Hold-to-talk works on
every other screen deliberately, but a hold here would transmit whatever
half-edited address is on screen, and the operator is plainly not trying
to talk while setting a value.

**Digits are edited most-significant first** and the cursor only moves
forward. Wrapping 0-9 with a single click means an address like 65534
costs a lot of presses; that is the price of one button, and it is paid
rarely (a keyboard types it directly). What must not happen is a
mis-entry being hard to abandon, so cancel is always four clicks or Esc,
from any field.
"""

from __future__ import annotations

from mfruit_sdk.input import BACK, CHAR, ERASE, NEXT, PREVIOUS, SELECT


class DigitEditor:
    """A fixed-width decimal number, edited one digit at a time."""

    def __init__(self, value: int = 0, digits: int = 5, minimum: int = 0,
                 maximum: int = 65534, label: str = ""):
        self.digits = digits
        self.minimum = minimum
        self.maximum = maximum
        self.label = label
        self.cursor = 0
        self.done = False
        self.cancelled = False
        self._set(value)

    def _set(self, value: int):
        value = max(self.minimum, min(self.maximum, int(value)))
        self.cells = [int(c) for c in str(value).zfill(self.digits)[-self.digits:]]

    @property
    def value(self) -> int:
        return int("".join(str(c) for c in self.cells))

    @property
    def valid(self) -> bool:
        return self.minimum <= self.value <= self.maximum

    @property
    def text(self) -> str:
        return "".join(str(c) for c in self.cells)

    def increment(self, step: int = 1):
        """Tap: bump the digit under the cursor, wrapping 9 -> 0."""
        self.cells[self.cursor] = (self.cells[self.cursor] + step) % 10

    def advance(self):
        """Hold: next digit, or commit from the last one."""
        if self.cursor + 1 < self.digits:
            self.cursor += 1
        else:
            self.commit()

    def commit(self):
        # Clamp rather than reject: an out-of-range number is always
        # a slip, and silently refusing to close looks like a freeze.
        self._set(self.value)
        self.done = True

    def type_digit(self, digit: int):
        """A digit typed on a keyboard: fill this cell, move to the next."""
        self.cells[self.cursor] = digit
        self.cursor = min(self.cursor + 1, self.digits - 1)

    def cancel(self):
        self.cancelled = True
        self.done = True

    def handle(self, action: str, char: str = "", keyboard: bool = False) -> bool:
        """Route an input action. True when the editor has finished."""
        if action == NEXT:
            self.increment()
        elif action == PREVIOUS:
            self.increment(-1)
        elif action == SELECT:
            self.commit() if keyboard else self.advance()
        elif action == BACK:
            self.cancel()
        elif action == CHAR and char.isdigit():
            self.type_digit(int(char))
        elif action == ERASE:
            self.cursor = max(0, self.cursor - 1)
        return self.done


class ChoiceEditor:
    """Pick one of a list. Each entry is (label, value)."""

    def __init__(self, choices: list, index: int = 0, label: str = ""):
        self.choices = list(choices) or [("(none)", None)]
        self.index = index % len(self.choices)
        self.label = label
        self.done = False
        self.cancelled = False

    @property
    def value(self):
        return self.choices[self.index][1]

    @property
    def text(self) -> str:
        return self.choices[self.index][0]

    def handle(self, action: str, char: str = "", keyboard: bool = False) -> bool:
        if action in (NEXT, PREVIOUS):
            step = 1 if action == NEXT else -1
            self.index = (self.index + step) % len(self.choices)
        elif action == SELECT:
            self.done = True
        elif action == BACK:
            self.cancelled = True
            self.done = True
        elif action == CHAR and char.strip():
            # A typed letter jumps to the next choice that starts with it.
            count = len(self.choices)
            for offset in range(1, count + 1):
                index = (self.index + offset) % count
                if str(self.choices[index][0]).lower().startswith(char.lower()):
                    self.index = index
                    break
        return self.done


class ConfirmEditor:
    """A destructive action, defaulting to No.

    Starting on "No" and requiring the operator to click onto "Yes"
    before confirming means no single reflex gesture can wipe the
    inbox -- which matters when the same hold means "open" almost
    everywhere else.
    """

    def __init__(self, prompt: str, detail: str = ""):
        self.prompt = prompt
        self.detail = detail
        self.yes = False
        self.done = False
        self.cancelled = False

    @property
    def text(self) -> str:
        return "YES" if self.yes else "no"

    def handle(self, action: str, char: str = "", keyboard: bool = False) -> bool:
        if action in (NEXT, PREVIOUS):
            self.yes = not self.yes
        elif action == CHAR and char.lower() in ("y", "n"):
            self.yes = char.lower() == "y"
        elif action == SELECT:
            self.done = True
            self.cancelled = not self.yes
        elif action == BACK:
            self.yes = False
            self.cancelled = True
            self.done = True
        return self.done


class ClockEditor:
    """Date and time, as a row of digit fields.

    Fields are ordered largest-first (year down to minute) so the cursor
    only ever moves in one direction and the display reads like a date.
    """

    FIELDS = (
        ("year", 2020, 2099, 4),
        ("month", 1, 12, 2),
        ("day", 1, 31, 2),
        ("hour", 0, 23, 2),
        ("minute", 0, 59, 2),
    )

    def __init__(self, when):
        self.values = [when.year, when.month, when.day, when.hour, when.minute]
        self.field = 0
        self.done = False
        self.cancelled = False

    @property
    def text(self) -> str:
        y, mo, d, h, mi = self.values
        return f"{y:04d}-{mo:02d}-{d:02d}  {h:02d}:{mi:02d}"

    @property
    def field_name(self) -> str:
        return self.FIELDS[self.field][0]

    def handle(self, action: str, char: str = "", keyboard: bool = False) -> bool:
        name, low, high, _width = self.FIELDS[self.field]
        if action == NEXT:
            value = self.values[self.field] + 1
            self.values[self.field] = low if value > high else value
        elif action == PREVIOUS:
            value = self.values[self.field] - 1
            self.values[self.field] = high if value < low else value
        elif action == SELECT:
            if self.field + 1 < len(self.FIELDS):
                self.field += 1
            else:
                self.done = True
        elif action == ERASE:
            self.field = max(0, self.field - 1)
        elif action == BACK:
            self.cancelled = True
            self.done = True
        return self.done

    def to_datetime(self):
        """The edited moment, with an impossible day clamped to the month."""
        import calendar
        import datetime

        year, month, day, hour, minute = self.values
        day = min(day, calendar.monthrange(year, month)[1])
        return datetime.datetime(year, month, day, hour, minute)
