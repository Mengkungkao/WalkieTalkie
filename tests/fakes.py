"""Fake SX126X modules wired to each other over a fake channel.

These model the module, not just a serial port, because the module is
not transparent in the way a naive fake would suggest. In fixed-point
transmission mode it **consumes the first three bytes of every write**
(destination address high, low, and channel) and puts only the rest on
the air; with RSSI reporting enabled it **appends one byte** to every
packet it delivers. Both behaviours are what the framing layer has to
cope with, so both belong in the fake -- a fake that just forwarded
bytes would let a broken deframer pass.
"""

from __future__ import annotations

import threading
from collections import deque

# Address the module treats as "everyone".
BROADCAST = 0xFFFF

# What the fake reports as signal strength: byte b means -(256 - b) dBm.
DEFAULT_RSSI_BYTE = 0xA5  # -91 dBm


class FakeModule:
    """One SX126X, as seen through its UART."""

    def __init__(self, name: str = "module", addr: int = 0,
                 rssi_byte: int | None = DEFAULT_RSSI_BYTE):
        self.name = name
        self.addr = addr
        self.rssi_byte = rssi_byte
        self.peers = []
        self._buffer = deque()
        self._cond = threading.Condition()
        self._closed = False
        self._cancelled = False
        self.written = bytearray()      # everything the host handed us
        self.transmitted = []           # what actually went on the air

    @staticmethod
    def pair(rssi_byte: int | None = DEFAULT_RSSI_BYTE):
        left = FakeModule("left", addr=1, rssi_byte=rssi_byte)
        right = FakeModule("right", addr=2, rssi_byte=rssi_byte)
        left.peers.append(right)
        right.peers.append(left)
        return left, right

    @staticmethod
    def network(count: int, rssi_byte: int | None = DEFAULT_RSSI_BYTE):
        """`count` modules that all hear each other, addressed 1..count."""
        modules = [FakeModule(f"node{i}", addr=i, rssi_byte=rssi_byte)
                   for i in range(1, count + 1)]
        for module in modules:
            module.peers.extend(m for m in modules if m is not module)
        return modules

    # --- pyserial surface ---------------------------------------------
    @property
    def in_waiting(self) -> int:
        with self._cond:
            return len(self._buffer)

    def write(self, data: bytes) -> int:
        self.written.extend(data)
        if len(data) < 3:
            return len(data)
        dst = (data[0] << 8) | data[1]
        payload = data[3:]
        self.transmitted.append(payload)
        self._air(dst, payload)
        return len(data)

    def _air(self, dst: int, payload: bytes):
        for peer in self.peers:
            if dst in (BROADCAST, peer.addr) or peer is self:
                peer._receive(payload)

    def _receive(self, payload: bytes):
        with self._cond:
            if self._closed:
                return
            self._buffer.extend(payload)
            if self.rssi_byte is not None:
                self._buffer.append(self.rssi_byte)
            self._cond.notify_all()

    def flush(self):
        pass

    def read(self, size: int = 1) -> bytes:
        out = bytearray()
        with self._cond:
            while len(out) < size:
                while not self._buffer and not self._closed and not self._cancelled:
                    self._cond.wait(timeout=2.0)
                    if not self._buffer:
                        break
                if self._closed or self._cancelled or not self._buffer:
                    break
                out.append(self._buffer.popleft())
        return bytes(out)

    def reset_input_buffer(self):
        with self._cond:
            self._buffer.clear()

    def cancel_read(self):
        with self._cond:
            self._cancelled = True
            self._cond.notify_all()

    def close(self):
        with self._cond:
            self._closed = True
            self._cond.notify_all()

    # --- test helpers ---------------------------------------------------
    def inject(self, data: bytes):
        """Deliver bytes as if they arrived off the air."""
        self._receive(data)


class LossyModule(FakeModule):
    """Drops whole packets, the way a marginal RF link does."""

    def __init__(self, name: str = "lossy", addr: int = 1, drop_indices=()):
        super().__init__(name, addr=addr)
        self.drop_indices = set(drop_indices)
        self.packet_index = 0

    def _air(self, dst: int, payload: bytes):
        index = self.packet_index
        self.packet_index += 1
        if index in self.drop_indices:
            return  # transmitted into the void
        super()._air(dst, payload)
