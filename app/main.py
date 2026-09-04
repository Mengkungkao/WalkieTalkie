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

import signal
import threading
import time

from app import board as board_module
from app.audio import devices as audio_devices
from app.audio.capture import Recorder
from app.audio.codec2 import (Codec2, Codec2Unavailable, MODE_BY_NAME,
                              NAME_BY_MODE, SAMPLE_RATE)
from app.audio.playback import (CUE_ERROR, CUE_RX, CUE_TX_DONE, CUE_TX_START,
                                Player)
from app.config import settings as settings_module
from app.config.settings import Contact
from app.input.button import DOUBLE, QUAD, SINGLE, TRIPLE, GestureDetector
from app.radio import protocol
from app.radio.link import LoraLink
from app.radio.sx126x import SX126x
from app.store.inbox import Inbox
from app.store.overrides import Overrides
from app.store.roster import Roster
from app.ui import navigation, screens, theme
from app.ui.display import Display
from app.ui.editors import (ChoiceEditor, ClockEditor, ConfirmEditor,
                            DigitEditor)
from app.ui.screens import (CONTACTS, EDIT, IDLE, INBOX, PLAYING, RECEIVING,
                            RECORDING, SENDING, SETTINGS, TALK, ViewState)
from app.utils import clock
from app.utils.logger import get_logger
from app.utils.single_instance import AlreadyRunning, SingleInstance

log = get_logger("main")

# Refresh cadence while something is visibly moving. Nothing animates
# when idle, so these rates apply only during a transmission or a
# recording -- seconds at a time, not continuously.
FRAME_INTERVAL = {RECORDING: 0.08, SENDING: 0.25, PLAYING: 0.3, RECEIVING: 0.3}

# A press shorter than this after the hold threshold is a slip, not speech.
MIN_TALK_SECONDS = 0.4


class WalkieApp:
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
        self.recorder = Recorder(capture_device, settings.audio.max_record_seconds)
        self.player = Player(playback_device)
        self._refresh_audio_state()

        # --- radio ------------------------------------------------------
        self.radio = None
        self.link = None
        self._open_radio()

        self._playback_lock = threading.Lock()
        self._actions = self._build_actions()
        self._refresh_entries()

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
        )
        self.link.on_message(self._on_radio_message)
        self.link.on_tx_progress(self._on_tx_progress)
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
        selected = self.roster.selected()
        self.state.target_name = selected.name if selected else ""

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
            navigation.NEXT_CONTACT: self._next_contact,
            navigation.OPEN_TALK: lambda: self._go(TALK),
            navigation.OPEN_INBOX: self._open_inbox,
            navigation.OPEN_STATUS: lambda: self._go(STATUS),
            navigation.BACK_CONTACTS: lambda: self._go(CONTACTS),
            navigation.BACK_TALK: lambda: self._go(TALK),
            navigation.NEXT_MESSAGE: self._next_message,
            navigation.PLAY_SELECTED: self._play_selected,
            navigation.REPLAY_LAST: self._replay_last,
            navigation.OPEN_SETTINGS: self._open_settings,
            navigation.NEXT_SETTING: self._next_setting,
            navigation.OPEN_SETTING: self._open_setting,
        }

    def _go(self, screen: str):
        self.state.screen = screen

    def _next_contact(self):
        self.roster.advance()
        self._refresh_entries()

    def _open_inbox(self):
        self.state.screen = INBOX
        self.state.inbox_index = 0

    def _next_message(self):
        if self.inbox.items:
            self.state.inbox_index = (
                self.state.inbox_index + 1) % len(self.inbox.items)

    # --- settings ---------------------------------------------------------
    def _settings_items(self) -> list:
        base = self.overrides.base_address
        base_name = next(
            (e.name for e in self.roster.entries() if e.address == base),
            str(base) if base is not None else "not set",
        )
        return [
            {"key": "device_id", "label": "Device ID",
             "value": f"{self.settings.radio.address}  ({self.settings.identity.callsign})"},
            {"key": "base", "label": "Base station", "value": base_name},
            {"key": "add", "label": "Add device", "value": "pair another radio by address"},
            {"key": "clock", "label": "Date & time",
             "value": clock.now().strftime("%Y-%m-%d %H:%M") + "  ·  " + clock.describe()},
            {"key": "reset", "label": "Reset all data",
             "value": f"{len(self.inbox.items)} message(s), roster, settings",
             "destructive": True},
        ]

    def _open_settings(self):
        self.state.screen = SETTINGS
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
            "device_id": self._edit_device_id, "base": self._edit_base,
            "add": self._edit_add_device, "clock": self._edit_clock,
            "reset": self._edit_reset,
        }[key]
        opener()

    def _begin_edit(self, editor, title: str, hint: str = ""):
        self.state.editor = editor
        self.state.editor_title = title
        self.state.editor_hint = hint
        self.state.screen = EDIT

    def _edit_device_id(self):
        self._begin_edit(
            DigitEditor(self.settings.radio.address, digits=5, maximum=65534),
            "DEVICE ID",
            "must be unique on the channel",
        )

    def _edit_base(self):
        choices = [(e.name, e.address) for e in self.roster.entries()
                   if not e.is_broadcast]
        choices.append(("(none)", None))
        current = self.overrides.base_address
        index = next((i for i, (_n, a) in enumerate(choices) if a == current), 0)
        self._begin_edit(ChoiceEditor(choices, index), "BASE STATION")

    def _edit_add_device(self):
        self._begin_edit(
            DigitEditor(0, digits=5, maximum=65534), "ADD DEVICE",
            "the other radio's address",
        )

    def _edit_clock(self):
        self._begin_edit(ClockEditor(clock.now()), "DATE & TIME")

    def _edit_reset(self):
        self._begin_edit(
            ConfirmEditor("Erase everything?",
                          "messages, voice clips,\nknown stations and settings"),
            "RESET",
        )

    def _editor_gesture(self, gesture: str):
        editor = self.state.editor
        if not editor.handle(gesture):
            return
        title = self.state.editor_title
        self.state.editor = None
        self.state.screen = SETTINGS
        if editor.cancelled:
            self.state.flash("cancelled")
        else:
            self._commit_edit(title, editor)
        self._refresh_settings()

    def _commit_edit(self, title: str, editor):
        if title == "DEVICE ID":
            self._apply_device_id(editor.value)
        elif title == "BASE STATION":
            self.overrides.set_base(editor.value)
            self.state.flash(f"base: {editor.text}")
        elif title == "ADD DEVICE":
            self._apply_add_device(editor.value)
        elif title == "DATE & TIME":
            how = clock.apply(editor.to_datetime(), self.overrides)
            self.state.flash("clock set" if how == "system" else "clock set (app only)")
        elif title == "RESET":
            self._apply_reset()

    def _apply_device_id(self, address: int):
        if address == self.settings.radio.address:
            return
        if any(c.address == address for c in self.settings.contacts):
            self.state.flash("that is a contact's ID", 4.0)
            self.player.cue(CUE_ERROR)
            return
        self.overrides.set("radio", "address", address)
        self.settings.radio.address = address
        # The module filters incoming packets on the address in its own
        # registers, which only provision_radio.py can change -- and that
        # needs the LCD's GPIO pins. So this takes effect for our own
        # outgoing src, and the radio must be reprovisioned to match.
        self.state.address = address
        self.state.flash(f"ID {address} — reprovision the module", 6.0)
        log.warning(
            "device id changed to %d; the module still filters on its "
            "provisioned address. Run: sudo ./tools/provision.sh --address %d",
            address, address,
        )

    def _apply_add_device(self, address: int):
        if address == self.settings.radio.address:
            self.state.flash("that is this device's ID", 4.0)
            self.player.cue(CUE_ERROR)
            return
        name = f"node {address}"
        if self.overrides.add_contact(name, address):
            self.settings.contacts.append(Contact(name=name, address=address))
            self.roster = Roster(self.settings.contacts, self.settings.data_dir)
            self._refresh_entries()
            self.state.flash(f"added {name}")
        else:
            self.state.flash("already known")

    def _apply_reset(self):
        log.warning("resetting all app data at the operator's request")
        for item in list(self.inbox.items):
            if item.voice_file:
                (self.inbox.voice_dir / item.voice_file).unlink(missing_ok=True)
        self.inbox.items = []
        self.inbox.save()
        self.overrides.clear()
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
        if self.link is None:
            self.state.flash("radio offline")
            self._wake.set()
            return
        if not self.recorder.available or self.codec is None:
            self.state.flash(self.state.audio_note or "no microphone", 3.0)
            self.player.cue(CUE_ERROR)
            self._wake.set()
            return

        self.player.stop()  # duck any playback so we do not record it
        if not self.recorder.start():
            self.state.flash("microphone busy")
            self._wake.set()
            return

        self.state.screen = TALK
        self.state.radio_state = RECORDING
        self.display.set_led(theme.LED_REC)
        self._wake.set()

    def _on_talk_end(self, held_seconds: float):
        if self.state.radio_state != RECORDING:
            return
        pcm = self.recorder.stop()
        self.state.radio_state = IDLE
        self.display.set_led(theme.LED_IDLE)

        duration = len(pcm) / 2 / SAMPLE_RATE
        if duration < MIN_TALK_SECONDS or not pcm:
            self.state.flash("too short")
            self.player.cue(CUE_ERROR)
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
        target = self.roster.selected()
        started = time.monotonic()
        try:
            encoded = self.codec.encode(pcm)
        except Exception:
            log.exception("codec2 encode failed")
            self.state.flash("encode failed")
            self.player.cue(CUE_ERROR)
            self._wake.set()
            return

        packets = max(1, -(-len(encoded) // protocol.MAX_BODY))
        log.info(
            "%.1fs speech -> %d B in %.0f ms -> %d packet(s) to %s",
            duration, len(encoded), (time.monotonic() - started) * 1000,
            packets, target.name,
        )

        airtime = self.link.budget.estimate_message(len(encoded) + packets * 11)
        if self.link.budget.remaining_seconds() < airtime:
            self.state.flash("duty cycle full", 4.0)
            self.player.cue(CUE_ERROR)
            self._wake.set()
            return

        self.state.radio_state = SENDING
        self.state.tx_sent, self.state.tx_total = 0, packets
        if self.settings.audio.cues:
            self.player.cue(CUE_TX_START)
        self.display.set_led(theme.LED_TX)
        self._wake.set()

        self.link.send_voice(target.address, encoded, self.codec_mode)
        self._record_outgoing(target, duration, len(encoded))

    def _record_outgoing(self, target, duration: float, size: int):
        sent = protocol.Message(
            type=protocol.VOICE, src=self.radio.addr, msg_id=0, body=b"",
            flags=self.codec_mode, missing=[], rssi_dbm=None,
            received_at=time.time(),
        )
        self.inbox.add_voice(sent, target.name, duration,
                             outgoing=True, store_audio=False)
        self.state.inbox = self.inbox.items

    # --- receiving ------------------------------------------------------
    def _on_radio_message(self, message, peer):
        """Called on the link's rx thread; must not block it for long."""
        self.roster.note_peer(message.src, peer.name, message.rssi_dbm)
        self.roster.save()
        self.state.last_rssi = message.rssi_dbm
        self.display.poke()

        if message.type == protocol.HELLO:
            self._refresh_entries()
            self.state.flash(f"{peer.name or message.src} on air")
            self._wake.set()
            return

        name = peer.name or f"node {message.src}"
        if message.type == protocol.TEXT:
            self.inbox.add_text(message, name)
            self.state.flash(f"{name}: {message.body.decode('utf-8', 'replace')[:24]}")
        elif message.type == protocol.VOICE:
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
                self.player.play(CUE_RX)
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
                self.player.cue(CUE_TX_DONE)
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

    def _follow_idle_with_the_microphone(self):
        """Keep the capture stream alive only while the radio is in use.

        A warm codec makes push-to-talk instant but draws current
        continuously, so arming tracks the backlight: lit means the
        operator is here and PTT must not lose their first word; blanked
        means the radio is idle and the codec should power down.
        """
        if not self.recorder.available or self.recorder.recording:
            return
        should_be_armed = not self.display.screen_off
        if should_be_armed and not self.recorder.armed:
            self.recorder.arm()
        elif not should_be_armed and self.recorder.armed:
            self.recorder.disarm()

    def _next_timeout(self) -> float:
        """How long we may sleep before something needs attention.

        Returning None means "sleep until an event happens" -- the deep
        idle case, and the reason this app costs nothing when quiet.
        """
        animating = FRAME_INTERVAL.get(self.state.radio_state)
        if animating:
            return animating
        deadlines = [self.display.next_idle_deadline()]
        if self.state.active_banner:
            deadlines.append(max(0.05, self.state.banner_until - time.monotonic()))
        if self.link is not None and self.link.reassembling:
            deadlines.append(self.settings.power.tick_seconds)
        if self.settings.power.beacon_interval_seconds:
            deadlines.append(max(1.0, self._beacon_due - time.monotonic()))
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
        if self.link and self.settings.power.beacon_interval_seconds:
            self.link.send_hello()

        while self.running:
            self._sync_state()
            screens.render(self.display, self.state)
            self.display.apply_idle_policy(keep_awake=self.state.busy)
            self._follow_idle_with_the_microphone()

            timeout = self._next_timeout()
            self._wake.wait(timeout)
            self._wake.clear()

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
        log.error("%s -- exiting", exc)
        return 1

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
