"""Entering values with one button.

The HAT has a single button, so every editor here is a small state
machine driven by click counts, with no state anywhere else. They are
plain objects with no display or hardware dependency, which is what lets
the whole of Settings be tested without a Pi.

The gesture language matches the rest of the app so nothing has to be
relearned inside an editor:

    1 click   change the thing under the cursor (digit, choice)
    2 clicks  commit -- next field, then save on the last one
    3 clicks  cancel, discarding every change

Push-to-talk is suspended while an editor is open. Hold-to-talk works on
every other screen deliberately, but a hold here would transmit whatever
half-edited address is on screen, and the operator is plainly not trying
to talk while setting a value.

**Digits are edited most-significant first** and the cursor only moves
forward. Wrapping 0-9 with a single click means an address like 65534
costs a lot of presses; that is the price of one button, and it is paid
rarely. What must not happen is a mis-entry being hard to abandon, so
cancel is always three clicks, from any field.
"""

from __future__ import annotations


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

    def increment(self):
        """One click: bump the digit under the cursor, wrapping 9 -> 0."""
        self.cells[self.cursor] = (self.cells[self.cursor] + 1) % 10

    def advance(self):
        """Two clicks: next digit, or commit from the last one."""
        if self.cursor + 1 < self.digits:
            self.cursor += 1
        else:
            # Clamp rather than reject: an out-of-range number is always
            # a slip, and silently refusing to close looks like a freeze.
            self._set(self.value)
            self.done = True

    def cancel(self):
        self.cancelled = True
        self.done = True

    def handle(self, gesture: str) -> bool:
        """Route a gesture. True when the editor has finished."""
        if gesture == "single":
            self.increment()
        elif gesture == "double":
            self.advance()
        elif gesture == "triple":
            self.cancel()
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

    def handle(self, gesture: str) -> bool:
        if gesture == "single":
            self.index = (self.index + 1) % len(self.choices)
        elif gesture == "double":
            self.done = True
        elif gesture == "triple":
            self.cancelled = True
            self.done = True
        return self.done


class ConfirmEditor:
    """A destructive action, defaulting to No.

    Starting on "No" and requiring the operator to click onto "Yes"
    before confirming means no single reflex gesture can wipe the
    inbox -- which matters when the same two clicks mean "back" almost
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

    def handle(self, gesture: str) -> bool:
        if gesture == "single":
            self.yes = not self.yes
        elif gesture == "double":
            self.done = True
            self.cancelled = not self.yes
        elif gesture == "triple":
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

    def handle(self, gesture: str) -> bool:
        name, low, high, _width = self.FIELDS[self.field]
        if gesture == "single":
            value = self.values[self.field] + 1
            self.values[self.field] = low if value > high else value
        elif gesture == "double":
            if self.field + 1 < len(self.FIELDS):
                self.field += 1
            else:
                self.done = True
        elif gesture == "triple":
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
