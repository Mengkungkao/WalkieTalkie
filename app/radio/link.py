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

The tx thread also paces fragments. The module buffers only one packet;
firing a six-fragment voice message at it back-to-back overruns that
buffer and the tail is silently dropped. So each fragment is followed by
a sleep of its own estimated airtime plus a guard, which is also where
the duty-cycle budget is charged and, when exhausted, waited out.
"""

from __future__ import annotations

import itertools
import os
import queue
import threading
import time
from dataclasses import dataclass, field

from app.radio import protocol
from app.radio.airtime import AirtimeBudget
from app.radio.framing import Deframer, encode_frame
from app.radio.sx126x import SX126x
from app.utils.logger import get_logger

log = get_logger("link")

# Extra settle time after each fragment, on top of the airtime estimate.
PACING_GUARD = 0.06

# A peer is "present" if heard from inside this many seconds.
PRESENCE_TIMEOUT = 15 * 60


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
    last_rssi: int | None = None
    airtime_used: float = 0.0
    queue_depth: int = 0
    errors: list = field(default_factory=list)


class LoraLink:
    """Reliable-ish message transport over the SX126X."""

    def __init__(self, radio: SX126x, air_speed: int = 9600,
                 duty_cycle_percent: float = 1.0, callsign: str = "",
                 addr: int | None = None, token: bytes | None = None):
        self.radio = radio
        self.callsign = callsign
        self.addr = (radio.addr if addr is None else addr) & 0xFFFF
        self.token = token or os.urandom(protocol.TOKEN_SIZE)
        self.budget = AirtimeBudget(air_speed, duty_cycle_percent)
        self.stats = Stats()
        self.peers: dict = {}

        self._deframer = Deframer()
        self._reassembler = protocol.Reassembler()
        self._msg_ids = itertools.cycle(range(256))
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

    # --- lifecycle -----------------------------------------------------
    def start(self):
        if self._running.is_set():
            return
        self._running.set()
        for name, target in (("lora-rx", self._rx_loop), ("lora-tx", self._tx_loop)):
            thread = threading.Thread(target=target, name=name, daemon=True)
            thread.start()
            self._threads.append(thread)
        log.info("link up (callsign %r, addr %d)", self.callsign, self.addr)

    def stop(self):
        self._running.clear()
        self._tx.put(None)          # unblock the tx thread
        self.radio.wake_reader()    # unblock the rx thread
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
            for payload, rssi_byte in self._deframer.feed(data):
                self._handle_payload(payload, rssi_byte)
            self.stats.frames_dropped = self._deframer.frames_bad
            # The module appends its RSSI report after the packet, and it
            # often arrives in a later read than the frame it describes.
            # Rather than delay every message waiting for it, take it as
            # the channel's most recent reading.
            if self._deframer.last_rssi_byte is not None:
                self.stats.last_rssi = -(256 - self._deframer.last_rssi_byte)

    def _handle_payload(self, payload: bytes, rssi_byte):
        rssi = -(256 - rssi_byte) if rssi_byte is not None else None
        packet = protocol.decode(payload, rssi_dbm=rssi)
        if packet is None:
            self.stats.frames_dropped += 1
            return
        if packet.src == self.addr:
            if packet.type == protocol.PAIR:
                token, name = protocol.parse_pair(packet.body)
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

        self.stats.packets_rx += 1
        if rssi is not None:
            self.stats.last_rssi = rssi
        peer = self._touch_peer(packet.src, rssi)

        message = self._reassembler.push(packet)
        if message is not None:
            self._deliver(message, peer)

    def _report_clash(self, name: str):
        self.stats.address_clashes += 1
        log.warning("%s is also using Device ID %d", name or "another radio", self.addr)
        if self._on_clash:
            try:
                self._on_clash(name)
            except Exception:
                log.exception("clash handler failed")

    def _deliver(self, message, peer: Peer):
        self.stats.messages_rx += 1
        peer.messages += 1

        if message.type in (protocol.HELLO, protocol.HELLO_ACK):
            name = message.body.decode("utf-8", "replace").strip()[:20]
            if name:
                peer.name = name
        elif message.type == protocol.PAIR:
            _token, name = protocol.parse_pair(message.body)
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
        """Flush partial messages whose sender went quiet. Cheap; call rarely."""
        for message in self._reassembler.expire():
            peer = self._touch_peer(message.src, message.rssi_dbm)
            log.info("flushing incomplete %s from %d", message.type_name, message.src)
            self._deliver(message, peer)

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

    def send_text(self, dst: int, text: str) -> int:
        msg_id = next(self._msg_ids)
        body = text.encode("utf-8")[: protocol.MAX_BODY * 255]
        packets = protocol.fragment(protocol.TEXT, self.addr, msg_id, body, dst=dst)
        self._enqueue(dst, packets, f"text/{msg_id}")
        return msg_id

    def send_voice(self, dst: int, encoded: bytes, codec_mode: int) -> int:
        msg_id = next(self._msg_ids)
        packets = protocol.fragment(
            protocol.VOICE, self.addr, msg_id, encoded, flags=codec_mode, dst=dst
        )
        self._enqueue(dst, packets, f"voice/{msg_id}")
        return msg_id

    def _send_named(self, type_: int, dst: int, label: str) -> int:
        msg_id = next(self._msg_ids)
        packets = protocol.fragment(
            type_, self.addr, msg_id,
            self.callsign.encode("utf-8")[: protocol.MAX_BODY], dst=dst,
        )
        self._enqueue(dst, packets, label, report=False)
        return msg_id

    def send_pair(self) -> int:
        """Announce that this radio is pairing, to anyone else who is."""
        msg_id = next(self._msg_ids)
        packets = protocol.fragment(
            protocol.PAIR, self.addr, msg_id,
            protocol.pair_body(self.token, self.callsign),
        )
        self._enqueue(protocol.BROADCAST, packets, "pair", report=False)
        return msg_id

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
