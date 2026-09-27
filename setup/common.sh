# Shared by setup/raspberrypi.sh and setup/orangepi.sh; not run directly.
#
# Each board installer sets STEPS, sources this file (which parses the
# command line and moves to the project root), then runs its own steps
# with the helpers below. Anything that is the same on both boards --
# packages, the serial port itself, audio, the display daemon, the
# self-test -- lives here so the two installers cannot drift apart.

cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/.."
HERE="$(pwd)"

ASSUME_YES=0
CHECK_ONLY=0
ADDRESS=""
FREQUENCY=""
NEED_REBOOT=0
FAILURES=0
STEP=0

usage() {
    # The calling script's header comment, minus the leading "# ".
    awk 'NR > 1 && /^#/ { sub(/^# ?/, ""); print; next } NR > 1 { exit }' "$0"
}

while [ $# -gt 0 ]; do
    case "$1" in
        -y|--yes)     ASSUME_YES=1 ;;
        -n|--check)   CHECK_ONLY=1 ;;
        --address)    ADDRESS="$2"; shift ;;
        --frequency)  FREQUENCY="$2"; shift ;;
        -h|--help)    usage; exit 0 ;;
        *) echo "unknown option: $1" >&2; exit 1 ;;
    esac
    shift
done

BOLD=$(tput bold 2>/dev/null || true)
RESET=$(tput sgr0 2>/dev/null || true)
RED=$(tput setaf 1 2>/dev/null || true)
GREEN=$(tput setaf 2 2>/dev/null || true)
YELLOW=$(tput setaf 3 2>/dev/null || true)

step() { STEP=$((STEP + 1)); echo; echo "${BOLD}==> ${STEP}/${STEPS}  $*${RESET}"; }
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

# ---------------------------------------------------------------- host
check_host() {
    MODEL=""
    if [ -f /proc/device-tree/model ]; then
        MODEL=$(tr -d '\0' < /proc/device-tree/model)
        ok "$MODEL"
    else
        warn "no device tree model -- is this the right board?"
    fi
    info "$(. /etc/os-release 2>/dev/null && echo "$PRETTY_NAME") · python $(python3 -V 2>&1 | cut -d' ' -f2)"
}

# ------------------------------------------------------------ packages
# codec2 ships under a versioned name: libcodec2-1.2 on Debian 13 and
# Ubuntu 24.04, libcodec2-1.0 on Debian 12 and Ubuntu 22.04. The ctypes
# binding loads either, so take whichever this release has.
codec2_package() {
    local pkg
    for pkg in libcodec2-1.2 libcodec2-1.0; do
        dpkg -s "$pkg" >/dev/null 2>&1 && { echo "$pkg"; return; }
    done
    for pkg in libcodec2-1.2 libcodec2-1.0; do
        apt-cache show "$pkg" >/dev/null 2>&1 && { echo "$pkg"; return; }
    done
    echo libcodec2-1.2
}

install_packages() {
    # install_packages pkg...   (libcodec2 is added and resolved here)
    local missing=() pkg
    for pkg in "$@" "$(codec2_package)"; do
        if dpkg -s "$pkg" >/dev/null 2>&1; then
            ok "$pkg"
        else
            warn "$pkg missing"
            missing+=("$pkg")
        fi
    done
    if [ ${#missing[@]} -gt 0 ]; then
        info "needed: ${missing[*]}"
        if ask "install them with apt now?"; then
            if sudo apt-get update -qq; then
                # A fresh image has empty package lists, so codec2 may
                # have been guessed; name it again now they are filled.
                missing=("${missing[@]/#libcodec2-*/$(codec2_package)}")
                sudo apt-get install -y "${missing[@]}" \
                    && ok "installed" || bad "apt install failed"
            else
                bad "apt-get update failed"
            fi
        else
            bad "install manually: sudo apt install ${missing[*]}"
        fi
    fi

    python3 - <<'PY'
import ctypes.util, sys
lib = ctypes.util.find_library("codec2")
print(f"    {'ok  ' if lib else 'fail'} libcodec2: {lib or 'NOT FOUND'}")
sys.exit(0 if lib else 1)
PY
    [ $? -eq 0 ] || bad "libcodec2 is required for voice"
}

# --------------------------------------------------------- serial port
disable_serial_getty() {
    # disable_serial_getty ttyS0
    [ "$CHECK_ONLY" = 1 ] && return
    sudo systemctl disable --now "serial-getty@$1.service" >/dev/null 2>&1 || true
}

check_port() {
    # check_port /dev/ttyS0
    local port=$1
    if [ -e "$port" ]; then
        ok "$port exists"
        if fuser "$port" >/dev/null 2>&1; then
            HOLDER=$(fuser -v "$port" 2>&1 | tail -1 | awk '{print $NF}')
            warn "$port is held by '$HOLDER'"
            [ "$NEED_REBOOT" = 0 ] && bad "free the port before running the app"
        else
            ok "$port is free"
        fi
    else
        warn "$port does not exist yet"
        [ "$NEED_REBOOT" = 0 ] && bad "the UART is not enabled"
    fi

    id -nG "$USER" | tr ' ' '\n' | grep -qx dialout \
        && ok "$USER is in the dialout group" \
        || { warn "$USER is not in dialout"; ask "add?" && sudo usermod -aG dialout "$USER" \
             && info "log out and back in for it to apply"; }
}

# --------------------------------------------------------------- audio
check_audio() {
    # Ask the app which cards it would open, rather than guessing here: a
    # first-card guess reported the Orange Pi's HDMI as the microphone
    # while the app was correctly using the Whisplay codec.
    local kind device name found=0
    while read -r kind device name; do
        found=1
        if [ "$device" != "-" ]; then
            ok "$kind: $name ($device)"
        elif [ "$kind" = capture ]; then
            warn "no capture device -- voice recording will be unavailable"
            if [ -e /sys/kernel/debug/devices_deferred ] && \
               sudo grep -q sound /sys/kernel/debug/devices_deferred 2>/dev/null; then
                info "the sound card is stuck in deferred probe:"
                sudo sed 's/^/           /' /sys/kernel/debug/devices_deferred
                info "the Whisplay codec driver is not finishing its probe; text"
                info "messaging works regardless, voice needs this fixed first."
            fi
        else
            warn "no playback device other than HDMI -- voice playback unavailable"
        fi
    done < <(python3 - 2>/dev/null <<'PY'
import logging

from app.audio import devices
from app.config import settings

logging.disable(logging.CRITICAL)  # the app logs to stdout, which is our reply
audio = settings.load().audio
for kind, configured, cards in (
        ("capture", audio.capture_device, devices.capture_cards()),
        ("playback", audio.playback_device, devices.playback_cards())):
    device = devices.resolve(configured, kind, audio.preferred_card)
    try:
        name = dict(cards).get(int(device.rsplit(":", 1)[-1]), device)
    except (AttributeError, ValueError):
        name = device  # None, or an explicit device such as "default"
    print(kind, device or "-", name or "-")
PY
)
    [ "$found" = 1 ] || warn "could not ask the app which sound cards it would use"
}

# -------------------------------------------------------------- daemon
register_with_daemon() {
    if systemctl is-active --quiet whisplay-daemon; then
        ok "whisplay-daemon is running"
        if [ "$CHECK_ONLY" = 0 ]; then
            ./install.sh 2>&1 | sed -n '/registering/,$p' | sed 's/^/    /'
        fi
    else
        warn "whisplay-daemon is not running -- the app will drive the HAT directly"
    fi
}

# ---------------------------------------------------------------- test
run_selftest() {
    if python3 -m pytest tests -q >/tmp/walkie-tests.log 2>&1; then
        ok "$(tail -1 /tmp/walkie-tests.log)"
    else
        warn "tests failed -- see /tmp/walkie-tests.log"
        [ -x "$(command -v pytest)" ] || info "(install with: sudo apt install python3-pytest)"
    fi
}

finish() {
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
}
