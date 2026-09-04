"""The paired-device list the app opens on.

Two sources feed one list:

* **config.yaml contacts** -- devices you deliberately paired, with a
  name you chose. These are always shown, in order, even when the other
  radio is switched off, because you need to be able to select a peer
  before you can call it.
* **peers heard on the air** -- anything that has transmitted to us and
  is not already a contact. LoRa has no discovery protocol, so this is
  the closest thing to one: a station that says hello shows up, and can
  be replied to without editing any config.

`ALL STATIONS` is always first: broadcast is the walkie-talkie default,
and pressing talk without choosing anyone should reach everybody.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass

from app.config.settings import Contact
from app.radio.protocol import BROADCAST
from app.utils.logger import get_logger

log = get_logger("roster")

BROADCAST_NAME = "ALL STATIONS"
ROSTER_FILE = "roster.json"


@dataclass
class Entry:
    name: str
    address: int
    known: bool  # from config.yaml rather than discovered
    last_heard: float = 0.0
    last_rssi: int | None = None

    @property
    def is_broadcast(self) -> bool:
        return self.address == BROADCAST

    @property
    def status(self) -> str:
        if self.is_broadcast:
            return "broadcast"
        if not self.last_heard:
            return "never heard"
        age = time.time() - self.last_heard
        if age < 60:
            return f"{int(age)}s ago"
        if age < 3600:
            return f"{int(age // 60)}m ago"
        if age < 86400:
            return f"{int(age // 3600)}h ago"
        return f"{int(age // 86400)}d ago"

    @property
    def online(self) -> bool:
        return self.is_broadcast or (
            bool(self.last_heard) and time.time() - self.last_heard < 900
        )


class Roster:
    """Ordered, de-duplicated view over contacts and discovered peers."""

    def __init__(self, contacts: list, data_dir):
        self._configured = [
            Contact(name=c.name, address=c.address)
            for c in contacts if c.address != BROADCAST
        ]
        self._path = data_dir / ROSTER_FILE
        self._seen = self._load()
        self.selected_index = 0

    # --- persistence ---------------------------------------------------
    def _load(self) -> dict:
        if not self._path.is_file():
            return {}
        try:
            raw = json.loads(self._path.read_text())
            return {int(k): v for k, v in raw.items()}
        except Exception:
            log.warning("could not read %s; starting fresh", self._path)
            return {}

    def save(self):
        try:
            self._path.write_text(json.dumps(
                {str(k): v for k, v in self._seen.items()}, indent=1
            ))
        except OSError:
            log.warning("could not write %s", self._path, exc_info=True)

    # --- updates -------------------------------------------------------
    def note_peer(self, addr: int, name: str = "", rssi: int | None = None):
        """Record a station we just heard from."""
        if addr == BROADCAST:
            return
        record = self._seen.setdefault(addr, {})
        record["last_heard"] = time.time()
        if name:
            record["name"] = name
        if rssi is not None:
            record["last_rssi"] = rssi

    # --- view ----------------------------------------------------------
    def entries(self) -> list:
        out = [Entry(name=BROADCAST_NAME, address=BROADCAST, known=True)]
        placed = {BROADCAST}
        for contact in self._configured:
            record = self._seen.get(contact.address, {})
            out.append(Entry(
                name=contact.name, address=contact.address, known=True,
                last_heard=record.get("last_heard", 0.0),
                last_rssi=record.get("last_rssi"),
            ))
            placed.add(contact.address)
        # Discovered stations, most recently heard first.
        extras = [
            (addr, record) for addr, record in self._seen.items()
            if addr not in placed
        ]
        extras.sort(key=lambda item: item[1].get("last_heard", 0.0), reverse=True)
        for addr, record in extras:
            out.append(Entry(
                name=record.get("name") or f"node {addr}", address=addr,
                known=False, last_heard=record.get("last_heard", 0.0),
                last_rssi=record.get("last_rssi"),
            ))
        return out

    def selected(self) -> Entry:
        entries = self.entries()
        self.selected_index %= len(entries)
        return entries[self.selected_index]

    def advance(self, step: int = 1) -> Entry:
        entries = self.entries()
        self.selected_index = (self.selected_index + step) % len(entries)
        return entries[self.selected_index]

    def select_address(self, addr: int) -> bool:
        for index, entry in enumerate(self.entries()):
            if entry.address == addr:
                self.selected_index = index
                return True
        return False
