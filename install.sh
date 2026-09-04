#!/usr/bin/env bash
# Register the app with whisplay-daemon so it appears on the HAT desktop.
#
# Registration is a plain Unix-socket call and needs no privileges. The
# optional --autostart step writes a systemd user service and does need
# sudo, so it is opt-in rather than part of the default path.
set -euo pipefail
cd "$(dirname "$(readlink -f "$0")")"
HERE="$(pwd)"

AUTOSTART=0
[ "${1:-}" = "--autostart" ] && AUTOSTART=1

echo "==> preflight"
python3 - <<'CHECK'
import ctypes.util, shutil, sys
ok = True
for module in ("serial", "yaml", "PIL", "numpy"):
    try:
        __import__(module)
        print(f"    {module:<8} ok")
    except ImportError:
        print(f"    {module:<8} MISSING")
        ok = False
lib = ctypes.util.find_library("codec2")
print(f"    codec2   {lib or 'MISSING -- sudo apt install libcodec2-1.2'}")
for tool in ("arecord", "aplay"):
    print(f"    {tool:<8} {shutil.which(tool) or 'MISSING -- sudo apt install alsa-utils'}")
sys.exit(0 if ok else 1)
CHECK

echo "==> checking the serial port"
if [ ! -e /dev/ttyS0 ]; then
    echo "    /dev/ttyS0 is missing. Add enable_uart=1 to /boot/firmware/config.txt"
    echo "    and reboot, or run: sudo raspi-config -> Interface -> Serial Port"
elif fuser /dev/ttyS0 >/dev/null 2>&1; then
    echo "    WARNING: /dev/ttyS0 is held by: $(fuser -v /dev/ttyS0 2>&1 | tail -1)"
    echo "    A login console on the LoRa port corrupts every transmission."
    echo "    Remove console=serial0,115200 from /boot/firmware/cmdline.txt and reboot."
else
    echo "    /dev/ttyS0 free"
fi

echo "==> registering with whisplay-daemon"
python3 - "$HERE" <<'REGISTER'
import json, socket, sys

root = sys.argv[1]
body = {
    "version": 1, "cmd": "app.register",
    "payload": {
        "app_id": "whisplay-lora-walkie",
        "display_name": "WalkieTalkie",
        "icon": "WT",
        "launch_command": f"{root}/run.sh",
        "cwd": root,
        # The app owns every gesture: single clicks step through
        # contacts, so the daemon's 4-clicks-in-3-seconds exit would
        # fire during ordinary browsing.
        "exit_gesture": "none",
        "priority": 45,
        "persist": True,
        "use_daemon_default_log": True,
    },
}
try:
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(5)
        client.connect("/tmp/whisplay-daemon.sock")
        client.sendall((json.dumps(body) + "\n").encode())
        reply = json.loads(client.makefile("r").readline())
    print("    ", "registered" if reply.get("ok") else f"failed: {reply}")
except OSError as exc:
    print(f"     daemon not reachable ({exc}); the app still runs with ./run.sh")
REGISTER

if [ "$AUTOSTART" = "1" ]; then
    echo "==> installing systemd user service"
    mkdir -p ~/.config/systemd/user
    cat > ~/.config/systemd/user/walkie-talkie.service <<UNIT
[Unit]
Description=LoRa Walkie-Talkie
After=whisplay-daemon.service

[Service]
Type=simple
WorkingDirectory=${HERE}
ExecStart=${HERE}/run.sh
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
UNIT
    systemctl --user daemon-reload
    systemctl --user enable --now walkie-talkie.service
    echo "     enabled. Survives reboot only with: sudo loginctl enable-linger $USER"
fi

echo
echo "==> next: provision the radio once (it stores settings permanently)"
echo "    sudo systemctl stop whisplay-daemon"
echo "    python3 provision_radio.py --address 5 --frequency 868"
echo "    sudo systemctl start whisplay-daemon"
