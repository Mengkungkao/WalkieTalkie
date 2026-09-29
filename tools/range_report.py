#!/usr/bin/env python3
"""Summarise range test logs (Home > Range test).

    python3 tools/range_report.py ~/.whisplay-walkie/rangetest/range-*.csv

Copy them off a radio first to read them on another machine:

    scp orangepi@192.168.0.130:.whisplay-walkie/rangetest/*.csv .

For each log: how many probes were answered, the signal both ways, and
the same between each pair of marks -- the clicks made along the walk --
so a stretch of the route can be read on its own. The first long run of
unanswered probes is where the link gave out. Voice messages sent and
received during the test are listed with what they lost.
"""

from __future__ import annotations

import argparse
import csv
import statistics
import sys
from pathlib import Path

# This many unanswered probes in a row is "the link gave out", not a
# packet lost to bad luck.
LOST_RUN = 3


def _int(value):
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _signal(values: list) -> str:
    values = [v for v in values if v is not None]
    if not values:
        return "--"
    return (f"median {statistics.median(values):.0f}, "
            f"worst {min(values)}, best {max(values)} dBm")


class Stretch:
    """Probes between two marks (or the start, or the end)."""

    def __init__(self, label: str):
        self.label = label
        self.sent = self.answered = 0
        self.down, self.up, self.rtt = [], [], []

    def add(self, row: dict):
        self.sent += 1
        if row.get("answered") == "1":
            self.answered += 1
            self.down.append(_int(row.get("rssi_down_dbm")))
            self.up.append(_int(row.get("rssi_up_dbm")))
            self.rtt.append(_int(row.get("rtt_ms")))

    def line(self) -> str:
        if not self.sent:
            return f"{self.label:<12} no probes"
        share = 100 * self.answered / self.sent
        rtt = [r for r in self.rtt if r is not None]
        return (f"{self.label:<12} {self.answered:>3}/{self.sent:<3} answered ({share:3.0f}%)"
                f"   here {_signal(self.down)}   there {_signal(self.up)}"
                + (f"   rtt median {statistics.median(rtt):.0f} ms" if rtt else ""))


def report(path: Path, out=sys.stdout) -> dict:
    with open(path, newline="") as handle:
        rows = list(csv.DictReader(handle))
    start = next((r for r in rows if r["event"] == "start"), {})
    print(f"\n== {path.name}", file=out)
    if start:
        print(f"   {start.get('time', '')}  {start.get('detail', '')}  "
              f"air {start.get('air_speed', '?')} bps", file=out)

    whole = Stretch("whole test")
    stretches = [Stretch("start")]
    run = longest = 0
    gave_out = None
    voice = []
    for row in rows:
        event = row["event"]
        if event == "probe":
            whole.add(row)
            stretches[-1].add(row)
            if row.get("answered") == "1":
                run = 0
            else:
                run += 1
                longest = max(longest, run)
                if run == LOST_RUN and gave_out is None:
                    gave_out = (row.get("elapsed_s"), stretches[-1].label)
        elif event == "mark":
            stretches.append(Stretch(f"after mark {row.get('mark')}"))
        elif event.startswith("voice"):
            voice.append(row)

    print(f"   {whole.line()}", file=out)
    if len(stretches) > 1:
        for stretch in stretches:
            print(f"     {stretch.line()}", file=out)
    skipped = sum(1 for r in rows if r["event"] == "skipped")
    if skipped:
        print(f"   {skipped} probe(s) skipped to stay inside the duty cycle", file=out)
    if gave_out:
        print(f"   link first gave out ({LOST_RUN} unanswered in a row) at "
              f"{gave_out[0]} s, {gave_out[1]}", file=out)
    print(f"   longest run unanswered: {longest}", file=out)
    for row in voice:
        print(f"   {row['elapsed_s']:>7} s  {row['event']:<13} {row.get('detail', '')}",
              file=out)
    return {"sent": whole.sent, "answered": whole.answered, "longest_lost": longest,
            "stretches": [(s.label, s.answered, s.sent) for s in stretches]}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("logs", nargs="+", type=Path)
    args = parser.parse_args()
    for path in args.logs:
        try:
            report(path)
        except (OSError, KeyError, csv.Error) as exc:
            print(f"! {path}: {exc}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
