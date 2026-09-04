"""Received messages, kept on disk so they survive a restart.

Voice payloads are stored as the **codec2 bitstream**, not decoded PCM:
a 20-second clip is 2 kB encoded against 320 kB as PCM, which matters on
a Pi that may be running from a small SD card, and decoding on replay
costs a few milliseconds.

The inbox is capped at `limit` entries and trimmed on write, so an
unattended radio left receiving overnight cannot fill the card.
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import asdict, dataclass, field

from app.radio import protocol
from app.utils.logger import get_logger

log = get_logger("inbox")

INDEX_FILE = "inbox.json"
VOICE_DIR = "voice"
DEFAULT_LIMIT = 50


@dataclass
class Item:
    id: str
    kind: str            # "text" | "voice"
    src: int
    peer_name: str
    received_at: float
    rssi_dbm: int | None = None
    text: str = ""
    voice_file: str = ""
    codec_mode: int = 0
    duration: float = 0.0
    incomplete: bool = False
    played: bool = False
    outgoing: bool = False

    @property
    def when(self) -> str:
        age = time.time() - self.received_at
        if age < 60:
            return "just now"
        if age < 3600:
            return f"{int(age // 60)}m"
        if age < 86400:
            return f"{int(age // 3600)}h"
        return time.strftime("%d %b", time.localtime(self.received_at))

    @property
    def summary(self) -> str:
        if self.kind == "text":
            return self.text[:40]
        mark = " (gaps)" if self.incomplete else ""
        return f"voice {self.duration:.1f}s{mark}"


class Inbox:
    def __init__(self, data_dir, limit: int = DEFAULT_LIMIT):
        self.dir = data_dir
        self.voice_dir = data_dir / VOICE_DIR
        self.voice_dir.mkdir(parents=True, exist_ok=True)
        self.path = data_dir / INDEX_FILE
        self.limit = limit
        self.items = self._load()

    def _load(self) -> list:
        if not self.path.is_file():
            return []
        try:
            return [Item(**entry) for entry in json.loads(self.path.read_text())]
        except Exception:
            log.warning("could not read %s; starting empty", self.path)
            return []

    def save(self):
        try:
            self.path.write_text(json.dumps([asdict(i) for i in self.items]))
        except OSError:
            log.warning("could not write inbox index", exc_info=True)

    @property
    def unread(self) -> int:
        return sum(1 for item in self.items if not item.played and not item.outgoing)

    def add_text(self, message, peer_name: str, outgoing: bool = False) -> Item:
        item = Item(
            id=uuid.uuid4().hex[:12], kind="text", src=message.src,
            peer_name=peer_name, received_at=message.received_at,
            rssi_dbm=message.rssi_dbm,
            text=message.body.decode("utf-8", "replace"),
            incomplete=not message.complete, outgoing=outgoing,
        )
        return self._append(item)

    def add_voice(self, message, peer_name: str, duration: float,
                  outgoing: bool = False, store_audio: bool = True) -> Item:
        item_id = uuid.uuid4().hex[:12]
        filename = ""
        # Our own transmissions are logged for the history but their audio
        # is not kept: it would double the card usage to store a copy of
        # something the operator just said.
        if store_audio and message.body:
            filename = f"{item_id}.c2"
            try:
                (self.voice_dir / filename).write_bytes(message.body)
            except OSError:
                log.warning("could not store voice payload", exc_info=True)
                filename = ""
        item = Item(
            id=item_id, kind="voice", src=message.src, peer_name=peer_name,
            received_at=message.received_at, rssi_dbm=message.rssi_dbm,
            voice_file=filename, codec_mode=message.flags, duration=duration,
            incomplete=not message.complete, outgoing=outgoing,
        )
        return self._append(item)

    def voice_bytes(self, item: Item) -> bytes:
        if not item.voice_file:
            return b""
        try:
            return (self.voice_dir / item.voice_file).read_bytes()
        except OSError:
            return b""

    def _append(self, item: Item) -> Item:
        self.items.insert(0, item)
        for stale in self.items[self.limit:]:
            if stale.voice_file:
                (self.voice_dir / stale.voice_file).unlink(missing_ok=True)
        del self.items[self.limit:]
        self.save()
        return item

    def mark_played(self, item: Item):
        item.played = True
        self.save()

    def latest_voice(self):
        return next(
            (i for i in self.items if i.kind == "voice" and not i.outgoing), None
        )
