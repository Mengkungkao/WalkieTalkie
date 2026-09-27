#!/usr/bin/env bash
#
# Orange Pi Zero 2W installer for the LoRa Walkie-Talkie (Whisplay HAT
# plus a Waveshare SX126X LoRa HAT) on Orange Pi OS or Armbian. ./setup.sh
# runs this on an Orange Pi; it can also be run directly.
#
#   setup/orangepi.sh               walk through every step, asking first
#   setup/orangepi.sh --yes         accept every prompt (unattended)
#   setup/orangepi.sh --check       report only; change nothing
#
# What differs from the Raspberry Pi: header pins 8/10 are UART0, which
# is also this board's debug console, so the console is moved off it in
# /boot/orangepiEnv.txt; the Whisplay HAT needs PiSugar's Orange Pi
# driver; and the radio cannot be provisioned from this board yet.
set -uo pipefail

STEPS=8
PORT=/dev/ttyS0
# shellcheck source=setup/common.sh
. "$(dirname "$(readlink -f "$0")")/common.sh"

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
install_packages python3-serial python3-yaml python3-pil python3-numpy alsa-utils gpiod

# ---------------------------------------------------------------- 3. UART
step "Serial port for the LoRa HAT"
info "Header pins 8/10 are UART0 ($PORT), which is also this board's"
info "debug console."
if [ ! -f "$BOOT_ENV" ]; then
    warn "neither /boot/orangepiEnv.txt nor /boot/armbianEnv.txt exists"
    bad "move the kernel console off ttyS0 by hand"
    CONSOLE=""
else
    CONSOLE=$(sed -n 's/^console=//p' "$BOOT_ENV" | tail -1)
    if [ "$CONSOLE" = display ]; then
        ok "console=display in $BOOT_ENV"
    else
        warn "console=${CONSOLE:-both (the default)} in $BOOT_ENV puts the kernel console on ttyS0"
        info "Orange Pi OS also logs a shell in there automatically: whatever"
        info "the radio receives is typed into it, and its output is transmitted."
        if ask "move the console to the display only?"; then
            sudo cp "$BOOT_ENV" "${BOOT_ENV}.walkie.bak"
            if grep -q '^console=' "$BOOT_ENV"; then
                sudo sed -i 's/^console=.*/console=display/' "$BOOT_ENV"
            else
                echo "console=display" | sudo tee -a "$BOOT_ENV" >/dev/null
            fi
            ok "set (backup at ${BOOT_ENV}.walkie.bak) -- reboot required"
            NEED_REBOOT=1
        else
            bad "the radio will be unreliable while a console shares the port"
        fi
    fi
fi

if grep -q 'console=ttyS0' /proc/cmdline && [ "$CONSOLE" = display ]; then
    warn "the running kernel still has its console on ttyS0 -- $BOOT_ENV"
    warn "was edited after the last boot. A reboot is required."
    NEED_REBOOT=1
fi
info "U-Boot still prints to this port for a moment at power-on, before"
info "Linux starts; that is not something orangepiEnv.txt can turn off."

disable_serial_getty ttyS0
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
# The same places app/board.py looks.
RUNTIME=""
for dir in "${WHISPLAY_RUNTIME:-}" "$HOME/Whisplay/runtime" "$HERE/../Whisplay/runtime" \
           /opt/whisplay/runtime /usr/local/share/whisplay/runtime; do
    if [ -n "$dir" ] && [ -f "$dir/whisplay_client.py" ]; then
        RUNTIME=$dir
        break
    fi
done
if [ -n "$RUNTIME" ]; then
    ok "runtime at $RUNTIME"
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
info "this project (setup/raspberrypi.sh, step 7), then fit it back here:"
info "    python3 provision_radio.py --frequency ${FREQUENCY:-868}"
info "Every module gets the same settings: the Device ID and pairing are"
info "handled in the app (Settings > Pair device), not in the module."

# ----------------------------------------------------------------- 8. test
step "Self-test"
run_selftest

finish
