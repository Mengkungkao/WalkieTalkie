#!/usr/bin/env bash
#
# One-shot setup for the LoRa Walkie-Talkie on a Raspberry Pi with a
# Whisplay HAT and a Waveshare SX126X LoRa HAT.
#
#   ./setup.sh                 walk through every step, asking before changes
#   ./setup.sh --yes           accept every prompt (unattended)
#   ./setup.sh --check         report only; change nothing
#   ./setup.sh --address 7     set this node's LoRa address while provisioning
#
# Steps that need a reboot say so and stop rather than pretending to have
# worked. Run the script again afterwards; it is safe to re-run and skips
# anything already done.
set -uo pipefail

cd "$(dirname "$(readlink -f "$0")")"
HERE="$(pwd)"

ASSUME_YES=0
CHECK_ONLY=0
ADDRESS=""
FREQUENCY=""
NEED_REBOOT=0
FAILURES=0

while [ $# -gt 0 ]; do
    case "$1" in
        -y|--yes)     ASSUME_YES=1 ;;
        -n|--check)   CHECK_ONLY=1 ;;
        --address)    ADDRESS="$2"; shift ;;
        --frequency)  FREQUENCY="$2"; shift ;;
        -h|--help)    sed -n '2,14p' "$0" | sed 's/^# \?//'; exit 0 ;;
        *) echo "unknown option: $1" >&2; exit 1 ;;
    esac
    shift
done

BOLD=$(tput bold 2>/dev/null || true)
RESET=$(tput sgr0 2>/dev/null || true)
RED=$(tput setaf 1 2>/dev/null || true)
GREEN=$(tput setaf 2 2>/dev/null || true)
YELLOW=$(tput setaf 3 2>/dev/null || true)

step() { echo; echo "${BOLD}==> $*${RESET}"; }
ok()   { echo "    ${GREEN}ok${RESET}   $*"; }
warn() { echo "    ${YELLOW}warn${RESET} $*"; }
bad()  { echo "    ${RED}fail${RESET} $*"; FAILURES=$((FAILURES + 1)); }
info() { echo "         $*"; }

ask() {
    # ask "question"  -> 0 for yes
    [ "$CHECK_ONLY" = 1 ] && { info "(check only: skipping)"; return 1; }
    [ "$ASSUME_YES" = 1 ] && return 0
    read -r -p "         $1 [y/N] " reply
    [[ "$reply" =~ ^[Yy] ]]
}

# ---------------------------------------------------------------- 1. host
step "1/8  Checking the host"
if [ -f /proc/device-tree/model ]; then
    MODEL=$(tr -d '\0' < /proc/device-tree/model)
    ok "$MODEL"
else
    warn "not a Raspberry Pi -- hardware steps will be skipped"
fi
info "$(. /etc/os-release 2>/dev/null && echo "$PRETTY_NAME") · python $(python3 -V 2>&1 | cut -d' ' -f2)"

BOOT_CONFIG=/boot/firmware/config.txt
BOOT_CMDLINE=/boot/firmware/cmdline.txt
[ -f "$BOOT_CONFIG" ]  || BOOT_CONFIG=/boot/config.txt
[ -f "$BOOT_CMDLINE" ] || BOOT_CMDLINE=/boot/cmdline.txt

# ------------------------------------------------------------ 2. packages
step "2/8  System packages"
APT_NEEDED=()
for pkg in python3-serial python3-yaml python3-pil python3-numpy \
           libcodec2-1.2 alsa-utils; do
    if dpkg -s "$pkg" >/dev/null 2>&1; then
        ok "$pkg"
    else
        warn "$pkg missing"
        APT_NEEDED+=("$pkg")
    fi
done
if [ ${#APT_NEEDED[@]} -gt 0 ]; then
    info "needed: ${APT_NEEDED[*]}"
    if ask "install them with apt now?"; then
        sudo apt-get update -qq && sudo apt-get install -y "${APT_NEEDED[@]}" \
            && ok "installed" || bad "apt install failed"
    else
        bad "install manually: sudo apt install ${APT_NEEDED[*]}"
    fi
fi

python3 - <<'PY'
import ctypes.util, sys
lib = ctypes.util.find_library("codec2")
print(f"    {'ok  ' if lib else 'fail'} libcodec2: {lib or 'NOT FOUND'}")
sys.exit(0 if lib else 1)
PY
[ $? -eq 0 ] || bad "libcodec2 is required for voice"

# ---------------------------------------------------------------- 3. UART
step "3/8  Serial port for the LoRa HAT"
if ! grep -qE '^\s*enable_uart=1' "$BOOT_CONFIG" 2>/dev/null; then
    warn "enable_uart=1 is not in $BOOT_CONFIG"
    if ask "add it?"; then
        echo "enable_uart=1" | sudo tee -a "$BOOT_CONFIG" >/dev/null
        ok "added -- takes effect after a reboot"
        NEED_REBOOT=1
    else
        bad "the LoRa HAT needs the UART enabled"
    fi
else
    ok "enable_uart=1 present in $BOOT_CONFIG"
fi

if [ "$(vcgencmd get_config enable_uart 2>/dev/null)" = "enable_uart=0" ]; then
    warn "the running firmware still has the UART off -- config.txt was edited"
    warn "after the last boot. A reboot is required."
    NEED_REBOOT=1
fi

# A kernel console or login prompt on the LoRa port corrupts every packet.
if grep -qE 'console=(serial0|ttyS0|ttyAMA0)' "$BOOT_CMDLINE" 2>/dev/null; then
    warn "a serial console is configured on the LoRa port in $BOOT_CMDLINE"
    info "kernel messages and a login prompt would be injected into your"
    info "transmissions, and agetty holds the port open."
    if ask "remove the serial console from cmdline.txt?"; then
        sudo cp "$BOOT_CMDLINE" "${BOOT_CMDLINE}.walkie.bak"
        sudo sed -i -E 's/console=(serial0|ttyS0|ttyAMA0),[0-9]+ ?//g' "$BOOT_CMDLINE"
        ok "removed (backup at ${BOOT_CMDLINE}.walkie.bak) -- reboot required"
        NEED_REBOOT=1
    else
        bad "the radio will be unreliable while a console shares the port"
    fi
else
    ok "no serial console on the LoRa port"
fi

sudo systemctl disable --now serial-getty@ttyS0.service >/dev/null 2>&1 || true

if [ -e /dev/ttyS0 ]; then
    ok "/dev/ttyS0 exists"
    if fuser /dev/ttyS0 >/dev/null 2>&1; then
        HOLDER=$(fuser -v /dev/ttyS0 2>&1 | tail -1 | awk '{print $NF}')
        warn "/dev/ttyS0 is held by '$HOLDER'"
        [ "$NEED_REBOOT" = 0 ] && bad "free the port before running the app"
    else
        ok "/dev/ttyS0 is free"
    fi
else
    warn "/dev/ttyS0 does not exist yet"
    [ "$NEED_REBOOT" = 0 ] && bad "the UART is not enabled"
fi

id -nG "$USER" | tr ' ' '\n' | grep -qx dialout \
    && ok "$USER is in the dialout group" \
    || { warn "$USER is not in dialout"; ask "add?" && sudo usermod -aG dialout "$USER" \
         && info "log out and back in for it to apply"; }

# ------------------------------------------------------------ 4. GPIO M0/M1
step "4/8  LoRa mode pins (M0/M1)"
CONFLICT=$(gpioinfo 2>/dev/null | grep -E 'line +(22|27):' | grep 'consumer=' || true)
if [ -n "$CONFLICT" ]; then
    warn "GPIO 22 and/or 27 are in use:"
    echo "$CONFLICT" | sed 's/^/         /'
    info "These are the SX126X M0/M1 pins AND the Whisplay LCD control pins."
    info "The app never drives them: it runs the module in transparent mode"
    info "using settings stored permanently by provision_radio.py (step 7)."
    info "Remove the M0/M1 jumpers on the LoRa HAT so the two boards do not"
    info "fight over these lines."
else
    ok "GPIO 22/27 are free"
fi

# --------------------------------------------------------------- 5. audio
step "5/8  Audio hardware"
CAPTURE=$(arecord -l 2>/dev/null | grep -c '^card' || true)
PLAYBACK=$(aplay -l 2>/dev/null | grep -v 'vc4hdmi' | grep -c '^card' || true)
if [ "$CAPTURE" -gt 0 ]; then
    ok "capture: $(arecord -l 2>/dev/null | grep '^card' | head -1)"
else
    warn "no capture device -- voice recording will be unavailable"
    if [ -e /sys/kernel/debug/devices_deferred ] && \
       sudo grep -q sound /sys/kernel/debug/devices_deferred 2>/dev/null; then
        info "the sound card is stuck in deferred probe:"
        sudo sed 's/^/           /' /sys/kernel/debug/devices_deferred
        info "the Whisplay codec driver is not finishing its probe; text"
        info "messaging works regardless, voice needs this fixed first."
    fi
fi
if [ "$PLAYBACK" -gt 0 ]; then
    ok "playback: $(aplay -l 2>/dev/null | grep '^card' | grep -v vc4hdmi | head -1)"
else
    warn "no playback device other than HDMI -- voice playback unavailable"
fi

# -------------------------------------------------------------- 6. daemon
step "6/8  Registering with whisplay-daemon"
if systemctl is-active --quiet whisplay-daemon; then
    ok "whisplay-daemon is running"
    if [ "$CHECK_ONLY" = 0 ]; then
        ./install.sh 2>&1 | sed -n '/registering/,$p' | sed 's/^/    /'
    fi
else
    warn "whisplay-daemon is not running -- the app will drive the HAT directly"
fi

# ------------------------------------------------------------ 7. provision
step "7/8  Provisioning the radio module"
if [ "$NEED_REBOOT" = 1 ]; then
    warn "skipped: reboot first so the UART comes up"
elif [ ! -e /dev/ttyS0 ]; then
    warn "skipped: no /dev/ttyS0"
else
    ADDR_ARG=(); [ -n "$ADDRESS" ] && ADDR_ARG=(--address "$ADDRESS")
    FREQ_ARG=(); [ -n "$FREQUENCY" ] && FREQ_ARG=(--frequency "$FREQUENCY")
    info "This writes frequency, address and air rate into the module's"
    info "non-volatile registers. It needs GPIO 22/27 for a few seconds, so"
    info "the display daemon is stopped and restarted around it."
    if ask "provision the module now?"; then
        sudo systemctl stop whisplay-daemon 2>/dev/null || true
        sleep 1
        if python3 provision_radio.py "${ADDR_ARG[@]}" "${FREQ_ARG[@]}" 2>&1 | sed 's/^/         /'; then
            ok "module provisioned"
        else
            bad "provisioning failed -- check the HAT is seated and powered"
        fi
        sudo systemctl start whisplay-daemon 2>/dev/null || true
    else
        info "later:  sudo systemctl stop whisplay-daemon"
        info "        python3 provision_radio.py --address 5 --frequency 868"
        info "        sudo systemctl start whisplay-daemon"
    fi
fi

# ----------------------------------------------------------------- 8. test
step "8/8  Self-test"
if python3 -m pytest tests -q >/tmp/walkie-tests.log 2>&1; then
    ok "$(tail -1 /tmp/walkie-tests.log)"
else
    warn "tests failed -- see /tmp/walkie-tests.log"
    [ -x "$(command -v pytest)" ] || info "(install with: sudo apt install python3-pytest)"
fi

echo
if [ "$NEED_REBOOT" = 1 ]; then
    echo "${BOLD}${YELLOW}A reboot is required.${RESET}  sudo reboot"
    echo "Then run ./setup.sh again to finish."
elif [ "$FAILURES" -gt 0 ]; then
    echo "${BOLD}${YELLOW}Setup finished with $FAILURES issue(s) above.${RESET}"
else
    echo "${BOLD}${GREEN}Ready.${RESET}"
fi
echo
echo "Start it:   ./run.sh          (or pick 'WalkieTalkie' on the HAT desktop)"
echo "Controls:   hold = talk · 1 click = next · 2 = select · 3 = replay · 4 = exit"
