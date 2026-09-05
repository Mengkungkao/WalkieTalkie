# Measured link baselines

Numbers from `tools/throughput.py`, so they can be compared after a
change of antenna, position or setting. Each run echoes every payload
back, so a figure covers both directions plus the far end's turnaround.

## 2026-09-05 — small antennas, both nodes on a desk

Pi 4B (addr 90) ⇄ Pi Zero 2 W (addr 51), 868 MHz, 9600 bps air,
displays running, apps stopped for the test.

| bytes | fragments | round trip | throughput | RSSI | result |
|---|---|---|---|---|---|
| 11 | 1 | 0.26 s | 43 B/s | −30 dBm | 8/8 |
| 32 | 1 | 0.39 s | 82 B/s | −30 dBm | 8/8 |
| 64 | 1 | 0.59 s | 108 B/s | −30 dBm | 8/8 |
| 128 | 1 | 0.99 s | 129 B/s | −30 dBm | 8/8 |
| 193 | 2 | 1.41 s | 136 B/s | −29 dBm | 8/8 |
| 386 | 3 | 2.57 s | 150 B/s | −28 dBm | 8/8 |
| 965 | 6 | 6.03 s | 160 B/s | −29 dBm | 8/8 |

**56 round trips, 0 lost. 120 packets each way, 0 dropped frames.**
Responder side: 56 received, 0 incomplete.

### What this does not prove

At −30 dBm the receiver is nowhere near its limit, so **this says
nothing about the antennas**. A poor antenna and a good one both give a
clean link across a desk; the difference only appears near the edge of
range. To compare antennas, walk one node away and record the distance
at which loss first appears, and the RSSI there.

Useful reference points from earlier in the session, same tools:

| condition | result |
|---|---|
| both apps running, voice traffic | ~2.6% of messages lost, one fragment each |
| app redrawing during reception | 6 of 10 messages missing a fragment |
| apps stopped, displays running | 0% across 56 round trips |

The middle row was the app redrawing while a message arrived — each
redraw clocks DC, which is the module's M1, so the radio went deaf for
about 11 ms at a time. Fixed by holding the screen still while a message
is in flight.

## How to repeat

```bash
# receiver
python3 tools/throughput.py --echo --address 51

# sender
python3 tools/throughput.py --ramp --to 51 --address 90 --repeat 8
```

Stop the app first (`pkill -f 'app[.]main'`) — it holds the serial port.
