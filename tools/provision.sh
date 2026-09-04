#!/usr/bin/env bash
# Provision the LoRa module, borrowing the display's GPIO pins safely.
#
#   sudo ./tools/provision.sh --address 5 --frequency 868
#
# Why this wrapper exists: setting the module's frequency, address and
# air rate requires driving M0/M1, which are GPIO 22 and 27 -- the same
# lines whisplay-daemon uses for the LCD. While the daemon runs it
# redraws continuously, overwriting M1 microseconds after we raise it,
# so the module never enters config mode and simply never answers. The
# symptom is "no reply from the module", which reads like broken wiring.
#
# So the daemon is stopped for the few seconds this takes, and restarted
# afterwards even if provisioning fails or you Ctrl-C out -- leaving the
# display dead would be a far worse outcome than an unprovisioned radio.
set -uo pipefail
cd "$(dirname "$(dirname "$(readlink -f "$0")")")"

DAEMON=whisplay-daemon
WAS_ACTIVE=0
systemctl is-active --quiet "$DAEMON" && WAS_ACTIVE=1

restore() {
    if [ "$WAS_ACTIVE" = 1 ]; then
        echo "==> restarting $DAEMON"
        systemctl start "$DAEMON" || echo "!! could not restart $DAEMON -- run: sudo systemctl start $DAEMON" >&2
    fi
}
trap restore EXIT INT TERM

if [ "$(id -u)" -ne 0 ]; then
    echo "!! run with sudo: sudo $0 $*" >&2
    exit 1
fi

if [ "$WAS_ACTIVE" = 1 ]; then
    echo "==> stopping $DAEMON to free GPIO 22/27 (M0/M1)"
    systemctl stop "$DAEMON"
    sleep 2
fi

# Drop back to the invoking user: provisioning needs /dev/ttyS0 (dialout)
# and /dev/gpiomem (gpio), not root, and running it as root would leave
# root-owned __pycache__ behind in the working tree.
RUN_AS="${SUDO_USER:-$(logname 2>/dev/null || echo root)}"
echo "==> provisioning as $RUN_AS"
runuser -u "$RUN_AS" -- python3 provision_radio.py --force "$@"
STATUS=$?

if [ $STATUS -eq 0 ]; then
    echo "==> verifying"
    runuser -u "$RUN_AS" -- python3 provision_radio.py --check --force
fi
exit $STATUS
