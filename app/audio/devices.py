"""Finding the HAT's microphone and speaker.

Kept separate from capture/playback because on this Pi the answer is
currently "there isn't one": the Whisplay sound card never finishes
probing, so `arecord -l` lists no capture hardware and ALSA has only the
HDMI output. The app has to start, run and stay useful in that state --
text messaging over LoRa works perfectly well without a codec -- so
device discovery returns None rather than raising, and the UI shows the
voice path as unavailable instead of dying at startup.

Everything is addressed through ALSA's `plughw:` rather than `hw:`, so
the plug layer resamples and down-mixes whatever the codec natively
supports to the 8 kHz mono Codec2 requires.
"""

from __future__ import annotations

import re
import subprocess

from app.utils.logger import get_logger

log = get_logger("audio-dev")

_CARD_LINE = re.compile(r"^card (\d+): (\S+)")

# Cards that exist but are useless for a walkie-talkie.
_IGNORED = ("vc4hdmi", "vc4-hdmi", "Loopback")


def _list_cards(command: str) -> list:
    try:
        output = subprocess.run(
            [command, "-l"], capture_output=True, text=True, timeout=5
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    cards = []
    for line in output.splitlines():
        match = _CARD_LINE.match(line)
        if match and not any(bad in match.group(2) for bad in _IGNORED):
            cards.append((int(match.group(1)), match.group(2)))
    return cards


def capture_cards() -> list:
    return _list_cards("arecord")


def playback_cards() -> list:
    return _list_cards("aplay")


def _pick(cards: list, preferred: str | None) -> str | None:
    if not cards:
        return None
    if preferred:
        for index, name in cards:
            if preferred.lower() in name.lower():
                return f"plughw:{index}"
    # Prefer anything that names itself whisplay before falling back.
    for index, name in cards:
        if "whisplay" in name.lower():
            return f"plughw:{index}"
    return f"plughw:{cards[0][0]}"


def resolve(configured: str | None, kind: str, preferred_card: str | None = None):
    """Return an ALSA device string, or None when there is no hardware.

    An explicit device in config.yaml is trusted as-is -- that is the
    escape hatch for USB dongles, `dsnoop` and Bluetooth headsets.
    """
    if configured and configured.lower() not in ("auto", ""):
        return configured
    cards = capture_cards() if kind == "capture" else playback_cards()
    device = _pick(cards, preferred_card)
    if device is None:
        log.warning(
            "no %s device found (cards seen: %s) -- voice %s is disabled",
            kind, cards or "none",
            "recording" if kind == "capture" else "playback",
        )
    else:
        log.info("%s device: %s (%s)", kind, device, cards)
    return device


def diagnose() -> str:
    """One-line summary for the status screen."""
    capture, playback = capture_cards(), playback_cards()
    if capture and playback:
        return f"mic {capture[0][1]} / spk {playback[0][1]}"
    if not capture and not playback:
        return "no audio hardware"
    return f"mic {'yes' if capture else 'NONE'} / spk {'yes' if playback else 'NONE'}"
