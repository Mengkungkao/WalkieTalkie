#!/usr/bin/env bash
#
# One-shot setup for the LoRa Walkie-Talkie: a Whisplay HAT plus a
# Waveshare SX126X LoRa HAT. Works out which board it is on and runs the
# matching installer:
#
#   setup/raspberrypi.sh   Raspberry Pi, Raspberry Pi OS
#   setup/orangepi.sh      Orange Pi Zero 2W, Orange Pi OS or Armbian
#
#   ./setup.sh                    walk through every step, asking before changes
#   ./setup.sh --yes              accept every prompt (unattended)
#   ./setup.sh --check            report only; change nothing
#   ./setup.sh --frequency 915    provision the module for another band
#   ./setup.sh --board orangepi   skip detection (raspberrypi or orangepi)
#
# Steps that need a reboot say so and stop rather than pretending to have
# worked. Run the script again afterwards; it is safe to re-run and skips
# anything already done.
set -uo pipefail

HERE="$(dirname "$(readlink -f "$0")")"

detect_board() {
    local model="" compatible=""
    [ -r /proc/device-tree/model ] && model=$(tr -d '\0' < /proc/device-tree/model)
    [ -r /proc/device-tree/compatible ] && compatible=$(tr '\0' ' ' < /proc/device-tree/compatible)
    case "$model $compatible" in
        *"Raspberry Pi"*|*raspberrypi,*)  echo raspberrypi ;;
        *[Oo]range*[Pp]i*|*xunlong,*)     echo orangepi ;;
        *) [ -f /boot/orangepiEnv.txt ] && echo orangepi ;;
    esac
}

BOARD=""
ARGS=()
while [ $# -gt 0 ]; do
    case "$1" in
        --board)   BOARD="${2:-}"; shift ;;
        -h|--help) awk 'NR > 1 && /^#/ { sub(/^# ?/, ""); print; next } NR > 1 { exit }' "$0"
                   exit 0 ;;
        *)         ARGS+=("$1") ;;
    esac
    shift
done

[ -n "$BOARD" ] || BOARD=$(detect_board)
case "$BOARD" in
    raspberrypi|orangepi) ;;
    "")
        echo "Cannot tell which board this is." >&2
        echo "Pass --board raspberrypi or --board orangepi." >&2
        exit 1 ;;
    *)
        echo "unknown board: $BOARD (expected raspberrypi or orangepi)" >&2
        exit 1 ;;
esac

echo "Board: $BOARD -- running setup/$BOARD.sh"
exec "$HERE/setup/$BOARD.sh" "${ARGS[@]}"
