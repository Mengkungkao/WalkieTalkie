#!/usr/bin/env bash
#
# Orange Pi Zero 2W installer for the LoRa Walkie-Talkie (Whisplay HAT
# plus a Waveshare SX126X LoRa HAT) on Orange Pi OS or Armbian. ./setup.sh
# runs this on an Orange Pi; it can also be run directly.
#
#   ./install-orangepi-zero2w.sh               walk through every step, asking first
#   ./install-orangepi-zero2w.sh --yes         accept every prompt (unattended)
#   ./install-orangepi-zero2w.sh --check       report only; change nothing
#
# What differs from the Raspberry Pi: header pins 8/10 are UART0, which
# is also this board's debug console, so the console is moved off it in
# /boot/orangepiEnv.txt; the Whisplay HAT needs PiSugar's Orange Pi
# driver; and the radio cannot be provisioned from this board yet.
set -uo pipefail

STEPS=8
PORT=/dev/ttyS0
# shellcheck source=setup/common.sh
. "$(dirname "$(readlink -f "$0")")/setup/common.sh"

# ---------------------------------------------------------------- 1. host
step "Checking the host"
check_host
case "$MODEL" in
    *[Zz]ero*2*[Ww]*) ;;
    *) warn "written for the Orange Pi Zero 2W; the pin names below may not match this board" ;;
esac

# Orange Pi OS reads orangepiEnv.txt, Armbian armbianEnv.txt. Both boot
# scripts treat a missing console= as "both", i.e. serial included.
BOOT_ENV=/boot/orangepiEnv.txt
[ -f "$BOOT_ENV" ] || BOOT_ENV=/boot/armbianEnv.txt

# ------------------------------------------------------------ 2. packages
step "System packages"
install_packages python3-serial python3-yaml python3-pil python3-numpy \
                 python3-cryptography alsa-utils gpiod

# ---------------------------------------------------------------- 3. UART
step "Serial port for the LoRa HAT"
info "Header pins 8/10 are UART0 ($PORT), which is also this board's"
info "debug console."

# What console= must be depends on the boot script. Armbian's keeps
# "display" off the serial port. Orange Pi OS's puts ttyS0 on the kernel
# command line for "display" as well as "both" -- on the Zero 2W image,
# console=display still booted with console=ttyS0 and a shell logged in
# on the LoRa port. There only a value the script ignores keeps ttyS0
# off, and the screen console comes back through extraargs.
BOOT_CMD="$(dirname "$BOOT_ENV")/boot.cmd"
if grep -qE '"display".*console=ttyS0' "$BOOT_CMD" 2>/dev/null; then
    WANT_CONSOLE=none
    WANT_EXTRA=console=tty1
else
    WANT_CONSOLE=display
    WANT_EXTRA=""
fi

set_env() {
    # set_env key value -- in $BOOT_ENV, replacing any earlier value
    if grep -q "^$1=" "$BOOT_ENV"; then
        sudo sed -i "s|^$1=.*|$1=$2|" "$BOOT_ENV"
    else
        echo "$1=$2" | sudo tee -a "$BOOT_ENV" >/dev/null
    fi
}

ENV_OK=0
if [ ! -f "$BOOT_ENV" ]; then
    warn "neither /boot/orangepiEnv.txt nor /boot/armbianEnv.txt exists"
    bad "move the kernel console off ttyS0 by hand"
else
    CONSOLE=$(sed -n 's/^console=//p' "$BOOT_ENV" | tail -1)
    EXTRA=$(sed -n 's/^extraargs=//p' "$BOOT_ENV" | tail -1)
    EXTRA_OK=1
    if [ -n "$WANT_EXTRA" ] && ! printf ' %s ' "$EXTRA" | grep -q " $WANT_EXTRA "; then
        EXTRA_OK=0
    fi
    if [ "$CONSOLE" = "$WANT_CONSOLE" ] && [ "$EXTRA_OK" = 1 ]; then
        ok "console=$CONSOLE${WANT_EXTRA:+ and extraargs=$EXTRA} in $BOOT_ENV"
        ENV_OK=1
    else
        warn "console=${CONSOLE:-both (the default)} in $BOOT_ENV puts the kernel console on ttyS0"
        [ "$WANT_CONSOLE" = none ] && \
            info "(this image's boot script adds ttyS0 for console=display too)"
        info "Orange Pi OS also logs a shell in there automatically: whatever"
        info "the radio receives is typed into it, its output is transmitted,"
        info "and each time it restarts it hangs the port up under the app."
        if ask "move the console off the LoRa port?"; then
            [ -f "${BOOT_ENV}.walkie.bak" ] || sudo cp "$BOOT_ENV" "${BOOT_ENV}.walkie.bak"
            set_env console "$WANT_CONSOLE"
            [ "$EXTRA_OK" = 0 ] && set_env extraargs "${EXTRA:+$EXTRA }$WANT_EXTRA"
            ok "set console=$WANT_CONSOLE${WANT_EXTRA:+ and $WANT_EXTRA} (backup at ${BOOT_ENV}.walkie.bak) -- reboot required"
            NEED_REBOOT=1
        else
            bad "the radio will be unreliable while a console shares the port"
        fi
    fi
fi

# The file can be right and the running kernel still started without it.
if grep -q 'console=ttyS0' /proc/cmdline; then
    if [ "$ENV_OK" = 1 ]; then
        warn "the running kernel still has its console on ttyS0 -- $BOOT_ENV"
        warn "was changed after the last boot. A reboot is required."
        NEED_REBOOT=1
    fi
else
    ok "the running kernel has no console on ttyS0"
fi
info "U-Boot still prints to this port for a moment at power-on, before"
info "Linux starts; that is not something $(basename "$BOOT_ENV") can turn off."

# Masked, not just disabled: systemd starts a login on every console the
# kernel names, whatever the unit says, and this image adds an auto-login
# drop-in to it.
GETTY_STATE=$(systemctl is-enabled serial-getty@ttyS0.service 2>/dev/null)
if [ "$GETTY_STATE" = masked ]; then
    ok "the serial login on ttyS0 is masked"
else
    warn "the serial login on ttyS0 is ${GETTY_STATE:-not masked}"
    if ask "mask it, so it can never log a shell in on the LoRa port?"; then
        sudo systemctl mask --now serial-getty@ttyS0.service >/dev/null 2>&1 \
            && ok "masked" || bad "could not mask serial-getty@ttyS0"
    else
        bad "a shell may be logged in on the LoRa port"
    fi
fi
check_port "$PORT"

# ------------------------------------------------------------ 4. GPIO M0/M1
step "LoRa mode pins (M0/M1)"
# The LoRa HAT's M0/M1 jumpers land on header pins 15 and 13. On a Pi
# those are GPIO 22/27; here they are PI5 and PH3 -- lines 261 and 227
# of the main pin controller -- and the Whisplay HAT uses them for the
# LCD backlight and data/command lines, exactly as on the Pi.
GPIO_SUDO=""
gpiodetect >/dev/null 2>&1 || GPIO_SUDO=sudo
PIO_CHIP=$($GPIO_SUDO gpiodetect 2>/dev/null | awk '/\[300b000\.pinctrl\]/ { print $1; exit }')
if ! command -v gpiodetect >/dev/null; then
    warn "cannot inspect the pins: gpiodetect is missing (the gpiod package, step 2)"
elif [ -z "$PIO_CHIP" ]; then
    warn "cannot find the H618 pin controller (300b000.pinctrl) in gpiodetect"
else
    CONFLICT=$($GPIO_SUDO gpioinfo 2>/dev/null \
        | awk -v chip="$PIO_CHIP" '/^gpiochip/ { cur = $1; next }
                                   cur == chip && /line +(227|261):/' \
        | grep -E '\[used\]|consumer=' || true)
    if [ -n "$CONFLICT" ]; then
        warn "PI5 (pin 15) and/or PH3 (pin 13) are in use:"
        echo "$CONFLICT" | sed 's/^/         /'
        info "These are the SX126X M0/M1 pins AND the Whisplay LCD control pins."
        info "The app never drives them: it runs the module in transparent mode"
        info "using settings stored permanently in the module (step 7)."
        info "Remove the M0/M1 jumpers on the LoRa HAT so the two boards do not"
        info "fight over these lines."
    else
        ok "PI5/PH3 (pins 15/13) are free"
    fi
fi

# ------------------------------------------------------------- 5. Whisplay
step "Whisplay HAT driver"
RUNTIME=$(find_whisplay_runtime)
if [ -n "$RUNTIME" ]; then
    ok "runtime at $RUNTIME"
    check_dc_fix "$RUNTIME"
    register_with_daemon
else
    bad "the Whisplay runtime is not installed -- the app would run with no screen or button"
    info "PiSugar's driver has an Orange Pi Zero 2W installer, which enables"
    info "the LCD's SPI bus and installs the sound card:"
    info "    git clone --depth 1 https://github.com/PiSugar/Whisplay.git ~/Whisplay"
    info "    cd ~/Whisplay && sudo bash script/install_orangepi_zero2w.sh"
    info "    sudo reboot"
fi

# --------------------------------------------------------------- 6. audio
step "Audio hardware"
check_audio

# ------------------------------------------------------------ 7. provision
step "Provisioning the radio module"
warn "not possible from this board yet"
info "Writing the module's settings means holding M1 high for a few"
info "seconds, and provision_radio.py drives M0/M1 through RPi.GPIO,"
info "which only exists on a Raspberry Pi. The settings live in the module"
info "itself, so provision this LoRa HAT once on a Raspberry Pi running"
info "this project (./install-raspberrypi.sh, step 7), then fit it back here:"
info "    python3 provision_radio.py --frequency ${FREQUENCY:-868}"
info "Every module gets the same settings: the Device ID and pairing are"
info "handled in the app (Home > Pair devices), not in the module."

# ----------------------------------------------------------------- 8. test
step "Self-test"
run_selftest

finish
