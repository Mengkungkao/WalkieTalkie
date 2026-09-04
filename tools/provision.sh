#!/usr/bin/env bash
# Provision the LoRa module, getting everything else off the radio first.
#
#   sudo ./tools/provision.sh --address 5 --frequency 868
#
# Two things have to be out of the way, and forgetting either produces a
# misleading failure.
#
# **The walkie app holds /dev/ttyS0.** Once it autostarts it is always
# running, so provisioning cannot open the port at all -- the tool
# reports "cannot open /dev/ttyS0: Device or resource busy", which reads
# like a permissions problem. It is a user service, so stopping it needs
# no root.
#
# **whisplay-daemon drives M0/M1.** They are GPIO 22 and 27, which it
# uses for the LCD, and it redraws continuously -- overwriting M1
# microseconds after we raise it, so the module never enters config mode
# and never answers. That one reads like broken wiring. Stopping it needs
# root; without root we fall back to --force, which drives the pins
# directly and usually wins the race, then verifies by reading back.
#
# Everything stopped here is restarted from an EXIT trap, so a failure or
# a Ctrl-C still gives back the display and the radio.
set -uo pipefail
cd "$(dirname "$(dirname "$(readlink -f "$0")")")"

APP_SERVICE=walkie-talkie.service
DAEMON=whisplay-daemon
RUN_AS="${SUDO_USER:-$USER}"
APP_WAS_ACTIVE=0
DAEMON_WAS_ACTIVE=0

as_user() {
    if [ "$(id -u)" -eq 0 ] && [ "$RUN_AS" != "root" ]; then
        runuser -u "$RUN_AS" -- "$@"
    else
        "$@"
    fi
}

user_systemctl() {
    as_user env XDG_RUNTIME_DIR="/run/user/$(id -u "$RUN_AS")" systemctl --user "$@"
}

restore() {
    if [ "$APP_WAS_ACTIVE" = 1 ]; then
        echo "==> restarting $APP_SERVICE"
        user_systemctl start "$APP_SERVICE" 2>/dev/null \
            || echo "!! restart it with: systemctl --user start $APP_SERVICE" >&2
    fi
    if [ "$DAEMON_WAS_ACTIVE" = 1 ]; then
        echo "==> restarting $DAEMON"
        systemctl start "$DAEMON" 2>/dev/null \
            || echo "!! restart it with: sudo systemctl start $DAEMON" >&2
    fi
}
trap restore EXIT INT TERM

# The radio app first: it holds the serial port, and it needs no root.
if user_systemctl is-active --quiet "$APP_SERVICE" 2>/dev/null; then
    APP_WAS_ACTIVE=1
    echo "==> stopping $APP_SERVICE (it holds /dev/ttyS0)"
    user_systemctl stop "$APP_SERVICE"
fi
as_user pkill -f "python3 -m app.main" 2>/dev/null || true
sleep 1

FORCE=()
if systemctl is-active --quiet "$DAEMON"; then
    if [ "$(id -u)" -eq 0 ]; then
        DAEMON_WAS_ACTIVE=1
        echo "==> stopping $DAEMON to free GPIO 22/27 (M0/M1)"
        systemctl stop "$DAEMON"
        sleep 2
    else
        echo "==> no root: leaving $DAEMON up and driving M0/M1 directly"
        echo "    (re-run with sudo if the module does not answer)"
        FORCE=(--force)
    fi
fi

echo "==> provisioning as $RUN_AS"
as_user python3 provision_radio.py "${FORCE[@]}" "$@"
STATUS=$?

if [ $STATUS -eq 0 ]; then
    echo "==> verifying"
    as_user python3 provision_radio.py --check "${FORCE[@]}" || true
fi
exit $STATUS
