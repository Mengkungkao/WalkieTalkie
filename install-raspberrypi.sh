#!/usr/bin/env bash
#
# Raspberry Pi installer for the LoRa Walkie-Talkie (Whisplay HAT plus a
# Waveshare SX126X LoRa HAT) on Raspberry Pi OS. ./setup.sh runs this on
# a Pi; it can also be run directly.
#
#   ./install-raspberrypi.sh               walk through every step, asking first
#   ./install-raspberrypi.sh --yes         accept every prompt (unattended)
#   ./install-raspberrypi.sh --check       report only; change nothing
#   ./install-raspberrypi.sh --frequency 915   provision the module for another band
#   ./install-raspberrypi.sh --range long      provision for range (2.4k air; every radio)
#
# Steps that need a reboot say so and stop rather than pretending to have
# worked. Run it again afterwards; it is safe to re-run and skips
# anything already done.
set -uo pipefail

STEPS=8
PORT=/dev/ttyS0
# shellcheck source=setup/common.sh
. "$(dirname "$(readlink -f "$0")")/setup/common.sh"

# ---------------------------------------------------------------- 1. host
step "Checking the host"
check_host

BOOT_CONFIG=/boot/firmware/config.txt
BOOT_CMDLINE=/boot/firmware/cmdline.txt
[ -f "$BOOT_CONFIG" ]  || BOOT_CONFIG=/boot/config.txt
[ -f "$BOOT_CMDLINE" ] || BOOT_CMDLINE=/boot/cmdline.txt

# ------------------------------------------------------------ 2. packages
step "System packages"
install_packages python3-serial python3-yaml python3-pil python3-numpy \
                 python3-cryptography alsa-utils

# ---------------------------------------------------------------- 3. UART
step "Serial port for the LoRa HAT"
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

disable_serial_getty ttyS0
check_port "$PORT"

# ------------------------------------------------------------ 4. GPIO M0/M1
step "LoRa mode pins (M0/M1)"
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
step "Audio hardware"
check_audio

# -------------------------------------------------------------- 6. daemon
step "Registering with whisplay-daemon"
RUNTIME=$(find_whisplay_runtime)
if [ -n "$RUNTIME" ]; then
    check_dc_fix "$RUNTIME"
else
    warn "no Whisplay runtime found -- install PiSugar's Whisplay driver first"
fi
register_with_daemon

# ------------------------------------------------------------ 7. provision
step "Provisioning the radio module"
provision_module "$PORT"

# ----------------------------------------------------------------- 8. test
step "Self-test"
run_selftest

finish
