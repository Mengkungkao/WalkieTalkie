"""This radio's keys, and the keys of every radio it has paired with.

Kept apart from settings.json because it holds secrets: the file is
created readable by its owner only, and is never copied between devices
(deploy.sh does not touch ~/.whisplay-walkie). Losing it is not
dangerous, only inconvenient -- the radio makes new keys and has to be
paired again.

See `app.radio.crypto` for what the keys are used for.
"""

from __future__ import annotations

import json
import os
import tempfile

from app.radio import crypto
from app.utils.logger import get_logger

log = get_logger("keyring")

FILE_NAME = "keys.json"


class Keyring:
    def __init__(self, data_dir):
        self.path = data_dir / FILE_NAME
        self._pairwise = {}
        self._load()

    # --- persistence ----------------------------------------------------
    def _load(self):
        data = {}
        if self.path.is_file():
            try:
                data = json.loads(self.path.read_text())
            except Exception:
                log.warning("could not read %s; making new keys", self.path)
        try:
            self.private = bytes.fromhex(data["private"])
            self.broadcast_key = bytes.fromhex(data["broadcast"])
            self.public = crypto.public_of(self.private)
        except (KeyError, TypeError, ValueError):
            self._new_identity()
            data = {}
        self._peers = {}
        for addr, entry in (data.get("peers") or {}).items():
            try:
                self._peers[int(addr)] = (bytes.fromhex(entry["public"]),
                                          bytes.fromhex(entry["broadcast"]))
            except (KeyError, TypeError, ValueError):
                continue
        if not data:
            self.save()

    def _new_identity(self):
        self.private = crypto.new_private()
        self.public = crypto.public_of(self.private)
        self.broadcast_key = crypto.new_key()
        self._pairwise = {}
        log.info("made new keys for this radio")

    def save(self):
        """Atomically, and readable by the owner only."""
        data = {
            "private": self.private.hex(),
            "broadcast": self.broadcast_key.hex(),
            "peers": {str(addr): {"public": public.hex(), "broadcast": broadcast.hex()}
                      for addr, (public, broadcast) in self._peers.items()},
        }
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            handle, temporary = tempfile.mkstemp(dir=self.path.parent, suffix=".tmp")
            try:
                os.fchmod(handle, 0o600)
                with os.fdopen(handle, "w") as out:
                    json.dump(data, out, indent=1)
                    out.flush()
                    os.fsync(out.fileno())
                os.replace(temporary, self.path)
            except BaseException:
                os.unlink(temporary)
                raise
        except OSError:
            log.warning("could not write %s", self.path, exc_info=True)

    def reset(self):
        """New keys, no peers: every radio has to pair again."""
        self._new_identity()
        self._peers = {}
        self.save()

    # --- peers -----------------------------------------------------------
    def is_paired(self, addr: int) -> bool:
        return addr in self._peers

    @property
    def paired(self) -> list:
        return sorted(self._peers)

    def add_peer(self, addr: int, public: bytes, broadcast: bytes):
        self._peers[int(addr)] = (bytes(public), bytes(broadcast))
        self._pairwise.pop(int(addr), None)
        self.save()

    def remove_peer(self, addr: int):
        if self._peers.pop(int(addr), None) is not None:
            self._pairwise.pop(int(addr), None)
            self.save()

    def peer_public(self, addr: int) -> bytes | None:
        entry = self._peers.get(addr)
        return entry[0] if entry else None

    def peer_broadcast(self, addr: int) -> bytes | None:
        """The key `addr` seals its messages to ALL with."""
        entry = self._peers.get(addr)
        return entry[1] if entry else None

    def pairwise(self, addr: int) -> bytes | None:
        """The key only this radio and `addr` hold."""
        if addr not in self._pairwise:
            public = self.peer_public(addr)
            if public is None:
                return None
            self._pairwise[addr] = crypto.pairwise_key(self.private, public)
        return self._pairwise[addr]

    # --- pairing -----------------------------------------------------------
    def code_with(self, their_public: bytes) -> str:
        return crypto.pairing_code(self.private, their_public)

    def pair_body(self, their_public: bytes, src: int, dst: int, name: str) -> bytes:
        """Our public key in the clear; our broadcast key and name sealed
        so that only the owner of `their_public` can read them."""
        key = crypto.pairwise_key(self.private, their_public)
        secret = self.broadcast_key + name.encode("utf-8")[:20]
        return self.public + crypto.seal_blob(key, src, dst, secret)

    def open_pair_body(self, body: bytes, src: int, dst: int):
        """(public, broadcast key, name) from a pairing message, or None."""
        if len(body) < crypto.PUBLIC_SIZE:
            return None
        public = bytes(body[:crypto.PUBLIC_SIZE])
        try:
            key = crypto.pairwise_key(self.private, public)
        except ValueError:
            return None
        secret = crypto.open_blob(key, src, dst, body[crypto.PUBLIC_SIZE:])
        if secret is None or len(secret) < crypto.KEY_SIZE:
            return None
        name = secret[crypto.KEY_SIZE:].decode("utf-8", "replace").strip()[:20]
        return public, secret[:crypto.KEY_SIZE], name
