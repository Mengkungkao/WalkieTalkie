#!/usr/bin/env bash
# Kept for old notes that call it: provision_radio.py now does all of
# this itself -- quits the app, stops whisplay-daemon, takes M0/M1 on
# either board, and puts everything back.
#
#   sudo ./tools/provision.sh --range long
cd "$(dirname "$(dirname "$(readlink -f "$0")")")" || exit 1
exec python3 provision_radio.py "$@"
