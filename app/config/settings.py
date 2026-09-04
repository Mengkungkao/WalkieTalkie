"""Configuration: YAML file, environment overrides, sane defaults.

Every default here is chosen so the app starts and does something useful
on this specific Pi even with an empty config.yaml -- including the two
cases that are currently true of the hardware: no audio codec, and no
access to the M0/M1 mode pins.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field, fields
from pathlib import Path

from app.utils.logger import get_logger

log = get_logger("settings")

CONFIG_NAME = "config.yaml"
ENV_PREFIX = "WALKIE_"


@dataclass
class RadioSettings:
    port: str = "/dev/ttyS0"
    address: int = 5
    frequency_mhz: int = 868
    air_speed: int = 9600
    power_dbm: int = 22
    uart_baud: int = 9600
    duty_cycle_percent: float = 1.0
    # M0/M1. Left unset because GPIO 22 and 27 belong to the Whisplay LCD;
    # only fill this in if the mode pins have been rewired to free lines.
    mode_pins: list | None = None


@dataclass
class IdentitySettings:
    callsign: str = "whisplay"


@dataclass
class AudioSettings:
    capture_device: str = "auto"
    playback_device: str = "auto"
    preferred_card: str = "whisplay"
    codec_mode: str = "700C"
    max_record_seconds: float = 20.0
    cues: bool = True


@dataclass
class UiSettings:
    brightness: int = 80
    # The backlight is by far the biggest power draw on the HAT, so it
    # steps down twice while idle and comes back on any button or packet.
    idle_dim_seconds: float = 25.0
    idle_dim_brightness: int = 15
    idle_off_seconds: float = 120.0
    led_enabled: bool = True


@dataclass
class InputSettings:
    debounce_ms: int = 75
    # 700 ms sits in the empty band measured on this button between
    # deliberate multi-clicks (158-522 ms apart) and ordinary browsing
    # clicks (>= 1214 ms apart). 400 ms splits genuine quad-clicks.
    click_window_ms: int = 700
    # Push-to-talk starts this long after the press, well above the
    # 30-60 ms a real click lasts, and low enough to feel immediate.
    hold_ms: int = 350


@dataclass
class PowerSettings:
    # 0 disables periodic presence beacons entirely. Each one costs
    # airtime out of the duty-cycle budget, so it is off by default.
    beacon_interval_seconds: float = 0.0
    tick_seconds: float = 5.0


@dataclass
class Contact:
    name: str
    address: int

    @property
    def is_broadcast(self) -> bool:
        return self.address == 0xFFFF


@dataclass
class Settings:
    radio: RadioSettings = field(default_factory=RadioSettings)
    identity: IdentitySettings = field(default_factory=IdentitySettings)
    audio: AudioSettings = field(default_factory=AudioSettings)
    ui: UiSettings = field(default_factory=UiSettings)
    input: InputSettings = field(default_factory=InputSettings)
    power: PowerSettings = field(default_factory=PowerSettings)
    contacts: list = field(default_factory=list)
    source: str = "defaults"

    @property
    def data_dir(self) -> Path:
        path = Path(os.getenv(f"{ENV_PREFIX}DATA_DIR",
                              Path.home() / ".whisplay-walkie"))
        path.mkdir(parents=True, exist_ok=True)
        return path


def _coerce(target, values: dict):
    """Apply a dict onto a dataclass, ignoring unknown keys."""
    known = {f.name: f.type for f in fields(target)}
    for key, value in (values or {}).items():
        if key not in known:
            log.warning("ignoring unknown setting %s.%s", type(target).__name__, key)
            continue
        setattr(target, key, value)


def _apply_env(settings: Settings):
    """WALKIE_RADIO_PORT, WALKIE_IDENTITY_CALLSIGN, ... override the file."""
    sections = {
        "RADIO": settings.radio, "IDENTITY": settings.identity,
        "AUDIO": settings.audio, "UI": settings.ui,
        "INPUT": settings.input, "POWER": settings.power,
    }
    for name, section in sections.items():
        for spec in fields(section):
            key = f"{ENV_PREFIX}{name}_{spec.name.upper()}"
            raw = os.getenv(key)
            if raw is None:
                continue
            current = getattr(section, spec.name)
            try:
                if isinstance(current, bool):
                    value = raw.strip().lower() in ("1", "true", "yes", "on")
                elif isinstance(current, int):
                    value = int(raw)
                elif isinstance(current, float):
                    value = float(raw)
                else:
                    value = raw
            except ValueError:
                log.warning("%s=%r is not valid for %s", key, raw, spec.name)
                continue
            setattr(section, spec.name, value)
            log.info("%s overridden from environment", key)


def load(path: str | None = None) -> Settings:
    settings = Settings()
    candidate = Path(path) if path else Path(__file__).resolve().parents[2] / CONFIG_NAME

    raw = {}
    if candidate.is_file():
        try:
            import yaml

            raw = yaml.safe_load(candidate.read_text()) or {}
            settings.source = str(candidate)
        except ImportError:
            log.warning("PyYAML missing; using defaults (pip install pyyaml)")
        except Exception:
            log.exception("could not parse %s; using defaults", candidate)
    else:
        log.info("no %s found; using defaults", candidate)

    _coerce(settings.radio, raw.get("radio"))
    _coerce(settings.identity, raw.get("identity"))
    _coerce(settings.audio, raw.get("audio"))
    _coerce(settings.ui, raw.get("ui"))
    _coerce(settings.input, raw.get("input"))
    _coerce(settings.power, raw.get("power"))

    settings.contacts = [
        Contact(name=str(entry.get("name", f"node {entry.get('address')}")),
                address=int(entry["address"]))
        for entry in (raw.get("contacts") or [])
        if isinstance(entry, dict) and entry.get("address") is not None
    ]

    _apply_env(settings)

    if settings.radio.mode_pins and len(settings.radio.mode_pins) != 2:
        log.warning("radio.mode_pins must be [M0, M1]; ignoring %r",
                    settings.radio.mode_pins)
        settings.radio.mode_pins = None
    return settings
