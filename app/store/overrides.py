"""Settings changed on the device, persisted across restarts.

config.yaml is the shipped default; this is what the operator changed
with the button. It has to be a separate file for two reasons.

Rewriting config.yaml would destroy its comments, which are most of its
value -- they explain what an air speed is and why the duty cycle is
1%. And config.yaml is under version control and shared between nodes,
while these values are the opposite: a device's address must be unique
to it, so the one setting that must never be copied between devices
would be the one a deploy overwrites.

Precedence is defaults < config.yaml < overrides < environment. The
environment stays on top so a one-off `WALKIE_RADIO_ADDRESS=9 ./run.sh`
still wins for debugging without permanently changing what the operator
configured.
"""

from __future__ import annotations

import json
import os
import random
import tempfile

from app.utils.logger import get_logger

log = get_logger("overrides")

FILE_NAME = "settings.json"

# Only these may be set from the device. Anything else (air speed, duty
# cycle, codec mode) changes how the radio behaves on air and belongs in
# config.yaml where it can be commented and reviewed.
ALLOWED = {
    "radio": {"address", "privacy_channel"},
    "identity": {"callsign"},
}


class Overrides:
    def __init__(self, data_dir):
        self.path = data_dir / FILE_NAME
        self.data = self._load()

    def _load(self) -> dict:
        if not self.path.is_file():
            return {}
        try:
            data = json.loads(self.path.read_text())
            return data if isinstance(data, dict) else {}
        except Exception:
            log.warning("could not read %s; ignoring it", self.path)
            return {}

    def save(self):
        """Write atomically: a truncated settings file would strand the radio."""
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(
                "w", dir=self.path.parent, delete=False, suffix=".tmp"
            ) as handle:
                json.dump(self.data, handle, indent=1)
                handle.flush()
                os.fsync(handle.fileno())
                temporary = handle.name
            os.replace(temporary, self.path)
        except OSError:
            log.warning("could not write %s", self.path, exc_info=True)

    # --- scalar settings ------------------------------------------------
    def set(self, section: str, key: str, value):
        if key not in ALLOWED.get(section, ()):  # pragma: no cover - guard
            raise KeyError(f"{section}.{key} is not settable from the device")
        self.data.setdefault(section, {})[key] = value
        self.save()

    def get(self, section: str, key: str, default=None):
        return self.data.get(section, {}).get(key, default)

    # --- contacts -------------------------------------------------------
    @property
    def contacts(self) -> list:
        return [c for c in self.data.get("contacts", []) if isinstance(c, dict)]

    def add_contact(self, name: str, address: int) -> bool:
        """Add a station. False if that address is already known."""
        if any(int(c.get("address", -1)) == address for c in self.contacts):
            return False
        self.data.setdefault("contacts", []).append(
            {"name": name, "address": int(address)}
        )
        self.save()
        return True

    def rename_contact(self, address: int, name: str) -> bool:
        for contact in self.contacts:
            if int(contact.get("address", -1)) == address:
                if contact.get("name") == name:
                    return False
                contact["name"] = name
                self.save()
                return True
        return False

    def remove_contact(self, address: int) -> bool:
        before = self.contacts
        kept = [c for c in before if int(c.get("address", -1)) != address]
        if len(kept) == len(before):
            return False
        self.data["contacts"] = kept
        self.save()
        return True

    # --- identity -------------------------------------------------------
    def assign_address(self, avoid=()) -> int:
        """Pick an unused Device ID for this radio, and keep it.

        Random rather than a shared default: every radio used to ship as
        address 5, and two radios on one address cannot talk -- each
        drops the other's packets as its own echo. With a handful of
        radios, a random pick out of 65534 almost never collides, and
        pairing detects and fixes it when it does.
        """
        avoid = set(avoid) | {0, 0xFFFF}
        picker = random.SystemRandom()
        address = picker.randint(1, 0xFFFE)
        while address in avoid:
            address = picker.randint(1, 0xFFFE)
        self.set("radio", "address", address)
        return address

    @property
    def node_token(self) -> bytes:
        """This installation's random token; see protocol.TOKEN_SIZE."""
        token = self.data.get("node_token")
        try:
            value = bytes.fromhex(token) if isinstance(token, str) else b""
        except ValueError:
            value = b""
        if len(value) != 4:
            value = os.urandom(4)
            self.data["node_token"] = value.hex()
            self.save()
        return value

    # --- base station ---------------------------------------------------
    @property
    def base_address(self):
        value = self.data.get("base_address")
        return int(value) if value is not None else None

    def set_base(self, address):
        if address is None:
            self.data.pop("base_address", None)
        else:
            self.data["base_address"] = int(address)
        self.save()

    # --- clock ----------------------------------------------------------
    @property
    def clock_offset(self) -> float:
        """Seconds to add to the system clock for display.

        Used when the clock cannot be set for real -- a Pi with no RTC
        and no passwordless sudo cannot have its system time changed by
        this app, but message timestamps should still read correctly.
        """
        try:
            return float(self.data.get("clock_offset", 0.0))
        except (TypeError, ValueError):
            return 0.0

    def set_clock_offset(self, seconds: float):
        if abs(seconds) < 1.0:
            self.data.pop("clock_offset", None)
        else:
            self.data["clock_offset"] = float(seconds)
        self.save()

    # --- reset ------------------------------------------------------------
    def clear(self):
        self.data = {}
        try:
            self.path.unlink(missing_ok=True)
        except OSError:
            log.warning("could not delete %s", self.path, exc_info=True)


def apply(settings, overrides: "Overrides"):
    """Fold saved overrides into a freshly loaded Settings object."""
    from app.config.settings import Contact

    address = overrides.get("radio", "address")
    if address is not None:
        settings.radio.address = int(address)
    elif settings.radio.address is None:
        settings.radio.address = overrides.assign_address(
            c.address for c in settings.contacts)
    channel = overrides.get("radio", "privacy_channel")
    if channel is not None:
        settings.radio.privacy_channel = int(channel)
    callsign = overrides.get("identity", "callsign")
    if callsign:
        settings.identity.callsign = str(callsign)

    known = {c.address for c in settings.contacts}
    for entry in overrides.contacts:
        try:
            addr = int(entry["address"])
        except (KeyError, TypeError, ValueError):
            continue
        if addr not in known:
            settings.contacts.append(
                Contact(name=str(entry.get("name") or f"node {addr}"), address=addr)
            )
            known.add(addr)
    return settings
