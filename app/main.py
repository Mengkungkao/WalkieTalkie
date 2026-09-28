"""LoRa Walkie-Talkie: the app itself.

A push-to-talk voice and text terminal for the Whisplay HAT and a
Waveshare SX126X LoRa HAT on one Pi Zero 2 W.

    hold the button   record, then encode and transmit
    1 click           next item / next screen
    2 clicks          open, play, or go back
    3 clicks          replay the last voice message
    4 clicks          leave the app

**The main loop does not poll.** It renders, works out when the next
thing genuinely needs to happen, and blocks on an Event until either
that deadline or a real event -- a button edge, a received packet, a
transmit-progress update -- wakes it. When the radio is quiet, the
screen has dimmed and nothing is being reassembled, that wait is
indefinite: the process makes no wakeups at all, the serial reader is
parked in a kernel read, and the transmit thread is parked on an empty
queue. Idle cost is as close to zero as Python allows, which is the
whole point on a device meant to sit on a belt all day.
"""

from __future__ import annotations

import dataclasses
import signal
import threading
import time

from app import board as board_module
from app.audio import devices as audio_devices
from app.audio.capture import CLIPPED_TOO_MUCH, Recorder
from app.audio.codec2 import (Codec2, Codec2Unavailable, MODE_BY_NAME,
                              NAME_BY_MODE, SAMPLE_RATE)
from app.audio.playback import CUE_ERROR, Player, cues_for, voice_for
from app.config import settings as settings_module
from app.config.settings import Contact
from app.input.button import DOUBLE, QUAD, SINGLE, TRIPLE, GestureDetector
from app.radio import protocol
from app.radio import modepins
from app.config.settings import hostname_callsign
from app.radio.link import LoraLink, NotPaired
from app.radio.sx126x import SX126x, port_conflicts
from app.store.inbox import Inbox
from app.store.keyring import Keyring
from app.store.overrides import Overrides
from app.store.roster import BROADCAST_NAME, Roster
from app.ui import navigation, screens, theme
from app.ui.display import Display
from app.ui.editors import (ChoiceEditor, ClockEditor, ConfirmEditor,
                            DigitEditor)
from app.ui.screens import (CONTACTS, EDIT, HOME, IDLE, INBOX, PAIR, PLAYING,
                            RECEIVING, RECORDING, SENDING, SETTINGS, START,
                            STATUS, TALK, ViewState)
from app.utils import battery, clock
from app.utils.logger import get_logger
from app.utils.single_instance import AlreadyRunning, SingleInstance

log = get_logger("main")

# Refresh cadence while something is visibly moving.
#
# Only recording animates. Every other radio state draws a static frame,
# and redrawing it costs more than it shows: a full 240x280 push clocks
# DC for about 11 ms, and DC is the module's M1 on this stack, so the
# radio is deaf for the duration. Receiving used to refresh every 300 ms,
# which over a three-fragment message meant roughly five redraws and 10%
# of the message's air time spent deaf -- the app was reliably deafening
# itself exactly while being spoken to.
FRAME_INTERVAL = {RECORDING: 0.08}

# A press shorter than this after the hold threshold is a slip, not speech.
MIN_TALK_SECONDS = 0.4

# How often to re-call an unlinked station while its Talk page is open.
RECALL_SECONDS = 45.0

# Pairing. A beacon is ~20 bytes -- about 50 ms of air at 9600 bps -- so
# a full two-minute window costs a few seconds of the hour's 36.
PAIR_BEACON_SECONDS = 3.0
PAIR_WINDOW_SECONDS = 120.0
# How long to wait for the other operator to accept before saying so.
PAIR_ANSWER_SECONDS = 30.0
# At most one reply a radio that is not pairing sends to a clashing one.
CLASH_REPLY_SECONDS = 10.0

# Settings > Voice quality: Codec2 modes, clearest first. Scored with STOI
# (0-1, intelligibility) on recorded speech through the whole path:
# 3200 0.866, 1600 0.83, 700C 0.73.
VOICE_QUALITIES = (("Clear", "3200"), ("Balanced", "1600"), ("Most messages", "700C"))

# Names offered under Settings > Name, after this machine's hostname.
CALLSIGNS = ("Alpha", "Bravo", "Charlie", "Delta", "Echo", "Foxtrot", "Golf",
             "Hotel", "India", "Juliet", "Kilo", "Lima", "Mike", "November",
             "Oscar", "Papa", "Quebec", "Romeo", "Sierra", "Tango", "Uniform",
             "Victor", "Whiskey", "X-ray", "Yankee", "Zulu")


class WalkieApp:
    # Defaults for state that __init__ would otherwise have to set before
    # anything can run; tests build the app without hardware via __new__.
    link = None
    keyring = None
    _pending_pair = None
    _parents = None
    _target = (protocol.BROADCAST, BROADCAST_NAME)
    _pairing = False
    _pairing_until = 0.0
    _pairing_with = None
    _pair_found = None
    _pair_beacon_due = 0.0
    _last_clash_reply = -CLASH_REPLY_SECONDS
    _return_screen = SETTINGS
    _warned_config_mode = False

    def __init__(self, settings):
        self.settings = settings
        self.running = True
        self._closing = False
        self._wake = threading.Event()
        self._exit_reason = "normal"

        # --- display and input -----------------------------------------
        self.board, self.mode = board_module.acquire_board(
            on_foreground_acquired=self._on_foreground
        )
        self.display = Display(self.board, settings)
        self.state = ViewState(
            callsign=settings.identity.callsign,
            address=settings.radio.address,
            channel=settings.radio.privacy_channel,
            frequency_mhz=settings.radio.frequency_mhz,
            max_record_seconds=settings.audio.max_record_seconds,
        )

        self.gestures = GestureDetector(
            on_gesture=self._on_gesture,
            on_hold_start=self._on_talk_start,
            on_hold_end=self._on_talk_end,
            debounce_ms=settings.input.debounce_ms,
            click_window_ms=settings.input.click_window_ms,
            hold_ms=settings.input.hold_ms,
        )
        self.gestures.attach(self.board)
        for hook, handler in (("on_exit_request", self._on_exit_request),
                              ("on_focus_revoked", self._on_focus_revoked)):
            if hasattr(self.board, hook):
                getattr(self.board, hook)(handler)

        # --- storage ----------------------------------------------------
        data_dir = settings.data_dir
        self.overrides = Overrides(data_dir)
        self.keyring = Keyring(data_dir)
        clock.set_offset(self.overrides.clock_offset)
        self.roster = Roster(settings.contacts, data_dir)
        self.inbox = Inbox(data_dir)
        self.state.inbox = self.inbox.items
        self.state.unread = self.inbox.unread

        # --- audio ------------------------------------------------------
        self.codec = None
        self.codec_mode = MODE_BY_NAME.get(settings.audio.codec_mode,
                                           MODE_BY_NAME["700C"])
        try:
            self.codec = Codec2(self.codec_mode)
            self.state.codec_name = NAME_BY_MODE.get(self.codec_mode, "700C")
        except Codec2Unavailable as exc:
            log.error("codec2 unavailable: %s", exc)

        capture_device = audio_devices.resolve(
            settings.audio.capture_device, "capture", settings.audio.preferred_card)
        playback_device = audio_devices.resolve(
            settings.audio.playback_device, "playback", settings.audio.preferred_card)
        audio_devices.set_mic_level(capture_device, settings.audio.mic_level)
        self.recorder = Recorder(capture_device, settings.audio.max_record_seconds)
        self.player = Player(playback_device)

        # This station's own sound. Beeps identify who as well as what,
        # which matters when two identical Pis sit on the same desk.
        self.cues = cues_for(settings.radio.address, settings.identity.callsign)
        clashing = [c.name for c in settings.contacts
                    if c.address != settings.radio.address
                    and voice_for(c.address) == self.cues.pitch]
        if clashing:
            log.warning(
                "%s share this station's cue pitch (%d Hz), so their beeps "
                "will sound identical to ours; change one address to separate "
                "them", ", ".join(clashing), self.cues.pitch,
            )
        log.info("cue pitch %d Hz for %s (address %d)",
                 self.cues.pitch, settings.identity.callsign, settings.radio.address)
        self._refresh_audio_state()

        # --- radio ------------------------------------------------------
        self.radio = None
        self.link = None
        # Before the radio opens: a request can arrive the moment it does.
        self._pending_pair = None
        self._parents = {}
        self._open_radio()

        self._playback_lock = threading.Lock()
        self._last_recall = 0.0
        self._display_stale = False
        # The screen cannot dim on this build -- the backlight pin is the
        # radio's M0 -- so the charge left is worth showing.
        self.battery = battery.Monitor()
        self._warned_critical = False
        self._actions = self._build_actions()
        self._refresh_entries()
        self._refresh_menus()

    # --- setup helpers -------------------------------------------------
    def _open_radio(self):
        radio_settings = self.settings.radio
        mode_pins = tuple(radio_settings.mode_pins) if radio_settings.mode_pins else None
        try:
            self.radio = SX126x(
                port=radio_settings.port, addr=radio_settings.address,
                freq_mhz=radio_settings.frequency_mhz,
                uart_baud=radio_settings.uart_baud, mode_pins=mode_pins,
            )
        except Exception as exc:
            log.error("radio unavailable on %s: %s", radio_settings.port, exc)
            self.state.flash("radio offline", 6.0)
            return
        self.link = LoraLink(
            self.radio, air_speed=radio_settings.air_speed,
            duty_cycle_percent=radio_settings.duty_cycle_percent,
            callsign=self.settings.identity.callsign,
            addr=radio_settings.address, token=self.overrides.node_token,
            keyring=self.keyring, channel=radio_settings.privacy_channel,
        )
        # The module is deaf unless M0/M1 are both low, and on this
        # hardware the LCD drives those pins. Say so rather than letting
        # every transmission succeed into nothing.
        # Check and report on where the pins physically are, not on which
        # ones we drive: with the stock jumpers we drive none of them and
        # the module is still at the LCD's mercy.
        wired = radio_settings.wired_mode_pins or [22, 27]
        pins = tuple(wired)[:2]

        # The backlight pin doubles as the module's M0 on this stack, and
        # dimming it is PWM -- which would toggle the radio's mode a
        # thousand times a second. Hearing beats saving the backlight, so
        # brightness is pinned and the idle policy stands down.
        if modepins.conflicts_with_backlight(pins):
            self.display.lock_brightness(
                f"GPIO{modepins.WHISPLAY_BACKLIGHT_BCM} is both the LCD "
                "backlight and the radio's M0; dimming it is PWM and would "
                "deafen the radio"
            )
            self.state.brightness_locked = True

        health = modepins.check_and_warn(*pins)
        self.state.radio_deaf = health["readable"] and not health["transparent"]
        self.state.radio_note = health.get("detail", "")
        if self.state.radio_deaf:
            self.state.flash("radio deaf: check M0/M1", 10.0)

        shared = port_conflicts(radio_settings.port)
        if shared:
            log.error(
                "the LoRa port %s is shared: %s. Whatever else reads it takes "
                "bytes meant for the radio, so messages arrive broken or not "
                "at all, and a login shell hangs the port up when it restarts. "
                "Run ./setup.sh on this device, then reboot.",
                radio_settings.port, "; ".join(shared))
            self.state.radio_note = "LoRa port shared: run setup"
            self.state.flash("LoRa port shared: run ./setup.sh", 10.0)

        self.link.on_message(self._on_radio_message)
        self.link.on_tx_progress(self._on_tx_progress)
        self.link.on_hello(self._on_hello)
        self.link.on_clash(self._on_clash)
        self.link.start()

    def _refresh_audio_state(self):
        can_record = self.recorder.available and self.codec is not None
        can_play = self.player.available and self.codec is not None
        self.state.audio_ok = can_record and can_play
        if self.codec is None:
            self.state.audio_note = "codec2 missing"
        elif not self.recorder.available and not self.player.available:
            self.state.audio_note = audio_devices.diagnose(
                self.settings.audio.preferred_card)
        elif not self.recorder.available:
            self.state.audio_note = "no microphone"
        elif not self.player.available:
            self.state.audio_note = "no speaker"
        else:
            self.state.audio_note = audio_devices.diagnose(
                self.settings.audio.preferred_card)

    def _refresh_entries(self):
        self.state.entries = self.roster.entries()
        self.state.selected_index = self.roster.selected_index % max(
            1, len(self.state.entries))
        paired = self.keyring.is_paired if self.keyring else (lambda _a: True)
        self.state.unpaired = {e.address for e in self.state.entries
                               if not paired(e.address)}
        addr, name = self._target
        entry = self.roster.entry(addr)
        self.state.target_address = addr
        self.state.target_name = entry.name if entry else name
        self.state.target_heard = entry.status if entry else ""

    # --- board callbacks -----------------------------------------------
    def _on_foreground(self):
        self.display.invalidate()
        self.display.poke()
        self._wake.set()

    def _on_exit_request(self, *_args):
        log.info("daemon asked us to exit")
        self.stop("daemon")

    def _on_focus_revoked(self, *_args):
        """Stop drawing; the radio keeps running.

        Deliberately passive. The framebuffer is gone, so drawing would
        write into a torn-down mapping, and grabbing the screen back
        would take it from whatever the user just switched to. When the
        daemon decides to hand it back it says so, and
        `board.watch_foreground_grants` re-attaches on that event.
        """
        if self._closing:
            return
        log.info("focus revoked; still listening, waiting for the screen back")
        try:
            self.board.foreground_ready = False
        except Exception:
            pass

    # --- gestures ------------------------------------------------------
    def _on_gesture(self, gesture: str):
        # A press on a blanked screen means "wake up", not "do the thing
        # that happens to be under the cursor". Acting on a gesture the
        # operator could not see the target of is how you end up
        # transmitting to the wrong station.
        was_dark = self.display.screen_off
        self.display.poke()
        if was_dark and gesture != QUAD:
            log.info("gesture %s consumed waking the screen", gesture)
            self._wake.set()
            return
        if self.state.screen == EDIT and self.state.editor is not None:
            self._editor_gesture(gesture)
            self._wake.set()
            return

        action = navigation.route(
            self.state.screen, gesture, inbox_empty=not self.inbox.items
        )
        if action is None:
            return
        if action == navigation.EXIT_APP:
            self.stop("user")
            return
        handler = self._actions.get(action)
        if handler is None:
            log.warning("no handler for action %s", action)
            return
        handler()
        self._wake.set()

    def _build_actions(self) -> dict:
        """Action name -> what it does. Keys must cover navigation's table."""
        return {
            navigation.NEXT_ITEM: self._next_item,
            navigation.OPEN_ITEM: self._open_item,
            navigation.NEXT_CONTACT: self._next_contact,
            navigation.OPEN_TALK: self._open_talk,
            navigation.OPEN_INBOX: self._open_inbox,
            navigation.OPEN_STATUS: lambda: self._show(STATUS),
            navigation.NEXT_MESSAGE: self._next_message,
            navigation.PLAY_SELECTED: self._play_selected,
            navigation.REPLAY_LAST: self._replay_last,
            navigation.OPEN_SETTINGS: self._open_settings,
            navigation.NEXT_SETTING: self._next_setting,
            navigation.OPEN_SETTING: self._open_setting,
            navigation.NEXT_FOUND: self._next_found,
            navigation.PAIR_SELECTED: self._pair_selected,
            navigation.GO_BACK: self._go_back,
        }

    # --- where you are ---------------------------------------------------
    def _show(self, screen: str):
        """Go to `screen`, remembering where "back" returns to."""
        current = self.state.screen
        if self._parents is None:
            self._parents = {}
        if screen != current and current != EDIT:
            self._parents[screen] = current
        self.state.screen = screen

    def _go_back(self):
        parents = self._parents or {}
        self.state.screen = parents.get(self.state.screen, HOME)
        if self.state.screen == SETTINGS:
            self._refresh_settings()

    # --- home and start menus ----------------------------------------------
    def _home_items(self) -> list:
        paired = len([e for e in self.roster.entries()
                      if e.address not in self.state.unpaired])
        unread, total = self.inbox.unread, len(self.inbox.items)
        return [
            {"key": "start", "label": "Start",
             "value": f"now talking to {self.state.target_name}"},
            {"key": "receive", "label": "Receive",
             "value": (f"{unread} new  ·  {total} in all" if unread
                       else f"nothing new  ·  listening on ch {self.state.channel}")},
            {"key": "pair", "label": "Pair devices",
             "value": f"{paired} paired  ·  add another radio"},
            {"key": "settings", "label": "Settings",
             "value": "name, ID, privacy channel"},
        ]

    def _start_items(self) -> list:
        paired = len([e for e in self.roster.entries()
                      if e.address not in self.state.unpaired])
        return [
            {"key": "all", "label": "To ALL",
             "value": f"every paired radio on channel {self.state.channel}"},
            {"key": "device", "label": "To a paired device",
             "value": f"{paired} paired" if paired else "none yet: pair one first"},
        ]

    def _refresh_menus(self):
        self.state.home_items = self._home_items()
        self.state.start_items = self._start_items()

    def _next_item(self):
        if self.state.screen == HOME:
            self.state.home_index = (self.state.home_index + 1) % len(self.state.home_items)
        elif self.state.screen == START:
            self.state.start_index = (self.state.start_index + 1) % len(self.state.start_items)

    def _open_item(self):
        if self.state.screen == HOME:
            key = self.state.home_items[self.state.home_index % len(self.state.home_items)]["key"]
            {"start": lambda: self._show(START), "receive": self._open_inbox,
             "pair": self._start_pairing, "settings": self._open_settings}[key]()
        elif self.state.screen == START:
            key = self.state.start_items[self.state.start_index % len(self.state.start_items)]["key"]
            if key == "all":
                self._talk_to(protocol.BROADCAST, BROADCAST_NAME)
            else:
                self._show(CONTACTS)

    # --- who to talk to ------------------------------------------------------
    def _talk_to(self, addr: int, name: str):
        """Make `addr` the target, and open Talk on it."""
        if addr != protocol.BROADCAST and not self._can_reach(addr, name):
            return
        self._target = (addr, name)
        self._refresh_entries()
        self._call(addr)
        self._show(TALK)

    def _can_reach(self, addr: int, name: str) -> bool:
        if self.link is not None and not self.link.can_send(addr):
            self.state.flash(f"pair with {name} first", 3.0)
            self.player.cue(self.cues.error)
            return False
        return True

    def _open_talk(self):
        entry = self.roster.selected()
        if entry is None:
            self.state.flash("no paired radios yet")
            return
        self._talk_to(entry.address, entry.name)

    def _next_contact(self):
        if self.roster.advance() is None:
            self.state.flash("no paired radios yet")
        self._refresh_entries()

    def _open_inbox(self):
        self._show(INBOX)
        self.state.inbox_index = 0

    def _next_message(self):
        if self.inbox.items:
            self.state.inbox_index = (
                self.state.inbox_index + 1) % len(self.inbox.items)

    # --- handshake ----------------------------------------------------------
    def _on_hello(self, peer, name: str):
        """A paired station is calling. Called on the receive thread.

        Its hello was sealed with our shared key, or the link would have
        dropped it unread, so it is answered without asking: you paired
        it deliberately, and confirming every boot would be noise. New
        radios are added only by pairing.
        """
        if self.keyring is not None and not self.keyring.is_paired(peer.addr):
            return False
        self.state.flash(f"{name or peer.addr} connected")
        self._wake.set()
        return True

    def _call(self, addr: int, force: bool = False):
        """Say hello to a station so both ends know the link works."""
        if self.link is None or addr == protocol.BROADCAST:
            return
        if not self.link.can_send(addr, protocol.HELLO):
            return  # a contact from before pairing: no key to call it with
        state = self.link.link_state(addr)
        if not force and state in (protocol.LINK_LINKED, protocol.LINK_CALLING):
            return
        log.info("calling %d", addr)
        self.link.send_hello(addr)
        self._wake.set()

    def _call_known_contacts(self, force: bool = False):
        """On startup, tell every paired station we are on the air."""
        for contact in self.settings.contacts:
            if contact.address != protocol.BROADCAST:
                self._call(contact.address, force=force)

    def _link_states(self) -> dict:
        if self.link is None:
            return {}
        return {addr: peer.link_state for addr, peer in self.link.peers.items()}

    # --- focus ---------------------------------------------------------------
    @property
    def foregrounded(self) -> bool:
        return bool(getattr(self.board, "foreground_ready", False))

    # --- settings ---------------------------------------------------------
    def _settings_items(self) -> list:
        base = self.overrides.base_address
        base_name = next(
            (e.name for e in self.roster.entries() if e.address == base),
            str(base) if base is not None else "not set",
        )
        return [
            {"key": "name", "label": "Name",
             "value": f"{self.settings.identity.callsign}  ·  what other radios see"},
            {"key": "device_id", "label": "Device ID",
             "value": f"{self.settings.radio.address}  ·  unique to this radio"},
            {"key": "channel", "label": "Privacy channel",
             "value": f"{self.settings.radio.privacy_channel}  ·  others are ignored"},
            {"key": "voice", "label": "Voice quality",
             "value": self._voice_summary(self.settings.audio.codec_mode)},
            {"key": "base", "label": "Base station", "value": base_name},
            {"key": "clock", "label": "Date & time",
             "value": clock.now().strftime("%Y-%m-%d %H:%M") + "  ·  " + clock.describe()},
            {"key": "reset", "label": "Reset all data",
             "value": f"{len(self.inbox.items)} message(s), paired radios, keys",
             "destructive": True},
        ]

    def _open_settings(self):
        self._show(SETTINGS)
        self.state.settings_index = 0
        self.state.settings_items = self._settings_items()

    def _next_setting(self):
        items = self.state.settings_items or self._settings_items()
        self.state.settings_index = (self.state.settings_index + 1) % len(items)

    def _refresh_settings(self):
        self.state.settings_items = self._settings_items()

    def _open_setting(self):
        items = self.state.settings_items or self._settings_items()
        key = items[self.state.settings_index % len(items)]["key"]
        opener = {
            "name": self._edit_name, "device_id": self._edit_device_id,
            "channel": self._edit_channel, "voice": self._edit_voice,
            "base": self._edit_base,
            "clock": self._edit_clock, "reset": self._edit_reset,
        }[key]
        opener()

    def _begin_edit(self, editor, title: str, hint: str = ""):
        # Where to go when it closes: an incoming call can interrupt any
        # screen, and answering it should not strand you in Settings.
        if self.state.screen != EDIT:
            self._return_screen = self.state.screen
        self.state.editor = editor
        self.state.editor_title = title
        self.state.editor_hint = hint
        self.state.screen = EDIT

    def _edit_name(self):
        current = self.settings.identity.callsign
        names = [hostname_callsign()] + list(CALLSIGNS)
        if current not in names:
            names.insert(0, current)  # a name set in config.yaml
        choices = [(name, name) for name in names]
        self._begin_edit(ChoiceEditor(choices, names.index(current)), "NAME",
                         "paired radios learn it next time you call")

    def _edit_device_id(self):
        self._begin_edit(
            DigitEditor(self.settings.radio.address, digits=5, maximum=65534),
            "DEVICE ID",
            "must be unique on the channel",
        )

    def _edit_channel(self):
        choices = [(f"channel {n}", n) for n in protocol.CHANNELS]
        current = self.settings.radio.privacy_channel
        index = next((i for i, (_l, n) in enumerate(choices) if n == current), 0)
        self._begin_edit(ChoiceEditor(choices, index), "CHANNEL",
                         "only radios on the same channel hear you")

    def _voice_summary(self, name: str) -> str:
        label = next((l for l, n in VOICE_QUALITIES if n == name), name)
        per_hour = self._messages_per_hour(name)
        return f"{label} ({name})" + (f"  ·  ~{per_hour} × 10 s an hour" if per_hour else "")

    def _messages_per_hour(self, name: str) -> int:
        """Ten-second messages the hour's airtime holds at this quality."""
        if self.link is None or name not in MODE_BY_NAME:
            return 0
        try:
            codec = Codec2(MODE_BY_NAME[name])
        except Codec2Unavailable:
            return 0
        size = codec.bytes_for_seconds(10.0)
        codec.close()
        _fragments, on_air = self.link.plan(protocol.BROADCAST, size)
        seconds = self.link.budget.estimate_message(on_air)
        limit = self.link.budget.limit_seconds
        return int(limit // seconds) if seconds and limit != float("inf") else 0

    def _edit_voice(self):
        choices = [(f"{label} ({name})", name) for label, name in VOICE_QUALITIES]
        current = self.settings.audio.codec_mode
        index = next((i for i, (_l, n) in enumerate(choices) if n == current), 0)
        self._begin_edit(ChoiceEditor(choices, index), "VOICE",
                         "clearer takes more airtime per message")

    def _edit_base(self):
        choices = [(e.name, e.address) for e in self.roster.entries()]
        choices.append(("(none)", None))
        current = self.overrides.base_address
        index = next((i for i, (_n, a) in enumerate(choices) if a == current), 0)
        self._begin_edit(ChoiceEditor(choices, index), "BASE STATION")

    def _edit_clock(self):
        self._begin_edit(ClockEditor(clock.now()), "DATE & TIME")

    def _edit_reset(self):
        self._begin_edit(
            ConfirmEditor("Erase everything?",
                          "messages, voice clips, paired\nradios, keys and settings"),
            "RESET",
        )

    def _editor_gesture(self, gesture: str):
        editor = self.state.editor
        if not editor.handle(gesture):
            return
        title = self.state.editor_title
        self.state.editor = None
        self.state.screen = self._return_screen
        if editor.cancelled:
            if title == "PAIRING":
                self._apply_pair_decision(False)
            else:
                self.state.flash("cancelled")
        else:
            self._commit_edit(title, editor)
        self._refresh_settings()

    def _commit_edit(self, title: str, editor):
        if title == "DEVICE ID":
            self._apply_device_id(editor.value)
        elif title == "NAME":
            self._apply_name(editor.value)
        elif title == "CHANNEL":
            self._apply_channel(editor.value)
        elif title == "VOICE":
            self._apply_voice(editor.value)
        elif title == "BASE STATION":
            self.overrides.set_base(editor.value)
            self.state.flash(f"base: {editor.text}")
        elif title == "DATE & TIME":
            how = clock.apply(editor.to_datetime(), self.overrides)
            self.state.flash("clock set" if how == "system" else "clock set (app only)")
        elif title == "RESET":
            self._apply_reset()
        elif title == "PAIRING":
            self._apply_pair_decision(True)

    def _apply_device_id(self, address: int):
        if address == self.settings.radio.address:
            return
        if any(c.address == address for c in self.settings.contacts):
            self.state.flash("that is a contact's ID", 4.0)
            self.player.cue(self.cues.error)
            return
        old = self.settings.radio.address
        self.overrides.set("radio", "address", address)
        self._set_address(address)
        # Effective at once: addressing is in the packet header, not the
        # module's registers. But radios that paired with the old ID still
        # have it saved, and will be calling a number nobody answers.
        self.state.flash(f"ID {address} · re-pair others", 5.0)
        log.info("device id changed %d -> %d; radios paired with %d must pair again",
                 old, address, old)

    def _set_address(self, address: int):
        """Use a new Device ID from now on. The caller persists it."""
        self.settings.radio.address = address
        self.state.address = address
        if self.link is not None:
            self.link.set_address(address)
        # Cues are pitched by address, so this radio's sound moves with it.
        self.cues = cues_for(address, self.settings.identity.callsign)

    def _apply_name(self, name: str):
        if name == self.settings.identity.callsign:
            return
        self.overrides.set("identity", "callsign", name)
        self.settings.identity.callsign = name
        self.state.callsign = name
        if self.link is not None:
            self.link.callsign = name
        self.cues = cues_for(self.settings.radio.address, name)
        # A hello carries the name, so calling everyone tells them now.
        self._call_known_contacts(force=True)
        self.state.flash(f"name: {name}")

    def _apply_channel(self, channel: int):
        if channel == self.settings.radio.privacy_channel:
            return
        self.overrides.set("radio", "privacy_channel", channel)
        self.settings.radio.privacy_channel = channel
        self.state.channel = channel
        if self.link is not None:
            self.link.set_channel(channel)
        self._refresh_menus()
        self.state.flash(f"channel {channel}", 3.0)

    def _apply_voice(self, name: str):
        if name == self.settings.audio.codec_mode or name not in MODE_BY_NAME:
            return
        try:
            codec = Codec2(MODE_BY_NAME[name])
        except Codec2Unavailable as exc:
            log.error("cannot switch to codec2 %s: %s", name, exc)
            self.state.flash("that quality is unavailable", 3.0)
            return
        old, self.codec = self.codec, codec
        self.codec_mode = codec.mode
        if old is not None:
            old.close()
        self.overrides.set("audio", "codec_mode", name)
        self.settings.audio.codec_mode = name
        self.state.codec_name = name
        # Receivers decode each message in the mode it names, so nothing
        # else has to change, here or on any other radio.
        self.state.flash(f"voice: {self._voice_summary(name).split('  ·')[0]}", 3.0)

    def _add_contact(self, name: str, address: int) -> bool:
        """Save a station as a contact. False if it already was one."""
        if not self.overrides.add_contact(name, address):
            self._rename_contact(address, name)
            return False
        self.settings.contacts.append(Contact(name=name, address=address))
        self.roster = Roster(self.settings.contacts, self.settings.data_dir)
        self._refresh_entries()
        return True

    def _rename_contact(self, address: int, name: str):
        """A paired radio announced a new name; show it under that."""
        if not name or not self.overrides.rename_contact(address, name):
            return
        for contact in self.settings.contacts:
            if contact.address == address:
                contact.name = name
        self.roster = Roster(self.settings.contacts, self.settings.data_dir)
        self._refresh_entries()

    # --- pairing ------------------------------------------------------------
    # Both operators open Home > Pair devices. Each radio beacons its
    # public key while the screen is open and lists the other radios it
    # hears beaconing. Picking one sends it a pairing request -- our
    # public key, and our broadcast key sealed so only it can read it --
    # and both screens show a four-digit code from the two keys. The
    # other operator checks the codes match and accepts, which answers
    # with the same in the other direction. Only then does either side
    # save the other: the one that asked saves when the answer arrives.
    def _start_pairing(self):
        if self.link is None:
            self.state.flash("radio offline")
            self.player.cue(self.cues.error)
            return
        self._pairing = True
        self._pairing_until = time.monotonic() + PAIR_WINDOW_SECONDS
        self._pair_beacon_due = 0.0           # announce straight away
        self._pair_found = {}
        self._pairing_with = None
        self.state.pair_index = 0
        self.state.pair_status = "looking for radios"
        self._refresh_pair_view()
        self._show(PAIR)
        log.info("pairing: announcing as %s (ID %d) on channel %d",
                 self.settings.identity.callsign, self.settings.radio.address,
                 self.settings.radio.privacy_channel)

    def _stop_pairing(self, then: str | None = None):
        """End pairing; if the pairing screen is up, move to `then`."""
        self._pairing = False
        self._pairing_with = None
        if then is None:
            return
        if self.state.screen == PAIR:
            self.state.screen = then
        elif self.state.screen == EDIT and self._return_screen == PAIR:
            self._return_screen = then

    def _finish_pairing(self, addr: int, name: str):
        # Land on the paired list, with Back leading out through Start to
        # Home -- not back into a pairing screen that has closed.
        self._stop_pairing(CONTACTS)
        self._parents[CONTACTS] = START
        self._parents[START] = HOME
        self.roster.select_address(addr)
        self._refresh_entries()
        self._refresh_menus()
        self.state.flash(f"paired with {name}", 4.0)
        log.info("pairing: paired with %s (%d)", name, addr)

    def _refresh_pair_view(self):
        # Discovery order, not signal or recency: re-sorting on every
        # beacon would move the row under the operator's cursor.
        paired = self.keyring.is_paired if self.keyring else (lambda _a: False)
        self.state.pair_found = [
            (addr, info["name"], info["rssi"], paired(addr))
            for addr, info in (self._pair_found or {}).items()
        ]
        self.state.pair_channels = {addr: info.get("channel")
                                    for addr, info in (self._pair_found or {}).items()}

    def _next_found(self):
        found = self.state.pair_found
        if not found:
            self.state.flash("none found yet")
            return
        self.state.pair_index = (self.state.pair_index + 1) % len(found)

    def _pair_selected(self):
        found = self.state.pair_found
        if not found or self.link is None:
            self.state.flash("none found yet")
            return
        addr, name, _rssi, _known = found[self.state.pair_index % len(found)]
        public = self._pair_found[addr]["public"]
        code = self.keyring.code_with(public)
        self._pairing_with = (addr, name, time.monotonic(), public)
        self.state.pair_status = f"code {code} · waiting for {name}"
        log.info("pairing: asking %s (%d), code %s", name, addr, code)
        body = self.keyring.pair_body(public, self.settings.radio.address, addr,
                                      self.settings.identity.callsign)
        self.link.send_pairing(protocol.PAIR_REQUEST, addr, body)

    def _on_pair_beacon(self, message, peer):
        """Someone nearby is pairing. On the rx thread."""
        if not self._pairing:
            return  # nobody here asked to see it
        _token, public, name = protocol.parse_pair(message.body)
        name = name or peer.name or f"node {message.src}"
        fresh = message.src not in self._pair_found
        self._pair_found[message.src] = {"name": name, "rssi": message.rssi_dbm,
                                         "public": public, "channel": message.channel}
        self._refresh_pair_view()
        if fresh:
            log.info("pairing: found %s (%d)", name, message.src)
            # Answer now rather than on the next beacon, so the other
            # radio lists us as soon as we listed it.
            self._pair_beacon_due = 0.0
        self._wake.set()

    def _on_pair_request(self, message):
        """Another radio asks to pair. On the rx thread.

        Only while pairing: a request that arrives otherwise is ignored,
        so nobody can make a radio in someone's pocket start asking.
        """
        if not self._pairing or self._pending_pair is not None:
            return
        opened = self.keyring.open_pair_body(message.body, message.src,
                                             self.settings.radio.address)
        if opened is None:
            log.warning("pairing: an unreadable request from %d", message.src)
            return
        public, broadcast, name = opened
        name = name or f"node {message.src}"
        code = self.keyring.code_with(public)
        self._pending_pair = (message.src, name, public, broadcast, code)
        log.info("pairing: %s (%d) asks to pair, code %s", name, message.src, code)
        self._wake.set()

    def _prompt_pending_pair(self):
        """Show the accept/refuse prompt, once there is a moment to."""
        if not self._pending_pair or self.state.screen == EDIT:
            return
        if self.state.busy or not self.foregrounded:
            return
        addr, name, _public, _broadcast, code = self._pending_pair
        self._begin_edit(
            ConfirmEditor(f"{name} wants to pair",
                          f"code {code}  ·  ID {addr}\nsame code on both screens?\n"
                          "accept to add it as a contact"),
            "PAIRING",
        )

    def _apply_pair_decision(self, accepted: bool):
        pending, self._pending_pair = self._pending_pair, None
        if pending is None or self.link is None:
            return
        addr, name, public, broadcast, _code = pending
        if not accepted:
            self.link.refuse(addr)
            self.state.flash(f"{name} refused")
            return
        self.keyring.add_peer(addr, public, broadcast)
        body = self.keyring.pair_body(public, self.settings.radio.address, addr,
                                      self.settings.identity.callsign)
        self.link.send_pairing(protocol.PAIR_ACCEPT, addr, body)
        self.link.mark_linked(addr)
        self._add_contact(name, addr)
        self._finish_pairing(addr, name)

    def _on_pair_accept(self, message):
        """The radio we asked said yes. On the rx thread."""
        if not self._pairing or not self._pairing_with \
                or self._pairing_with[0] != message.src:
            return
        addr, name, _asked, expected = self._pairing_with
        opened = self.keyring.open_pair_body(message.body, message.src,
                                             self.settings.radio.address)
        if opened is None or opened[0] != expected:
            # Not the key its beacon offered: someone else answered for it.
            self._pairing_with = None
            self.state.pair_status = f"{name}: keys did not match, not paired"
            log.warning("pairing: %s (%d) answered with a different key", name, addr)
            return
        _public, broadcast, announced = opened
        self._pairing_with = None
        self.keyring.add_peer(addr, expected, broadcast)
        self.link.mark_linked(addr)
        self._add_contact(announced or name, addr)
        self._finish_pairing(addr, announced or name)
        # Pairing is heard across privacy channels, talking is not: the
        # radio that asked joins the channel of the one that said yes, or
        # the two would be paired and still unable to hear each other.
        if message.channel != self.settings.radio.privacy_channel:
            self._apply_channel(message.channel)
            self.state.flash(f"paired with {announced or name} · now on channel "
                             f"{message.channel}", 5.0)

    def _on_pair_refused(self, message):
        if self._pairing_with and self._pairing_with[0] == message.src:
            self.state.pair_status = f"{self._pairing_with[1]} said no"
            self._pairing_with = None
            self._wake.set()

    def _on_clash(self, name: str):
        """Another radio is beaconing with our Device ID. On the rx thread."""
        if self._pairing:
            # This is the radio being set up, so this is the one that moves.
            old = self.settings.radio.address
            taken = {c.address for c in self.settings.contacts} | {old}
            if self.link is not None:
                taken |= set(self.link.peers)
            new = self.overrides.assign_address(taken)
            self._set_address(new)
            self._pair_beacon_due = 0.0       # announce the new ID at once
            self.state.flash(f"ID {old} was taken: now {new}", 5.0)
            log.warning("pairing: %s also uses ID %d; this radio is now %d",
                        name or "another radio", old, new)
        elif time.monotonic() - self._last_clash_reply >= CLASH_REPLY_SECONDS:
            # Not the one being set up: answer, so the radio that is
            # pairing hears the clash and moves itself.
            self._last_clash_reply = time.monotonic()
            if self.link is not None:
                self.link.send_pair()
        self._wake.set()

    def _pairing_tick(self):
        """Beacon, and time out, while pairing. Called from the main loop."""
        now = time.monotonic()
        if now >= self._pairing_until:
            log.info("pairing: window closed")
            self._stop_pairing((self._parents or {}).get(PAIR, HOME))
            self.state.flash("pairing timed out", 3.0)
            return
        if self._pairing_with and now - self._pairing_with[2] >= PAIR_ANSWER_SECONDS:
            self.state.pair_status = f"no answer from {self._pairing_with[1]}"
            self._pairing_with = None
        if now >= self._pair_beacon_due and self.link is not None:
            self.link.send_pair()
            self._pair_beacon_due = now + PAIR_BEACON_SECONDS

    def _pairing_deadline(self) -> float:
        deadlines = [self._pair_beacon_due, self._pairing_until]
        if self._pairing_with:
            deadlines.append(self._pairing_with[2] + PAIR_ANSWER_SECONDS)
        return max(0.05, min(deadlines) - time.monotonic())

    def _apply_reset(self):
        log.warning("resetting all app data at the operator's request")
        for item in list(self.inbox.items):
            if item.voice_file:
                (self.inbox.voice_dir / item.voice_file).unlink(missing_ok=True)
        self.inbox.items = []
        self.inbox.save()
        self.overrides.clear()
        # New keys: every radio this one paired with has to pair again,
        # and nothing recorded off the air before can be opened with them.
        if self.keyring is not None:
            self.keyring.reset()
        self.settings.contacts = []
        self.roster = Roster(self.settings.contacts, self.settings.data_dir)
        self._target = (protocol.BROADCAST, BROADCAST_NAME)
        clock.set_offset(0.0)
        try:
            self.roster._seen = {}
            self.roster.save()
        except Exception:
            log.debug("roster reset failed", exc_info=True)
        self.state.inbox = self.inbox.items
        self.state.unread = 0
        self.state.inbox_index = 0
        self._refresh_entries()
        self._refresh_menus()
        self.state.flash("all data erased", 4.0)

    # --- push to talk ---------------------------------------------------
    def _on_talk_start(self):
        """The button has been down long enough to mean speech."""
        self.display.poke()
        if self.state.radio_state in (RECORDING, SENDING):
            return
        if self.state.screen == EDIT:
            # A hold here is not an attempt to talk; it would transmit
            # whatever half-edited value is on screen.
            self.state.flash("finish editing first")
            self._wake.set()
            return
        if not navigation.can_talk(self.state.screen):
            self.state.flash("listening only here" if self.state.screen == INBOX
                             else "to talk: Home > Start")
            self._wake.set()
            return
        if self.link is None:
            self.state.flash("radio offline")
            self._wake.set()
            return
        if not self.recorder.available or self.codec is None:
            self.state.flash(self.state.audio_note or "no microphone", 3.0)
            self.player.cue(self.cues.error)
            self._wake.set()
            return
        addr, name = self._target
        if not self._can_reach(addr, name):
            self._wake.set()
            return

        self.player.stop()  # duck any playback so we do not record it
        if not self.recorder.start():
            self.state.flash("microphone busy")
            self._wake.set()
            return

        self._show(TALK)
        self.state.radio_state = RECORDING
        self.display.set_led(theme.LED_REC)
        self._wake.set()

    def _on_talk_end(self, held_seconds: float):
        if self.state.radio_state != RECORDING:
            return
        pcm = self.recorder.stop()
        self.state.radio_state = IDLE
        self.display.set_led(theme.LED_IDLE)
        if getattr(self.recorder, "last_clipped", 0.0) > CLIPPED_TOO_MUCH:
            # Distorted at the microphone: no codec can make that clear.
            self.state.flash("mic too loud: hold the radio further away", 4.0)

        duration = len(pcm) / 2 / SAMPLE_RATE
        if duration < MIN_TALK_SECONDS or not pcm:
            self.state.flash("too short")
            self.player.cue(self.cues.error)
            self._wake.set()
            return

        # Encoding is off the UI thread: 20 s of 700C is real work on a
        # Zero 2 W, and the screen should stay live while it happens.
        threading.Thread(
            target=self._encode_and_send, args=(pcm, duration),
            name="encode-send", daemon=True,
        ).start()
        self._wake.set()

    def _encode_and_send(self, pcm: bytes, duration: float):
        address, name = self._target
        started = time.monotonic()
        try:
            encoded = self.codec.encode(pcm)
        except Exception:
            log.exception("codec2 encode failed")
            self.state.flash("encode failed")
            self.player.cue(self.cues.error)
            self._wake.set()
            return

        try:
            packets, on_air = self.link.plan(address, len(encoded))
        except NotPaired:
            self.state.flash(f"pair with {name} first", 3.0)
            self.player.cue(self.cues.error)
            self._wake.set()
            return
        log.info(
            "%.1fs speech -> %d B in %.0f ms -> %d packet(s) to %s",
            duration, len(encoded), (time.monotonic() - started) * 1000,
            packets, name,
        )

        airtime = self.link.budget.estimate_message(on_air)
        if self.link.budget.remaining_seconds() < airtime:
            self.state.flash("duty cycle full", 4.0)
            self.player.cue(self.cues.error)
            self._wake.set()
            return

        self.state.radio_state = SENDING
        self.state.tx_sent, self.state.tx_total = 0, packets
        if self.settings.audio.cues:
            self.player.cue(self.cues.tx_start)
        self.display.set_led(theme.LED_TX)
        self._wake.set()

        self.link.send_voice(address, encoded, self.codec_mode)
        self._record_outgoing(name, duration, len(encoded))

    def _record_outgoing(self, target_name: str, duration: float, size: int):
        sent = protocol.Message(
            type=protocol.VOICE, src=self.settings.radio.address, msg_id=0, body=b"",
            flags=self.codec_mode, missing=[], rssi_dbm=None,
            received_at=time.time(),
        )
        self.inbox.add_voice(sent, target_name, duration,
                             outgoing=True, store_audio=False)
        self.state.inbox = self.inbox.items

    # --- receiving ------------------------------------------------------
    def _on_radio_message(self, message, peer):
        """Called on the link's rx thread; must not block it for long."""
        # Pairing traffic comes before the roster: a stranger pairing across
        # the street is not someone to list among your stations.
        pairing = {protocol.PAIR: lambda: self._on_pair_beacon(message, peer),
                   protocol.PAIR_REQUEST: lambda: self._on_pair_request(message),
                   protocol.PAIR_ACCEPT: lambda: self._on_pair_accept(message),
                   protocol.REJECT: lambda: self._on_pair_refused(message)}
        if message.type in pairing:
            pairing[message.type]()
            return
        self.roster.note_peer(message.src, peer.name, message.rssi_dbm)
        self.roster.save()
        self.state.last_rssi = message.rssi_dbm
        self.display.poke()

        if message.type in (protocol.HELLO, protocol.HELLO_ACK):
            # Sealed with our shared key, so the name in it is really theirs.
            self._rename_contact(message.src, peer.name)
            self._refresh_entries()
            if message.type == protocol.HELLO:
                self.state.flash(f"{peer.name or message.src} on air")
            self._wake.set()
            return

        name = peer.name or f"node {message.src}"
        if message.type == protocol.TEXT:
            self.inbox.add_text(message, name)
            self.state.flash(f"{name}: {message.body.decode('utf-8', 'replace')[:24]}")
        elif message.type == protocol.VOICE:
            message = self._silence_gaps(message)
            duration = self._voice_duration(message)
            item = self.inbox.add_voice(message, name, duration)
            self.state.flash(f"{name} · {duration:.0f}s voice")
            self._autoplay(item)
        else:
            return

        self.state.inbox = self.inbox.items
        self.state.unread = self.inbox.unread
        self._refresh_entries()
        self._wake.set()

    def _silence_gaps(self, message):
        """Fill each lost fragment's place with encoded silence.

        The link keeps a lost fragment's place as zero bytes, so the
        frames after it still line up; zeros are not silence to Codec2,
        though, and play as a burst. Encoded silence plays as a pause the
        length of what was lost, and the words either side stay clear.
        """
        if not message.missing or not message.fragment_size:
            return message
        mode = message.flags if message.flags in NAME_BY_MODE else self.codec_mode
        try:
            codec = self.codec if self.codec and mode == self.codec_mode else Codec2(mode)
        except Codec2Unavailable:
            return message
        if codec is None:
            return message
        frame = codec.encode(bytes(codec.samples_per_frame * 2))
        body = bytearray(message.body)
        size = message.fragment_size
        for seq in message.missing:
            start = seq * size
            frames = (min(start + size, len(body)) - start) // len(frame)
            if frames > 0:
                body[start:start + frames * len(frame)] = frame * frames
        log.info("silenced %d lost fragment(s) of a voice message from %d",
                 len(message.missing), message.src)
        return dataclasses.replace(message, body=bytes(body))

    def _voice_duration(self, message) -> float:
        mode = message.flags if message.flags in NAME_BY_MODE else self.codec_mode
        try:
            codec = self.codec if mode == self.codec_mode else Codec2(mode)
        except Codec2Unavailable:
            return 0.0
        if codec is None:
            return 0.0
        return codec.seconds_for_bytes(len(message.body))

    def _autoplay(self, item):
        """Walkie-talkie behaviour: incoming speech plays straight away.

        Held back only while the operator is talking or transmitting --
        playing then would both record our own speaker and confuse who
        has the channel.
        """
        if self.state.radio_state in (RECORDING, SENDING):
            log.info("holding playback: busy transmitting")
            return
        threading.Thread(target=self._play_item, args=(item,),
                         name="autoplay", daemon=True).start()

    def _play_selected(self):
        if not self.inbox.items:
            self.state.flash("inbox empty")
            return
        item = self.inbox.items[self.state.inbox_index % len(self.inbox.items)]
        if item.kind != "voice" or not item.voice_file:
            self.state.flash("nothing to play")
            return
        threading.Thread(target=self._play_item, args=(item,),
                         name="playback", daemon=True).start()

    def _replay_last(self):
        item = self.inbox.latest_voice()
        if item is None:
            self.state.flash("no voice yet")
            return
        threading.Thread(target=self._play_item, args=(item,),
                         name="replay", daemon=True).start()

    def _play_item(self, item):
        if not self._playback_lock.acquire(timeout=10):
            return
        try:
            data = self.inbox.voice_bytes(item)
            if not data or self.codec is None or not self.player.available:
                self.state.flash(self.state.audio_note or "no speaker")
                self._wake.set()
                return

            mode = item.codec_mode if item.codec_mode in NAME_BY_MODE else self.codec_mode
            codec = self.codec if mode == self.codec_mode else Codec2(mode)
            pcm = codec.decode(data)
            if item.incomplete:
                # Fragments that never arrived become silence of the right
                # length, so the clip keeps its timing instead of jumping.
                pcm += codec.silence(0.3)

            self.state.radio_state = PLAYING
            self.display.set_led(theme.LED_RX)
            self._wake.set()
            if self.settings.audio.cues:
                # The *sender's* pitch, so you know who is calling before
                # a word is decoded.
                self.player.play(cues_for(item.src).rx)
            self.player.play(pcm)
            self.inbox.mark_played(item)
            self.state.unread = self.inbox.unread
        except Exception:
            log.exception("playback failed")
        finally:
            self.state.radio_state = IDLE
            self.display.set_led(theme.LED_IDLE)
            self._playback_lock.release()
            self._wake.set()

    def _on_tx_progress(self, sent: int, total: int):
        self.state.tx_sent, self.state.tx_total = sent, total
        if sent >= total:
            self.state.radio_state = IDLE
            self.display.set_led(theme.LED_IDLE)
            if self.settings.audio.cues:
                self.player.cue(self.cues.tx_done)
            self.state.flash("sent")
        self._wake.set()

    # --- main loop -------------------------------------------------------
    def _sync_state(self):
        if self.recorder.recording:
            self.state.record_level = self.recorder.level
            self.state.record_seconds = self.recorder.elapsed
            if self.state.record_seconds >= self.settings.audio.max_record_seconds:
                self._on_talk_end(self.state.record_seconds)
        if self.link is not None:
            stats = self.link.stats
            self.state.queued = self.link.pending()
            self.state.duty_fraction = self.link.budget.fraction_used()
            remaining = self.link.budget.remaining_seconds()
            self.state.duty_remaining = 999 if remaining == float("inf") else remaining
            if stats.last_rssi is not None:
                self.state.last_rssi = stats.last_rssi
            self.state.stats = {
                "packets_tx": stats.packets_tx, "packets_rx": stats.packets_rx,
                "frames_dropped": stats.frames_dropped,
                "messages_rx": stats.messages_rx,
            }
        self.state.unread = self.inbox.unread

        if (self.link is not None and not self._warned_config_mode
                and self.link.stats.config_mode_replies >= 3):
            # The module answers FF FF FF to everything: M1 is held high.
            self._warned_config_mode = True
            self.state.radio_note = "radio stuck in setup mode (M1 high)"
            self.state.flash("radio in setup mode: run the installer", 10.0)

        # Leaving the pairing screen any way at all -- a hold to talk, say
        # -- ends pairing, so it never beacons behind another screen.
        if self._pairing and self.state.screen not in (PAIR, EDIT):
            self._stop_pairing()

        # Link state for the contact dots and the Talk screen's warning.
        self.state.link_states = self._link_states()
        target, _name = self._target
        broadcast = target == protocol.BROADCAST
        # Stale counts as connected: the handshake succeeded and nothing
        # has contradicted it. Only never-linked or refused is "not
        # connected", which is what the operator can actually act on.
        target_state = self.state.link_states.get(target)
        self.state.target_linked = broadcast or target_state in (
            protocol.LINK_LINKED, protocol.LINK_STALE)
        self._refresh_menus()

        # Quietly re-call anything not linked while its page is open. A
        # hello is 13 bytes; sitting there saying "not connected" when one
        # small packet would fix it is the worse trade.
        if (self.state.screen == TALK and not broadcast
                and target_state not in (protocol.LINK_LINKED,
                                         protocol.LINK_CALLING,
                                         protocol.LINK_REJECTED)):
            now = time.monotonic()
            if now - self._last_recall >= RECALL_SECONDS:
                self._last_recall = now
                self._call(target)

        power = self.battery.poll()
        self.state.battery_present = power.present
        self.state.battery_summary = power.compact()
        self.state.battery_detail = power.summary()
        self.state.battery_percent = power.percent
        self.state.battery_low = power.low
        if power.critical and not self._warned_critical:
            self._warned_critical = True
            self.state.flash("battery critical", 8.0)
            self.player.cue(self.cues.error)
        elif not power.critical:
            self._warned_critical = False

    def _follow_idle_with_the_microphone(self):
        """Keep the capture stream alive only while the radio is in use.

        A warm codec makes push-to-talk instant but draws current
        continuously, so arming tracks the backlight: lit means the
        operator is here and PTT must not lose their first word; blanked
        means the radio is idle and the codec should power down.
        """
        if not self.recorder.available or self.recorder.recording:
            return
        # Backgrounded: nobody can press talk, so the codec can power down
        # even though our own backlight tracking says the screen is lit.
        # And only where a hold can talk: elsewhere the pre-roll would keep
        # the codec powered for a press that cannot happen.
        should_be_armed = (self.foregrounded and not self.display.screen_off
                           and navigation.can_talk(self.state.screen))
        if should_be_armed and not self.recorder.armed:
            self.recorder.arm()
        elif not should_be_armed and self.recorder.armed:
            self.recorder.disarm()

    def _radio_is_busy(self) -> bool:
        """Is the radio mid-message, in either direction?

        Redrawing now flips the module's mode partway through a packet:
        a lost fragment inbound, or a corrupted one outbound. The screen
        can wait the second or two it takes.
        """
        if self.link is None:
            return False
        return bool(self.link.reassembling) or self.state.radio_state == SENDING

    def _next_timeout(self) -> float:
        """How long we may sleep before something needs attention.

        Returning None means "sleep until an event happens" -- the deep
        idle case, and the reason this app costs nothing when quiet.
        """
        animating = FRAME_INTERVAL.get(self.state.radio_state)
        if animating:
            return animating
        if self._radio_is_busy():
            # Nothing to draw and nothing to poll: wake on the packet.
            return None
        deadlines = [self.display.next_idle_deadline()]
        if self.state.active_banner:
            deadlines.append(max(0.05, self.state.banner_until - time.monotonic()))
        if self.link is not None and self.link.reassembling:
            deadlines.append(self.settings.power.tick_seconds)
        if self.settings.power.beacon_interval_seconds:
            deadlines.append(max(1.0, self._beacon_due - time.monotonic()))
        if self._pairing:
            deadlines.append(self._pairing_deadline())
        if self.state.battery_present:
            # A wakeup a minute is nothing against a device drawing half
            # an amp, and it is exactly when the charge matters.
            deadlines.append(max(5.0, self.battery.seconds_until_next_poll()))
        soonest = min(deadlines)
        return None if soonest == float("inf") else soonest

    def run(self):
        log.info(
            "walkie up: mode=%s radio=%s audio=%s codec2=%s",
            self.mode, "ok" if self.link else "offline",
            self.state.audio_note, self.state.codec_name,
        )
        self.gestures.start()
        # Warm the codec now: a cold open costs ~690 ms of lost speech,
        # and the operator may press talk the moment the app appears.
        if self.recorder.available:
            self.recorder.arm()
        self._beacon_due = time.monotonic() + (
            self.settings.power.beacon_interval_seconds or 1e9)
        if self.link is not None:
            self._call_known_contacts()

        while self.running:
            self._sync_state()
            # Hold the screen still while a message is in flight, then
            # catch up the moment it lands.
            if self._radio_is_busy():
                self._display_stale = True
            else:
                if self._display_stale:
                    self.display.invalidate()
                    self._display_stale = False
                screens.render(self.display, self.state)
            self.display.apply_idle_policy(keep_awake=self.state.busy)
            self._follow_idle_with_the_microphone()
            self._prompt_pending_pair()

            timeout = self._next_timeout()
            self._wake.wait(timeout)
            self._wake.clear()

            if self._pairing:
                self._pairing_tick()
            if self.link is not None:
                if self.link.reassembling:
                    self.link.tick()
                if (self.settings.power.beacon_interval_seconds
                        and time.monotonic() >= self._beacon_due):
                    self.link.send_hello()
                    self._beacon_due = time.monotonic() + \
                        self.settings.power.beacon_interval_seconds

        self._shutdown()

    def stop(self, reason: str = "normal"):
        self._exit_reason = reason
        self._closing = True
        self.running = False
        self._wake.set()

    def _shutdown(self):
        log.info("shutting down (%s)", self._exit_reason)
        try:
            self.recorder.close()
            self.player.stop()
        except Exception:
            pass
        self.gestures.stop()
        if self.link is not None:
            self.link.stop()
        if self.radio is not None:
            self.radio.close()
        if self.codec is not None:
            self.codec.close()
        self.roster.save()
        self.inbox.save()
        try:
            self.display.set_led(theme.LED_IDLE)
            # Hand the panel back lit: the daemon inherits this brightness
            # for its desktop and never resets it, so blanking here leaves
            # the user staring at what looks like broken hardware.
            self.display.restore_backlight()
            if hasattr(self.board, "prepare_exit"):
                self.board.prepare_exit()
            if hasattr(self.board, "release_focus"):
                self.board.release_focus()
            self.board.cleanup()
        except Exception:
            log.debug("board cleanup failed", exc_info=True)
        log.info(
            "frames pushed %d, skipped %d; backlight handed back at %s%%",
            self.display.frames_pushed, self.display.frames_skipped,
            self.display._backlight,
        )


def main():
    settings = settings_module.load()

    # Two instances fight the daemon for focus several times a second and
    # interleave bytes into the same radio. Refuse rather than thrash.
    try:
        lock = SingleInstance(settings.data_dir).acquire()
    except AlreadyRunning as exc:
        # Exit quietly, and above all do not ask the daemon for focus.
        # The daemon binds focus to the process it spawned and revokes it
        # when that process exits, so a stub that grabs focus and quits
        # makes the screen flick to the app and straight back to the
        # desktop, over and over. The app must be launched by the daemon,
        # not by systemd -- see tools/launch_via_daemon.py.
        log.info("%s; leaving it alone", exc)
        return 0

    app = WalkieApp(settings)

    def handle_signal(signum, _frame):
        log.info("signal %s", signum)
        app.stop("signal")

    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, handle_signal)

    try:
        app.run()
    except KeyboardInterrupt:
        app.stop("keyboard")
        app._shutdown()
    finally:
        lock.release()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
