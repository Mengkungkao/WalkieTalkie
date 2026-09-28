"""The radio link: one object the rest of the app talks to.

Owns the driver, the framer, the reassembler and the duty-cycle budget,
and presents `send_text` / `send_voice` / `on_message`.

Two threads, both of which block rather than poll -- the whole reason
this app can sit on a battery all day:

* **rx** parks in a blocking `read(1)` on the serial port. Zero CPU
  until a byte physically arrives.
* **tx** parks in `queue.get()`. Zero CPU until something is queued.

Everything is transmitted as a module broadcast, with the real
destination in the packet header (see `protocol`), so this radio's
address is `self.addr` -- the Device ID, changeable at runtime -- rather
than whatever number is burned into the module's registers.

**Sealing.** Given a keyring, the link seals everything it sends except
the few messages that agree keys (`protocol.CLEAR_TYPES`), and drops
anything it receives that is unsealed, sealed with a key it does not
hold, altered, replayed, or on another privacy channel -- before any of
it reaches the app. Without a keyring (tools, older tests) it works in
the clear, as it always did.

The tx thread also paces fragments. The module buffers only one packet;
firing a six-fragment voice message at it back-to-back overruns that
buffer and the tail is silently dropped. So each fragment is followed by
a sleep of its own estimated airtime plus a guard, which is also where
the duty-cycle budget is charged and, when exhausted, waited out.

A third thread, **reassembly**, sleeps until a half-received message
needs attention. At the edge of range fragments go missing; rather than
play noise, or wait forever for a last fragment that is never coming,
it asks the sender for the missing ones (`protocol.REPAIR`) -- the
sender keeps each message for a minute to answer -- and then delivers
what it has, with the gaps kept in place for the player to silence.
"""

from __future__ import annotations

import itertools
import os
import queue
import random
import threading
import time
from collections import deque
from dataclasses import dataclass, field

from app.radio import crypto, protocol
from app.radio.airtime import AirtimeBudget
from app.radio.framing import Deframer, encode_frame
from app.radio.sx126x import SX126x
from app.utils.logger import get_logger

log = get_logger("link")

# Extra settle time after each fragment, on top of the airtime estimate.
PACING_GUARD = 0.06

# A peer is "present" if heard from inside this many seconds.
PRESENCE_TIMEOUT = 15 * 60

# Sealed fragments remembered, so one recorded off the air and sent again
# is dropped rather than played twice. A few minutes of busy traffic.
REPLAY_MEMORY = 1024

# How long a sent message is kept to answer requests for missing pieces,
# and how often any one fragment may be sent again.
KEEP_SENT_SECONDS = 60.0
MAX_RESENDS = 2
# Two radios asking for the same broadcast fragment inside this window get
# one resend between them.
RESEND_GAP = 1.5


class NotPaired(Exception):
    """There is no key to seal a message to this station with."""


@dataclass
class Peer:
    """What we know about another radio, learned from the air."""

    addr: int
    name: str = ""
    last_heard: float = 0.0
    last_rssi: int | None = None
    messages: int = 0
    # Handshake state. `linked_at` is set when the station answered our
    # hello, or accepted theirs -- proof the link carries both ways.
    linked_at: float = 0.0
    called_at: float = 0.0
    rejected: bool = False

    @property
    def seconds_since_heard(self) -> float:
        return time.time() - self.last_heard if self.last_heard else float("inf")

    @property
    def present(self) -> bool:
        return self.seconds_since_heard < PRESENCE_TIMEOUT

    @property
    def link_state(self) -> str:
        if self.rejected:
            return protocol.LINK_REJECTED
        if self.linked_at:
            if self.seconds_since_heard < protocol.LINK_TIMEOUT:
                return protocol.LINK_LINKED
            return protocol.LINK_STALE
        if self.called_at and time.monotonic() - self.called_at < 30:
            return protocol.LINK_CALLING
        return protocol.LINK_UNLINKED

    @property
    def linked(self) -> bool:
        return self.link_state == protocol.LINK_LINKED


@dataclass
class Stats:
    packets_tx: int = 0
    packets_rx: int = 0
    bytes_tx: int = 0
    bytes_rx: int = 0
    frames_dropped: int = 0
    messages_rx: int = 0
    self_addressed_drops: int = 0
    overheard: int = 0
    address_clashes: int = 0
    other_channel: int = 0
    unsealed_refused: int = 0
    no_key: int = 0
    failed_to_open: int = 0
    replays: int = 0
    config_mode_replies: int = 0
    repairs_asked: int = 0
    fragments_resent: int = 0
    delivered_with_gaps: int = 0
    last_rssi: int | None = None
    airtime_used: float = 0.0
    queue_depth: int = 0
    errors: list = field(default_factory=list)


class LoraLink:
    """Reliable-ish message transport over the SX126X."""

    def __init__(self, radio: SX126x, air_speed: int = 9600,
                 duty_cycle_percent: float = 1.0, callsign: str = "",
                 addr: int | None = None, token: bytes | None = None,
                 keyring=None, channel: int = protocol.DEFAULT_CHANNEL):
        self.radio = radio
        self.callsign = callsign
        self.addr = (radio.addr if addr is None else addr) & 0xFFFF
        self.token = token or os.urandom(protocol.TOKEN_SIZE)
        self.keyring = keyring
        self.channel = channel
        self._ff_run = 0
        self._seen = deque(maxlen=REPLAY_MEMORY)
        self._seen_set = set()
        self.budget = AirtimeBudget(air_speed, duty_cycle_percent)
        self.stats = Stats()
        self.peers: dict = {}

        self._deframer = Deframer()
        # One full fragment on the air, plus the pause the sender leaves.
        full_frame = len(encode_frame(bytes(protocol.HEADER_SIZE + protocol.MAX_BODY)))
        self._reassembler = protocol.Reassembler(
            fragment_seconds=self.budget.estimate(full_frame) + PACING_GUARD)
        self._reassembly = threading.Condition()
        # msg_id -> [sent_at, dst, packets, {seq: (resends, last_at)}]
        self._sent = {}
        # From a random start, so a radio that restarts does not reuse the
        # numbers its last session's messages still hold on other radios.
        start = random.randrange(256)
        self._msg_ids = itertools.cycle([(start + i) % 256 for i in range(256)])
        self._tx = queue.Queue()
        self._threads = []
        self._running = threading.Event()
        self._on_message = None
        self._on_tx_progress = None
        self._on_hello = None
        self._on_clash = None
        self._peers_lock = threading.Lock()

    # --- callbacks -----------------------------------------------------
    def on_message(self, callback):
        """callback(message: protocol.Message, peer: Peer)"""
        self._on_message = callback

    def on_tx_progress(self, callback):
        """callback(sent: int, total: int) -- for the sending progress bar."""
        self._on_tx_progress = callback

    def on_hello(self, callback):
        """callback(peer, name) -> bool: accept this station's handshake?

        Called on the receive thread when another radio calls us. Return
        True to answer and link, False to refuse. Returning None leaves
        the decision pending, for a UI that wants to ask the operator.
        """
        self._on_hello = callback

    def on_clash(self, callback):
        """callback(name): another radio is using our Device ID.

        Only a pairing beacon can reveal this, because only it carries a
        token: any other packet with our address on it looks exactly like
        our own transmission echoed back. Called on the receive thread.
        """
        self._on_clash = callback

    def set_address(self, addr: int):
        """Take a new Device ID. Effective from the next packet, both ways."""
        self.addr = addr & 0xFFFF
        log.info("device id is now %d", self.addr)

    def set_channel(self, channel: int):
        self.channel = channel
        log.info("privacy channel is now %d", channel)

    # --- lifecycle -----------------------------------------------------
    def start(self):
        if self._running.is_set():
            return
        self._running.set()
        for name, target in (("lora-rx", self._rx_loop), ("lora-tx", self._tx_loop),
                             ("lora-reassembly", self._reassembly_loop)):
            thread = threading.Thread(target=target, name=name, daemon=True)
            thread.start()
            self._threads.append(thread)
        log.info("link up (callsign %r, addr %d)", self.callsign, self.addr)

    def stop(self):
        self._running.clear()
        self._tx.put(None)          # unblock the tx thread
        self.radio.wake_reader()    # unblock the rx thread
        with self._reassembly:
            self._reassembly.notify_all()
        for thread in self._threads:
            thread.join(timeout=1.5)
        self._threads = []

    # --- receive -------------------------------------------------------
    def _rx_loop(self):
        while self._running.is_set():
            data = self.radio.read_blocking()
            if not data:
                if self._running.is_set():
                    time.sleep(0.2)  # port hiccup: back off rather than spin
                continue
            self.stats.bytes_rx += len(data)
            self._watch_for_config_mode(data)
            for payload, rssi_byte in self._deframer.feed(data):
                self._handle_payload(payload, rssi_byte)
            self.stats.frames_dropped = self._deframer.frames_bad
            # The module appends its RSSI report after the packet, and it
            # often arrives in a later read than the frame it describes.
            # Rather than delay every message waiting for it, take it as
            # the channel's most recent reading.
            if self._deframer.last_rssi_byte is not None:
                self.stats.last_rssi = -(256 - self._deframer.last_rssi_byte)

    def _watch_for_config_mode(self, data: bytes):
        """Count FF FF FF: what a module in configuration mode says.

        With M1 high the module takes every write as a malformed setting
        and answers FF FF FF instead of transmitting it -- the only sign,
        from this side of the UART, that nothing is going on the air and
        nothing will be heard. On the Whisplay stack M1 is the LCD's DC
        line, which the stock driver leaves high after drawing.
        """
        for byte in data:
            # Replies arrive back to back, with nothing between them, so a
            # run of FFs is counted in threes rather than once.
            self._ff_run = self._ff_run + 1 if byte == 0xFF else 0
            if self._ff_run and self._ff_run % 3 == 0:
                self.stats.config_mode_replies += 1
                if self.stats.config_mode_replies == 3:
                    log.error(
                        "the radio module is in configuration mode (it answers "
                        "FF FF FF to what we send): nothing goes on the air and "
                        "nothing is heard. M1 is held high -- on the Whisplay HAT "
                        "that is the LCD's DC line; apply docs/whisplay-dc-fix.patch "
                        "(the installer does) and restart whisplay-daemon")

    def _handle_payload(self, payload: bytes, rssi_byte):
        rssi = -(256 - rssi_byte) if rssi_byte is not None else None
        packet = protocol.decode(payload, rssi_dbm=rssi)
        if packet is None:
            self.stats.frames_dropped += 1
            return
        if packet.channel != self.channel and packet.type not in protocol.PAIRING_TYPES:
            # Somebody else's conversation on the same frequency.
            self.stats.other_channel += 1
            return
        if packet.src == self.addr:
            if packet.type == protocol.PAIR:
                token, _public, name = protocol.parse_pair(packet.body)
                if token != self.token:
                    self._report_clash(name)
                    return
            # Normally our own broadcast heard back through a repeater. But
            # it is also what a second node misconfigured with our address
            # looks like -- and then this line silently eats every message
            # it sends, with nothing in the log to explain the silence.
            self.stats.self_addressed_drops += 1
            if self.stats.self_addressed_drops in (1, 10, 100):
                log.warning(
                    "dropped a %s packet claiming our own address (%d): either a "
                    "repeater echo, or another node is configured with the same "
                    "Device ID -- pairing detects and fixes that",
                    protocol.TYPE_NAMES.get(packet.type, packet.type), packet.src,
                )
            return

        if not packet.addressed_to(self.addr):
            # Every radio hears every packet now. One meant for somebody
            # else still proves its sender is on the air, but it is not
            # ours to deliver.
            self.stats.overheard += 1
            self._touch_peer(packet.src, rssi)
            return

        if not self._unseal(packet):
            return

        self.stats.packets_rx += 1
        if rssi is not None:
            self.stats.last_rssi = rssi
        peer = self._touch_peer(packet.src, rssi)

        with self._reassembly:
            message = self._reassembler.push(packet)
            self._reassembly.notify_all()   # a new or moved deadline
        if message is not None:
            self._deliver(message, peer)

    def _unseal(self, packet) -> bool:
        """Open a sealed body in place. False if the packet must be dropped."""
        if packet.sealing == protocol.CLEAR:
            if self.keyring is not None and packet.type not in protocol.CLEAR_TYPES:
                # Content in the clear: from a radio that never paired, or
                # an attempt to talk past the encryption. Either way, unread.
                self.stats.unsealed_refused += 1
                return False
            return True
        if self.keyring is None:
            self.stats.no_key += 1
            return False
        if packet.sealing == protocol.PAIRWISE:
            key = self.keyring.pairwise(packet.src)
        elif packet.sealing == protocol.SENDER:
            key = self.keyring.peer_broadcast(packet.src)
        else:
            key = None
        if key is None:
            # A radio we have not paired with, talking to its own contacts.
            self.stats.no_key += 1
            return False
        plain = crypto.open_sealed(key, packet.header, packet.body)
        if plain is None:
            self.stats.failed_to_open += 1
            if self.stats.failed_to_open in (1, 10, 100):
                log.warning("a packet from %d failed to open: altered, or its "
                            "keys changed -- pair again if this persists",
                            packet.src)
            return False
        marker = (packet.src, crypto.salt_of(packet.body), packet.seq)
        if marker in self._seen_set:
            self.stats.replays += 1
            return False
        if len(self._seen) == self._seen.maxlen:
            self._seen_set.discard(self._seen[0])
        self._seen.append(marker)
        self._seen_set.add(marker)
        packet.body = plain
        return True

    def _report_clash(self, name: str):
        self.stats.address_clashes += 1
        log.warning("%s is also using Device ID %d", name or "another radio", self.addr)
        if self._on_clash:
            try:
                self._on_clash(name)
            except Exception:
                log.exception("clash handler failed")

    def _deliver(self, message, peer: Peer):
        if message.type == protocol.REPAIR:
            self._resend(message)          # plumbing, not for the app
            return
        self.stats.messages_rx += 1
        peer.messages += 1
        if message.missing:
            self.stats.delivered_with_gaps += 1

        if message.type in (protocol.HELLO, protocol.HELLO_ACK):
            name = message.body.decode("utf-8", "replace").strip()[:20]
            if name:
                peer.name = name
        elif message.type == protocol.PAIR:
            _token, _public, name = protocol.parse_pair(message.body)
            if name:
                peer.name = name

        if message.type == protocol.HELLO:
            # Answer the handshake here, but still hand the message up:
            # the app wants to refresh the roster and tell the operator
            # who just called.
            self._handle_hello(peer)
        elif message.type == protocol.HELLO_ACK:
            peer.linked_at = time.time()
            peer.rejected = False
            log.info("linked with %s (%d)", peer.name or "?", peer.addr)
        elif message.type == protocol.REJECT:
            peer.rejected = True
            peer.linked_at = 0.0
            log.warning("%s (%d) refused the link", peer.name or "?", peer.addr)
        log.info(
            "rx %s from %d (%s) %d B%s",
            message.type_name, message.src, peer.name or "unknown",
            len(message.body),
            f", missing {message.missing}" if message.missing else "",
        )
        if self._on_message:
            try:
                self._on_message(message, peer)
            except Exception:
                log.exception("message handler failed")

    def _handle_hello(self, peer: Peer):
        """Another station is calling us. Answer, refuse, or defer."""
        decision = True
        if self._on_hello is not None:
            try:
                decision = self._on_hello(peer, peer.name)
            except Exception:
                log.exception("hello handler failed")
                decision = False
        if decision is None:
            log.info("%s (%d) is calling; waiting for the operator",
                     peer.name or "?", peer.addr)
            return
        if decision:
            peer.linked_at = time.time()
            peer.rejected = False
            self.send_hello_ack(peer.addr)
            log.info("accepted %s (%d)", peer.name or "?", peer.addr)
        else:
            self.send_reject(peer.addr)
            log.info("refused %s (%d)", peer.name or "?", peer.addr)

    def accept(self, addr: int):
        """Answer a deferred handshake, e.g. after the operator agreed."""
        peer = self._touch_peer(addr, None)
        peer.linked_at = time.time()
        peer.rejected = False
        self.send_hello_ack(addr)

    def refuse(self, addr: int):
        peer = self._touch_peer(addr, None)
        peer.rejected = True
        peer.linked_at = 0.0
        self.send_reject(addr)

    def link_state(self, addr: int) -> str:
        if addr == protocol.BROADCAST:
            return protocol.LINK_LINKED  # broadcast needs no handshake
        peer = self.peers.get(addr)
        return peer.link_state if peer else protocol.LINK_UNLINKED

    def tick(self):
        """Attend to half-received messages now. The reassembly thread does
        this by itself; calling it as well is harmless."""
        self._service_partials()

    # --- missing fragments ---------------------------------------------
    def _reassembly_loop(self):
        while self._running.is_set():
            with self._reassembly:
                deadline = self._reassembler.next_deadline()
                wait = None if deadline is None else max(0.0, deadline - time.monotonic())
                # No partials: sleep until a fragment arrives, at no cost.
                self._reassembly.wait(wait)
            if self._running.is_set():
                self._service_partials()

    def _service_partials(self):
        asks, finished = [], []
        now = time.monotonic()
        with self._reassembly:
            for key, partial in self._reassembler.due(now):
                src, msg_id, _type = key
                too_old = now - partial.last_seen >= self._reassembler.timeout
                if (partial.repairs < protocol.REPAIR_ROUNDS and not too_old
                        and self.can_send(src, protocol.REPAIR)):
                    missing = partial.missing
                    wait = (protocol.REPAIR_WAIT
                            + protocol.REPAIR_WAIT_PER_FRAGMENT * len(missing))
                    self._reassembler.postpone(key, wait)
                    asks.append((src, msg_id, missing))
                else:
                    finished.append(self._reassembler.finish(key))
        for src, msg_id, missing in asks:
            log.info("asking %d for %d missing fragment(s) of message %d: %s",
                     src, len(missing), msg_id, missing)
            self.stats.repairs_asked += 1
            _id, packets = self._fragments(
                protocol.REPAIR, src, bytes([msg_id]) + bytes(missing[:protocol.MAX_BODY - 64]))
            self._enqueue(src, packets, f"repair-ask/{msg_id}", report=False)
        for message in finished:
            if message is None:
                continue
            peer = self._touch_peer(message.src, message.rssi_dbm)
            log.info("delivering %s from %d with %d fragment(s) missing: %s",
                     message.type_name, message.src, len(message.missing), message.missing)
            self._deliver(message, peer)

    def _remember(self, msg_id: int, dst: int, packets: list):
        now = time.monotonic()
        for old in [m for m, entry in self._sent.items()
                    if now - entry[0] > KEEP_SENT_SECONDS]:
            del self._sent[old]
        if len(packets) > 1:
            self._sent[msg_id] = [now, dst, packets, {}]

    def _resend(self, message):
        """Someone asked for fragments of a message of ours again."""
        if not message.body:
            return
        msg_id, wanted = message.body[0], message.body[1:]
        entry = self._sent.get(msg_id)
        now = time.monotonic()
        if entry is None or now - entry[0] > KEEP_SENT_SECONDS:
            return
        _at, dst, packets, counts = entry
        if dst not in (message.src, protocol.BROADCAST):
            return  # not a message they were meant to have
        again = []
        for seq in sorted(set(wanted)):
            if seq >= len(packets):
                continue
            resends, last = counts.get(seq, (0, -RESEND_GAP))
            if resends >= MAX_RESENDS or now - last < RESEND_GAP:
                continue
            counts[seq] = (resends + 1, now)
            again.append(packets[seq])
        if again:
            self.stats.fragments_resent += len(again)
            log.info("resending %d fragment(s) of message %d for %d",
                     len(again), msg_id, message.src)
            self._enqueue(dst, again, f"resend/{msg_id}", report=False)

    def _touch_peer(self, addr: int, rssi) -> Peer:
        with self._peers_lock:
            peer = self.peers.get(addr)
            if peer is None:
                peer = Peer(addr=addr)
                self.peers[addr] = peer
            peer.last_heard = time.time()
            if rssi is not None:
                peer.last_rssi = rssi
        return peer

    # --- transmit ------------------------------------------------------
    def _enqueue(self, dst: int, packets: list, label: str, report: bool = True):
        # `report` is for the operator's progress bar and "sent" cue, which
        # belong to messages they sent -- not to handshakes and beacons.
        self._tx.put((dst, packets, label, report))
        self.stats.queue_depth = self._tx.qsize()

    def _sealing(self, type_: int, dst: int):
        """(sealing, key) for a message; raises NotPaired if there is no key."""
        if self.keyring is None or type_ in protocol.CLEAR_TYPES:
            return protocol.CLEAR, None
        if dst == protocol.BROADCAST:
            return protocol.SENDER, self.keyring.broadcast_key
        key = self.keyring.pairwise(dst)
        if key is None:
            raise NotPaired(dst)
        return protocol.PAIRWISE, key

    def can_send(self, dst: int, type_: int = protocol.TEXT) -> bool:
        try:
            self._sealing(type_, dst)
        except NotPaired:
            return False
        return True

    def plan(self, dst: int, size: int) -> tuple:
        """(fragments, bytes on the air) for a `size`-byte voice message."""
        sealing, _key = self._sealing(protocol.VOICE, dst)
        overhead = crypto.OVERHEAD if sealing != protocol.CLEAR else 0
        per_fragment = min(protocol.MAX_BODY - overhead, protocol.VOICE_CHUNK)
        fragments = max(1, -(-size // per_fragment))
        framing = len(encode_frame(b"")) + protocol.HEADER_SIZE + overhead
        return fragments, size + fragments * framing

    def _fragments(self, type_: int, dst: int, body: bytes, flags: int = 0):
        sealing, key = self._sealing(type_, dst)
        msg_id = next(self._msg_ids)
        packets = protocol.fragment(
            type_, self.addr, msg_id, body, flags=flags, dst=dst,
            channel=self.channel, sealing=sealing,
            seal=(lambda header, chunk: crypto.seal(key, header, chunk)) if key else None,
            overhead=crypto.OVERHEAD if key else 0,
            chunk=protocol.VOICE_CHUNK if type_ == protocol.VOICE else None,
        )
        if type_ in (protocol.VOICE, protocol.TEXT):
            self._remember(msg_id, dst, packets)
        return msg_id, packets

    def send_text(self, dst: int, text: str) -> int:
        body = text.encode("utf-8")[: protocol.MAX_BODY * 255]
        msg_id, packets = self._fragments(protocol.TEXT, dst, body)
        self._enqueue(dst, packets, f"text/{msg_id}")
        return msg_id

    def send_voice(self, dst: int, encoded: bytes, codec_mode: int) -> int:
        msg_id, packets = self._fragments(protocol.VOICE, dst, encoded, flags=codec_mode)
        self._enqueue(dst, packets, f"voice/{msg_id}")
        return msg_id

    def _send_named(self, type_: int, dst: int, label: str) -> int:
        name = self.callsign.encode("utf-8")[: protocol.MAX_BODY - crypto.OVERHEAD]
        msg_id, packets = self._fragments(type_, dst, name)
        self._enqueue(dst, packets, label, report=False)
        return msg_id

    def send_pair(self) -> int:
        """Announce that this radio is pairing, to anyone else who is."""
        public = self.keyring.public if self.keyring is not None else b""
        msg_id, packets = self._fragments(
            protocol.PAIR, protocol.BROADCAST,
            protocol.pair_body(self.token, public, self.callsign))
        self._enqueue(protocol.BROADCAST, packets, "pair", report=False)
        return msg_id

    def send_pairing(self, type_: int, dst: int, body: bytes) -> int:
        """A pairing request or answer; its body is sealed by the keyring."""
        msg_id, packets = self._fragments(type_, dst, body)
        self._enqueue(dst, packets, protocol.TYPE_NAMES[type_], report=False)
        return msg_id

    def mark_linked(self, addr: int):
        """Pairing completed both ways: as good as an answered hello."""
        peer = self._touch_peer(addr, None)
        peer.linked_at = time.time()
        peer.rejected = False

    def send_hello(self, dst: int = protocol.BROADCAST) -> int:
        """Call a station. It is linked once it answers."""
        if dst != protocol.BROADCAST:
            peer = self._touch_peer_quiet(dst)
            peer.called_at = time.monotonic()
        return self._send_named(protocol.HELLO, dst, "hello")

    def send_hello_ack(self, dst: int) -> int:
        return self._send_named(protocol.HELLO_ACK, dst, "hello-ack")

    def send_reject(self, dst: int) -> int:
        return self._send_named(protocol.REJECT, dst, "reject")

    def _touch_peer_quiet(self, addr: int) -> Peer:
        """Get or create a peer without claiming we heard from it."""
        with self._peers_lock:
            peer = self.peers.get(addr)
            if peer is None:
                peer = Peer(addr=addr)
                self.peers[addr] = peer
            return peer

    def pending(self) -> int:
        return self._tx.qsize()

    def _tx_loop(self):
        while self._running.is_set():
            item = self._tx.get()
            if item is None:
                return
            dst, packets, label, report = item
            self.stats.queue_depth = self._tx.qsize()
            self._transmit(dst, packets, label, report)

    def _transmit(self, dst: int, packets: list, label: str, report: bool = True):
        total = len(packets)
        for index, packet in enumerate(packets):
            if not self._running.is_set():
                return
            frame = encode_frame(packet)

            wait = self.budget.wait_seconds(len(frame))
            if wait > 0:
                if wait > 60:
                    # Refusing beats stalling for an hour with no feedback.
                    log.warning(
                        "%s dropped: duty cycle exhausted, %.0fs until it fits",
                        label, wait,
                    )
                    self.stats.errors.append("duty cycle full")
                    return
                log.info("holding %s for %.1fs to stay inside duty cycle", label, wait)
                time.sleep(wait)

            try:
                # A module broadcast whatever the destination: the header
                # carries `dst`, and receivers filter on it themselves.
                self.radio.send(protocol.BROADCAST, frame)
            except Exception:
                log.exception("transmit failed for %s", label)
                self.stats.errors.append("tx failed")
                return

            airtime = self.budget.record(len(frame))
            self.stats.airtime_used = self.budget.used_seconds()
            self.stats.packets_tx += 1
            self.stats.bytes_tx += len(frame)

            if report and self._on_tx_progress:
                try:
                    self._on_tx_progress(index + 1, total)
                except Exception:
                    log.debug("tx progress handler failed", exc_info=True)

            # Let the packet clear the air before loading the next one.
            if index + 1 < total:
                time.sleep(airtime + PACING_GUARD)

        log.info("sent %s: %d packet(s) to %s", label, total,
                 "all" if dst == protocol.BROADCAST else dst)

    @property
    def reassembling(self) -> int:
        """Open partial messages. Zero means no timer needs to run."""
        return self._reassembler.pending
