"""The radio link: one object the rest of the app talks to.

Owns the driver, the framer, the reassembler and the duty-cycle budget,
and presents `send_text` / `send_voice` / `on_message`.

Two threads, both of which block rather than poll -- the whole reason
this app can sit on a battery all day:

* **rx** parks in a blocking `read(1)` on the serial port. Zero CPU
  until a byte physically arrives.
* **tx** parks in `queue.get()`. Zero CPU until something is queued.

The tx thread also paces fragments. The module buffers only one packet;
firing a six-fragment voice message at it back-to-back overruns that
buffer and the tail is silently dropped. So each fragment is followed by
a sleep of its own estimated airtime plus a guard, which is also where
the duty-cycle budget is charged and, when exhausted, waited out.
"""

from __future__ import annotations

import itertools
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

    @property
    def seconds_since_heard(self) -> float:
        return time.time() - self.last_heard if self.last_heard else float("inf")

    @property
    def present(self) -> bool:
        return self.seconds_since_heard < PRESENCE_TIMEOUT


@dataclass
class Stats:
    packets_tx: int = 0
    packets_rx: int = 0
    bytes_tx: int = 0
    bytes_rx: int = 0
    frames_dropped: int = 0
    messages_rx: int = 0
    last_rssi: int | None = None
    airtime_used: float = 0.0
    queue_depth: int = 0
    errors: list = field(default_factory=list)


class LoraLink:
    """Reliable-ish message transport over the SX126X."""

    def __init__(self, radio: SX126x, air_speed: int = 9600,
                 duty_cycle_percent: float = 1.0, callsign: str = ""):
        self.radio = radio
        self.callsign = callsign
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
        self._peers_lock = threading.Lock()

    # --- callbacks -----------------------------------------------------
    def on_message(self, callback):
        """callback(message: protocol.Message, peer: Peer)"""
        self._on_message = callback

    def on_tx_progress(self, callback):
        """callback(sent: int, total: int) -- for the sending progress bar."""
        self._on_tx_progress = callback

    # --- lifecycle -----------------------------------------------------
    def start(self):
        if self._running.is_set():
            return
        self._running.set()
        for name, target in (("lora-rx", self._rx_loop), ("lora-tx", self._tx_loop)):
            thread = threading.Thread(target=target, name=name, daemon=True)
            thread.start()
            self._threads.append(thread)
        log.info("link up (callsign %r, addr %d)", self.callsign, self.radio.addr)

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

    def _handle_payload(self, payload: bytes, rssi_byte):
        rssi = -(256 - rssi_byte) if rssi_byte is not None else None
        packet = protocol.decode(payload, rssi_dbm=rssi)
        if packet is None:
            self.stats.frames_dropped += 1
            return
        if packet.src == self.radio.addr:
            return  # our own broadcast heard back through a repeater

        self.stats.packets_rx += 1
        if rssi is not None:
            self.stats.last_rssi = rssi
        peer = self._touch_peer(packet.src, rssi)

        message = self._reassembler.push(packet)
        if message is not None:
            self._deliver(message, peer)

    def _deliver(self, message, peer: Peer):
        self.stats.messages_rx += 1
        peer.messages += 1
        if message.type == protocol.HELLO:
            name = message.body.decode("utf-8", "replace").strip()[:20]
            if name:
                peer.name = name
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
    def _enqueue(self, dst: int, packets: list, label: str):
        self._tx.put((dst, packets, label))
        self.stats.queue_depth = self._tx.qsize()

    def send_text(self, dst: int, text: str) -> int:
        msg_id = next(self._msg_ids)
        body = text.encode("utf-8")[: protocol.MAX_BODY * 255]
        packets = protocol.fragment(protocol.TEXT, self.radio.addr, msg_id, body)
        self._enqueue(dst, packets, f"text/{msg_id}")
        return msg_id

    def send_voice(self, dst: int, encoded: bytes, codec_mode: int) -> int:
        msg_id = next(self._msg_ids)
        packets = protocol.fragment(
            protocol.VOICE, self.radio.addr, msg_id, encoded, flags=codec_mode
        )
        self._enqueue(dst, packets, f"voice/{msg_id}")
        return msg_id

    def send_hello(self, dst: int = protocol.BROADCAST) -> int:
        msg_id = next(self._msg_ids)
        packets = protocol.fragment(
            protocol.HELLO, self.radio.addr, msg_id,
            self.callsign.encode("utf-8")[: protocol.MAX_BODY],
        )
        self._enqueue(dst, packets, "hello")
        return msg_id

    def pending(self) -> int:
        return self._tx.qsize()

    def _tx_loop(self):
        while self._running.is_set():
            item = self._tx.get()
            if item is None:
                return
            dst, packets, label = item
            self.stats.queue_depth = self._tx.qsize()
            self._transmit(dst, packets, label)

    def _transmit(self, dst: int, packets: list, label: str):
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
                self.radio.send(dst, frame)
            except Exception:
                log.exception("transmit failed for %s", label)
                self.stats.errors.append("tx failed")
                return

            airtime = self.budget.record(len(frame))
            self.stats.airtime_used = self.budget.used_seconds()
            self.stats.packets_tx += 1
            self.stats.bytes_tx += len(frame)

            if self._on_tx_progress:
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
