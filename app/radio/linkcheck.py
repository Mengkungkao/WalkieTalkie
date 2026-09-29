"""Is each paired radio in range, and how well do we hear each other?

Paired radios ping each other every `interval` seconds (config.yaml:
radio.link_check_seconds) -- one small sealed packet per radio, to all
of them at once. A ping says how strongly its sender last heard every
other radio, so each side learns both directions of the link from one
packet each: how well it hears the other (`down`), and how well it is
heard (`up`). Anything else heard from a radio counts too.

A radio not heard for a couple of its intervals is *disconnected*: out
of range, switched off, or its app closed -- the same thing to someone
about to talk to it. A probe (a ping that asks for an answer) settles it
sooner, and is what the Talk screen and the range test send.

Pure bookkeeping, clocked by the caller, so it tests without a radio.
"""

from __future__ import annotations

from dataclasses import dataclass

IN_RANGE = "in range"
WEAK = "weak signal"
DISCONNECTED = "disconnected"
UNKNOWN = "not checked yet"

# Below this, either way, a link is one wall or one hill from failing.
# The bars in the header call it "weak" too (see theme.SIGNAL_FLOORS).
WEAK_RSSI = -105

# Missed pings before a radio counts as gone, plus a margin for a ping
# that waited for the duty cycle or went out late.
MISSED_INTERVALS = 2.0
MARGIN_SECONDS = 30.0
# Never sooner than this, however often pings are sent.
MIN_GRACE = 45.0

# An answer to a probe takes well under a second at 9.6k and about two at
# 1.2k; after this it is not coming.
PROBE_TIMEOUT = 5.0


@dataclass
class PeerLink:
    addr: int
    last_heard: float | None = None  # monotonic; None = not this session
    down: int | None = None          # dBm we last heard it at
    up: int | None = None            # dBm it last heard us at
    up_at: float = 0.0
    interval: float = 0.0            # its own ping interval, when it said
    probe_seq: int | None = None     # an unanswered probe, if any
    probe_at: float = 0.0
    probes: int = 0
    answers: int = 0
    rtt: float | None = None
    state: str = UNKNOWN


class LinkMonitor:
    def __init__(self, interval: float):
        self.interval = interval
        self.peers: dict = {}
        # When each radio started being watched: one never heard from
        # since is disconnected after the same grace as one gone quiet.
        self._watched_since: dict = {}

    def peer(self, addr: int) -> PeerLink:
        if addr not in self.peers:
            self.peers[addr] = PeerLink(addr)
        return self.peers[addr]

    def watch(self, addrs, now: float):
        """Watch exactly these radios: the paired ones."""
        keep = set(addrs)
        for addr in [a for a in self.peers if a not in keep]:
            del self.peers[addr]
            self._watched_since.pop(addr, None)
        for addr in keep:
            self.peer(addr)
            self._watched_since.setdefault(addr, now)

    # --- what was heard ------------------------------------------------------
    def heard(self, addr: int, rssi, now: float):
        """Any packet from `addr`: it is on the air, and this is its signal.

        Only radios being watched are kept: this runs on the receive
        thread, and adding one here would change the table under the
        main loop while it reads it.
        """
        link = self.peers.get(addr)
        if link is None:
            return
        link.last_heard = now
        if rssi is not None:
            link.down = rssi

    def ping(self, addr: int, ping, rssi, me: int, now: float):
        """A ping from `addr`, which also says how it hears us."""
        self.heard(addr, rssi, now)
        link = self.peers.get(addr)
        if link is None:
            return
        if ping.interval:
            link.interval = float(ping.interval)
        if me in ping.reports and ping.reports[me] is not None:
            link.up, link.up_at = ping.reports[me], now

    def probe_sent(self, addr: int, seq: int, now: float):
        link = self.peer(addr)
        link.probe_seq, link.probe_at = seq, now
        link.probes += 1

    def pong(self, addr: int, seq: int, rssi_at_them, rssi, now: float) -> float | None:
        """An answer to a probe. Returns the round trip, if it was ours."""
        self.heard(addr, rssi, now)
        link = self.peers.get(addr)
        if link is None:
            return None
        if rssi_at_them is not None:
            link.up, link.up_at = rssi_at_them, now
        if link.probe_seq != seq:
            return None
        link.probe_seq = None
        link.answers += 1
        link.rtt = now - link.probe_at
        return link.rtt

    # --- what it adds up to --------------------------------------------------
    def grace(self, link: PeerLink) -> float:
        interval = link.interval or self.interval or 0.0
        if not interval:
            return float("inf")
        return max(MIN_GRACE, MISSED_INTERVALS * interval + MARGIN_SECONDS)

    def state_of(self, addr: int, now: float) -> str:
        link = self.peers.get(addr)
        if link is None:
            return UNKNOWN
        unanswered = (link.probe_seq is not None
                      and now - link.probe_at >= PROBE_TIMEOUT
                      and (link.last_heard is None or link.probe_at >= link.last_heard))
        if link.last_heard is None:
            since = self._watched_since.get(addr)
            never = since is not None and now - since > self.grace(link)
            return DISCONNECTED if unanswered or never else UNKNOWN
        if unanswered or now - link.last_heard > self.grace(link):
            return DISCONNECTED
        weakest = min((r for r in (link.down, link.up) if r is not None), default=None)
        if weakest is not None and weakest < WEAK_RSSI:
            return WEAK
        return IN_RANGE

    def update(self, now: float) -> list:
        """(addr, old state, new state) for every radio whose state moved."""
        changes = []
        for addr, link in list(self.peers.items()):
            state = self.state_of(addr, now)
            if state != link.state:
                changes.append((addr, link.state, state))
                link.state = state
        return changes

    def next_change(self, now: float) -> float | None:
        """Seconds until a state could move on time alone, if ever."""
        soonest = None
        for addr, link in list(self.peers.items()):
            candidates = []
            if link.probe_seq is not None:
                candidates.append(link.probe_at + PROBE_TIMEOUT - now)
            if link.state != DISCONNECTED:
                since = (link.last_heard if link.last_heard is not None
                         else self._watched_since.get(addr))
                if since is not None:
                    candidates.append(since + self.grace(link) - now)
            for seconds in candidates:
                if seconds > 0 and (soonest is None or seconds < soonest):
                    soonest = seconds
        return soonest

    def summary(self, addr: int, now: float) -> str:
        """One line for a list row: state, and the signal both ways."""
        link = self.peers.get(addr)
        state = self.state_of(addr, now)
        if link is None or state == UNKNOWN:
            return UNKNOWN
        if state == DISCONNECTED:
            return f"{DISCONNECTED} · {ago(now - link.last_heard)}" \
                if link.last_heard is not None \
                else f"{DISCONNECTED} · not heard"
        return f"{state} · {signal_pair(link.down, link.up)}"


def signal_pair(down, up) -> str:
    """"-85/-91 dBm": how we hear them, then how they hear us."""
    def one(value):
        return "?" if value is None else str(value)
    return f"{one(down)}/{one(up)} dBm"


def ago(seconds: float) -> str:
    if seconds < 60:
        return f"{int(seconds)}s ago"
    if seconds < 3600:
        return f"{int(seconds // 60)}m ago"
    return f"{int(seconds // 3600)}h ago"
