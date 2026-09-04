"""Getting hold of the Whisplay hardware.

Under `whisplay-daemon` the app never touches SPI or GPIO: the daemon
owns the LCD, the RGB LED, the backlight and the button, and hands out a
shared framebuffer plus an event stream. That separation is what makes
the LoRa HAT workable at all here -- the daemon is already driving GPIO
22 and 27 for the display, which are the same pins the SX126X uses for
M0/M1, so an app that grabbed GPIO directly would fight the screen.

`exit_gesture` is "none" on purpose. The daemon's default is four clicks
inside a 3-second window, and this app uses single clicks to step
through contacts and inbox items -- a user moving briskly down a list
produces four clicks in three seconds routinely and would be killed
mid-transmission. With "none" the app owns every gesture and handles
exit itself, using the much tighter 700 ms click window.
"""

from __future__ import annotations

import os
import sys
import threading
import time
from pathlib import Path

from app.utils.logger import get_logger

log = get_logger("board")

APP_ID = "whisplay-lora-walkie"
DISPLAY_NAME = "LoRa Walkie"
ICON = "LW"
EXIT_GESTURE = "none"
PRIORITY = 45

FOREGROUND_RETRY_SECONDS = 5.0


def _runtime_candidates() -> list:
    project_root = Path(__file__).resolve().parents[1]
    candidates = []
    env_path = os.getenv("WHISPLAY_RUNTIME")
    if env_path:
        candidates.append(Path(env_path))
    candidates += [
        Path.home() / "Whisplay" / "runtime",
        project_root.parent / "Whisplay" / "runtime",
        Path("/home/pi/Whisplay/runtime"),
        Path("/opt/whisplay/runtime"),
        Path("/usr/local/share/whisplay/runtime"),
    ]
    return candidates


def _import_client():
    for candidate in _runtime_candidates():
        if not (candidate / "whisplay_client.py").is_file():
            continue
        path = str(candidate)
        if path not in sys.path:
            sys.path.append(path)
        try:
            import whisplay_client
        except ImportError as exc:
            # Expected off-device: the runtime imports spidev/gpiod.
            log.warning("whisplay runtime at %s is unusable: %s", path, exc)
            return None
        log.info("using whisplay runtime at %s", path)
        return whisplay_client
    return None


class NullBoard:
    """Headless stand-in for development machines and PNG previews."""

    width = 240
    height = 280
    foreground_ready = True

    def fill_screen(self, colour): pass
    def draw_image(self, x, y, width, height, pixels): pass
    def set_backlight(self, brightness): pass
    def set_rgb(self, r, g, b): pass
    def set_rgb_fade(self, r, g, b, duration_ms=100): pass
    def button_pressed(self): return False
    def on_button_press(self, callback): pass
    def on_button_release(self, callback): pass
    def on_exit_request(self, callback): pass
    def on_focus_revoked(self, callback): pass
    def start_event_listener(self): pass
    def release_focus(self): pass
    def prepare_exit(self): pass
    def cleanup(self): pass


def _start_foreground_retry(proxy, on_acquired=None,
                            interval: float = FOREGROUND_RETRY_SECONDS):
    """Keep asking for the screen until whoever holds it lets go.

    Without this an app autostarted at boot sits headless forever --
    receiving packets and drawing to nothing -- because another app
    happened to own the foreground at the moment it started.
    """

    def loop():
        attempts = 0
        while not getattr(proxy, "foreground_ready", False):
            time.sleep(interval)
            attempts += 1
            try:
                proxy.acquire_foreground(timeout_sec=1.0)
            except Exception:
                continue
            proxy.foreground_ready = True
            log.info("foreground acquired after %d attempt(s)", attempts)
            if on_acquired:
                try:
                    on_acquired()
                except Exception:
                    log.exception("foreground callback failed")
            return

    threading.Thread(target=loop, name="foreground-retry", daemon=True).start()


def acquire_board(launch_command: str | None = None,
                  launch_cwd: str | None = None,
                  on_foreground_acquired=None):
    """Return (board, mode): daemon | waiting | direct | headless."""
    client = _import_client()
    if client is None:
        log.warning("whisplay runtime not found; running headless")
        return NullBoard(), "headless"

    project_root = Path(__file__).resolve().parents[1]
    launch_command = launch_command or str(project_root / "run.sh")
    launch_cwd = launch_cwd or str(project_root)

    proxy = None
    daemon_alive = False
    try:
        proxy = client.WhisplayDaemonProxy(
            socket_path=client.DEFAULT_DAEMON_SOCKET_PATH,
            app_id=APP_ID, display_name=DISPLAY_NAME, icon=ICON,
            launch_command=launch_command, launch_cwd=launch_cwd,
            persist=True, exit_gesture=EXIT_GESTURE, priority=PRIORITY,
            use_daemon_default_log=True,
        )
        daemon_alive = proxy.ping()
    except Exception:
        log.exception("could not talk to the whisplay daemon")

    if not daemon_alive:
        try:
            board = client.WhisplayBoard()
            log.info("no daemon; driving the HAT directly over SPI")
            board.foreground_ready = True
            return board, "direct"
        except Exception:
            log.exception("direct hardware access failed; running headless")
            return NullBoard(), "headless"

    proxy.register()
    proxy.start_event_listener()
    try:
        proxy.acquire_foreground(timeout_sec=2.0)
        proxy.foreground_ready = True
        return proxy, "daemon"
    except Exception:
        # Another app owns the screen. Keep the radio running and take
        # the display over as soon as it is free.
        proxy.foreground_ready = False
        log.warning("another app holds the screen; retrying in the background")
        _start_foreground_retry(proxy, on_foreground_acquired)
        return proxy, "waiting"
