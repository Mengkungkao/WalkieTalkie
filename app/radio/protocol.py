"""Application protocol carried inside each framed radio packet.

Layered on top of `framing`:

    [FF FF chan]  <- eaten by the module: always a module broadcast
    [ COBS( header(10) || body || crc16 ) 0x00 ]

    header: ver+type | channel+sealing | src(2) | dst(2) | msg_id | seq | total | flags

The header carries the sender's address, because in fixed transmission
mode the module tells the receiver nothing about who sent a packet, and
the destination, because the module's own address filter is not used:
every packet goes out as a module broadcast, every radio hears it, and
`Packet.addressed_to` does the filtering in software. That is what lets
a Device ID be set in the app -- the module's registers can only be
rewritten with the mode pins, which the Whisplay LCD owns on a Pi and an
Orange Pi cannot drive at all.

Version 3 adds two things to the second byte:

* **A privacy channel** (low six bits). Like the privacy codes on a
  handheld walkie-talkie, radios set to different channels share the
  frequency but ignore each other completely.
* **How the body is sealed** (top two bits): in the clear, with a
  pairwise key, or with the sender's broadcast key; see `crypto`. Only
  the messages that agree keys may travel in the clear.

Every radio has to run the same version: an older radio's packets are
not understood, and it does not understand ours.

Messages larger than one packet are split into fragments sharing a
`msg_id`, numbered `seq` of `total`. There are no retransmissions on the
voice path: at 700 bps a redelivery costs more airtime than the gap it
fills, so `Reassembler` substitutes silence for anything missing and
plays on. Text is short enough to be acknowledged and is retried.
"""

from __future__ import annotations

import struct
import time
from dataclasses import dataclass, field

from app.radio.framing import MAX_FRAME_PAYLOAD

VERSION = 3
HEADER = struct.Struct(">BBHHBBBB")  # ver_type, chan_seal, src, dst, msg_id, seq, total, flags
HEADER_SIZE = HEADER.size  # 10
MAX_BODY = MAX_FRAME_PAYLOAD - HEADER_SIZE  # 190

# How a body is sealed: the top two bits of the second header byte.
CLEAR = 0
PAIRWISE = 1  # with the key only the sender and the destination hold
SENDER = 2  # with the sender's broadcast key, held by everyone it paired with

# The privacy channel: the low six bits of the same byte.
DEFAULT_CHANNEL = 1
CHANNELS = range(1, 17)  # what Settings offers
_CHANNEL_MASK = 0x3F

# Message types
HELLO = 0x0  # "I am here" -- body is the operator's display name
TEXT = 0x1  # UTF-8 text
VOICE = 0x2  # codec2 bitstream
ACK = 0x3  # body is the acknowledged msg_id
BYE = 0x4  # leaving the channel
HELLO_ACK = 0x5  # "I hear you, and I accept" -- body is our name
REJECT = 0x6  # "I hear you, and I do not accept"
PAIR = 0x7  # "I am pairing" -- token, public key, name
PAIR_REQUEST = 0x8  # "pair with me" -- public key, then our secrets sealed for you
PAIR_ACCEPT = 0x9  # "yes" -- the same, back
REPAIR = 0xA  # "resend these" -- msg_id, then the missing fragment numbers

TYPE_NAMES = {HELLO: "hello", TEXT: "text", VOICE: "voice", ACK: "ack",
              BYE: "bye", HELLO_ACK: "hello-ack", REJECT: "reject",
              PAIR: "pair", PAIR_REQUEST: "pair-request",
              PAIR_ACCEPT: "pair-accept", REPAIR: "repair"}

# The only types that may travel in the clear: they are how two radios
# agree keys in the first place, or say no. Everything else from a radio
# we have not paired with is dropped unread.
CLEAR_TYPES = frozenset({PAIR, PAIR_REQUEST, PAIR_ACCEPT, REJECT})
# Heard on every privacy channel, not just this radio's own: two radios
# set to different channels could otherwise never find each other to
# pair -- which is exactly what happened the first time. The app only
# listens to them while its Pair screen is open.
PAIRING_TYPES = CLEAR_TYPES

# A random number each installation picks once. Two radios that ended up
# with the same Device ID are indistinguishable by address alone -- each
# drops the other's packets as its own echo -- but not by token, so the
# pairing beacon carries one and a clash can be seen and fixed.
TOKEN_SIZE = 4
PUBLIC_SIZE = 32

# A station is "linked" once it has both heard us and answered. Presence
# alone is not enough: hearing someone does not prove they hear you, and
# a one-way link is the classic radio failure -- you talk for a minute
# before discovering nobody received a word.
LINK_UNLINKED = "unlinked"
LINK_CALLING = "calling"
LINK_LINKED = "linked"
LINK_STALE = "stale"
LINK_REJECTED = "rejected"

# How long a completed handshake stays good without hearing anything.
#
# Long, deliberately. A walkie-talkie on standby is silent for most of
# its life, and treating silence as disconnection made the Talk screen
# say "not connected" about a station that was sitting right there and
# perfectly reachable. A handshake proved the link once; only evidence
# should retract that, not the absence of conversation.
#
# Past this the station is "stale" -- still linked, but worth re-checking
# and worth telling the operator when it was last heard.
LINK_TIMEOUT = 60 * 60

BROADCAST = 0xFFFF

# Voice fragments carry exactly this many bytes, all but the last. That
# is what lets a lost one be stood in for without shifting everything
# after it: 168 is a whole number of frames in every Codec2 mode (4, 6, 7
# and 8 bytes a frame), and exactly what a sealed fragment holds. Joining
# what did arrive end to end used to put every later frame out of step,
# and the rest of the message decoded as noise.
VOICE_CHUNK = 168

# A message's sender counts as gone quiet after this long with no new
# fragment -- they arrive a fraction of a second apart. Then the missing
# ones are asked for again, and each round waits for them to arrive.
QUIET_SECONDS = 2.0
REPAIR_WAIT = 2.0
REPAIR_WAIT_PER_FRAGMENT = 0.4
REPAIR_ROUNDS = 2
# A finished message is remembered this long, so fragments of it resent
# for someone else do not start it over again. Longer than any repair
# takes; short enough that a sender restarting its message numbers is
# not mistaken for a repeat (it also starts them at random).
DONE_SECONDS = 20.0
# Never hold a message longer than this after its last fragment, whatever
# happens: past it, what arrived is delivered with the gaps silenced.
REASSEMBLY_TIMEOUT = 30.0


@dataclass
class Packet:
    """One fragment, decoded."""

    type: int
    src: int
    msg_id: int
    seq: int
    total: int
    flags: int
    body: bytes
    rssi_dbm: int | None = None
    dst: int = BROADCAST
    channel: int = DEFAULT_CHANNEL
    sealing: int = CLEAR
    # The header exactly as received: a sealed body is authenticated
    # against it, so it cannot be rebuilt from the fields.
    header: bytes = b""

    @property
    def type_name(self) -> str:
        return TYPE_NAMES.get(self.type, f"0x{self.type:x}")

    def addressed_to(self, addr: int) -> bool:
        return self.dst in (addr, BROADCAST)


def encode_header(type_: int, src: int, msg_id: int, seq: int, total: int,
                  flags: int = 0, dst: int = BROADCAST,
                  channel: int = DEFAULT_CHANNEL, sealing: int = CLEAR) -> bytes:
    return HEADER.pack(
        ((VERSION & 0xF) << 4) | (type_ & 0xF),
        ((sealing & 0x3) << 6) | (channel & _CHANNEL_MASK),
        src & 0xFFFF, dst & 0xFFFF, msg_id & 0xFF, seq & 0xFF, total & 0xFF,
        flags & 0xFF,
    )


def encode(type_: int, src: int, msg_id: int, seq: int, total: int,
           body: bytes, flags: int = 0, dst: int = BROADCAST,
           channel: int = DEFAULT_CHANNEL, sealing: int = CLEAR) -> bytes:
    if len(body) > MAX_BODY:
        raise ValueError(f"body {len(body)} B exceeds {MAX_BODY} B per fragment")
    return encode_header(type_, src, msg_id, seq, total, flags, dst,
                         channel, sealing) + body


def decode(payload: bytes, rssi_dbm: int | None = None) -> Packet | None:
    """Parse one framed payload. None if it is not a packet we understand."""
    if len(payload) < HEADER_SIZE or (payload[0] >> 4) != VERSION:
        return None  # an older, future or foreign sender; ignore rather than guess
    ver_type, chan_seal, src, dst, msg_id, seq, total, flags = HEADER.unpack_from(payload)
    if total == 0 or seq >= total:
        return None
    return Packet(
        type=ver_type & 0xF, src=src, msg_id=msg_id, seq=seq, total=total,
        flags=flags, body=payload[HEADER_SIZE:], rssi_dbm=rssi_dbm, dst=dst,
        channel=chan_seal & _CHANNEL_MASK, sealing=chan_seal >> 6,
        header=bytes(payload[:HEADER_SIZE]),
    )


def fragment(type_: int, src: int, msg_id: int, body: bytes,
             flags: int = 0, dst: int = BROADCAST,
             channel: int = DEFAULT_CHANNEL, seal=None, sealing: int = CLEAR,
             overhead: int = 0, chunk: int | None = None) -> list[bytes]:
    """Split `body` into ready-to-frame packets.

    With `seal` -- a function (header, chunk) -> sealed body adding
    `overhead` bytes -- each fragment is sealed on its own, so a lost
    fragment never makes the others unreadable. `chunk` caps how much of
    the body each fragment carries (see VOICE_CHUNK).
    """
    size = MAX_BODY - overhead
    if chunk:
        size = min(size, chunk)
    chunks = [body[i:i + size] for i in range(0, len(body), size)] or [b""]
    total = len(chunks)
    if total > 255:
        raise ValueError(f"message needs {total} fragments; limit is 255")
    packets = []
    for seq, chunk in enumerate(chunks):
        header = encode_header(type_, src, msg_id, seq, total, flags, dst,
                               channel, sealing)
        packets.append(header + (seal(header, chunk) if seal else chunk))
    return packets


def pair_body(token: bytes, public: bytes, name: str) -> bytes:
    """What a pairing beacon carries: token, public key, then name."""
    token = bytes(token[:TOKEN_SIZE]).ljust(TOKEN_SIZE, b"\0")
    public = bytes(public[:PUBLIC_SIZE]).ljust(PUBLIC_SIZE, b"\0")
    limit = MAX_BODY - TOKEN_SIZE - PUBLIC_SIZE
    return token + public + name.encode("utf-8")[:limit]


def parse_pair(body: bytes) -> tuple:
    """(token, public key, name) from a pairing beacon's body."""
    start = TOKEN_SIZE + PUBLIC_SIZE
    name = body[start:].decode("utf-8", "replace").strip()[:20]
    return bytes(body[:TOKEN_SIZE]), bytes(body[TOKEN_SIZE:start]), name


@dataclass
class _Partial:
    total: int
    flags: int
    src: int
    type: int
    started: float
    dst: int = BROADCAST
    channel: int = DEFAULT_CHANNEL
    best_rssi: int | None = None
    chunks: dict = field(default_factory=dict)
    last_seen: float = 0.0
    deadline: float = 0.0
    repairs: int = 0
    fragment_size: int = 0
    highest: int = -1

    @property
    def size(self) -> int:
        """What each fragment but the last carries."""
        if self.fragment_size:
            return self.fragment_size
        return VOICE_CHUNK if self.type == VOICE else 0

    @property
    def complete(self) -> bool:
        return len(self.chunks) == self.total

    @property
    def missing(self) -> list:
        return [i for i in range(self.total) if i not in self.chunks]


@dataclass
class Message:
    """A reassembled message handed to the app."""

    type: int
    src: int
    msg_id: int
    body: bytes
    flags: int
    missing: list
    rssi_dbm: int | None
    received_at: float
    dst: int = BROADCAST
    channel: int = DEFAULT_CHANNEL
    # Where each missing fragment would have started is seq * this: the
    # body keeps a zero-filled space for it, so everything after it is
    # still where it belongs.
    fragment_size: int = 0

    @property
    def complete(self) -> bool:
        return not self.missing

    @property
    def type_name(self) -> str:
        return TYPE_NAMES.get(self.type, f"0x{self.type:x}")


class Reassembler:
    """Collects fragments into whole messages.

    Single-fragment messages (every text, every hello) complete on the
    spot and never allocate. A multi-fragment message is held until its
    last fragment lands; if some are missing, each partial carries a
    `deadline` -- `QUIET_SECONDS` after its latest fragment -- at which the
    link asks the sender for the missing ones (`postpone` then waits for
    them) or, when that is not possible or has been tried, `finish`es it
    with the gaps kept as zero-filled space of the right size.
    """

    def __init__(self, timeout: float = REASSEMBLY_TIMEOUT,
                 fragment_seconds: float = 0.0):
        self.timeout = timeout
        # How far apart the sender's fragments arrive: the quiet that
        # means "gone" is measured after the ones still to come.
        self.fragment_seconds = fragment_seconds
        self._partials: dict = {}
        self._done: dict = {}

    def push(self, packet: Packet) -> Message | None:
        if packet.total == 1:
            return Message(
                type=packet.type, src=packet.src, msg_id=packet.msg_id,
                body=packet.body, flags=packet.flags, missing=[],
                rssi_dbm=packet.rssi_dbm, received_at=time.time(),
                dst=packet.dst, channel=packet.channel,
            )

        now = time.monotonic()
        key = (packet.src, packet.msg_id, packet.type)
        if now - self._done.get(key, -DONE_SECONDS) < DONE_SECONDS:
            return None  # a resend of a message already delivered
        partial = self._partials.get(key)
        if partial is None or partial.total != packet.total:
            partial = _Partial(
                total=packet.total, flags=packet.flags, src=packet.src,
                type=packet.type, started=now, dst=packet.dst,
                channel=packet.channel,
            )
            self._partials[key] = partial

        if packet.seq in partial.chunks:
            return None  # a repeat of one we have, sent for someone else
        partial.chunks[packet.seq] = packet.body
        if packet.seq < packet.total - 1:
            partial.fragment_size = len(packet.body)
        partial.last_seen = now
        partial.highest = max(partial.highest, packet.seq)
        still_coming = max(0, partial.total - 1 - partial.highest)
        # Mid-repair, keep waiting for the rest of what was asked for.
        partial.deadline = max(partial.deadline, now + QUIET_SECONDS
                               + still_coming * self.fragment_seconds)
        if packet.rssi_dbm is not None:
            if partial.best_rssi is None or packet.rssi_dbm > partial.best_rssi:
                partial.best_rssi = packet.rssi_dbm

        if partial.complete:
            return self.finish(key)
        return None

    @property
    def pending(self) -> int:
        """Partial messages still open. Zero means the app can sleep deeply."""
        return len(self._partials)

    def next_deadline(self) -> float | None:
        """When the earliest partial needs attention (monotonic), if any."""
        if not self._partials:
            return None
        return min(p.deadline for p in self._partials.values())

    def due(self, now: float | None = None) -> list:
        """(key, partial) for every partial whose deadline has passed."""
        now = time.monotonic() if now is None else now
        return [(key, partial) for key, partial in self._partials.items()
                if now >= partial.deadline]

    def postpone(self, key, seconds: float):
        partial = self._partials.get(key)
        if partial is not None:
            partial.repairs += 1
            partial.deadline = time.monotonic() + seconds

    def finish(self, key) -> Message | None:
        partial = self._partials.pop(key, None)
        if partial is None:
            return None
        now = time.monotonic()
        for old in [k for k, at in self._done.items() if now - at >= DONE_SECONDS]:
            del self._done[old]
        self._done[key] = now
        return self._build(key, partial)

    def expire(self) -> list:
        """Emit partials whose sender went quiet `timeout` seconds ago."""
        now = time.monotonic()
        return [self.finish(key) for key, partial in list(self._partials.items())
                if now - partial.last_seen >= self.timeout]

    def _build(self, key, partial: _Partial) -> Message:
        size = partial.size
        pieces = []
        for seq in range(partial.total):
            if seq in partial.chunks:
                pieces.append(partial.chunks[seq])
            elif seq < partial.total - 1:
                pieces.append(bytes(size))  # keep the place of what is lost
        return Message(
            type=partial.type, src=partial.src, msg_id=key[1], body=b"".join(pieces),
            flags=partial.flags, missing=partial.missing,
            rssi_dbm=partial.best_rssi, received_at=time.time(),
            dst=partial.dst, channel=partial.channel, fragment_size=size,
        )
