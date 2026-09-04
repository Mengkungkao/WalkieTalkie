#!/usr/bin/env bash
# Copy the app to the Pi and install its Python dependencies.
#
#   ./deploy.sh jarvis@192.168.0.33
#
# rsync excludes the venv and caches, so a redeploy over Wi-Fi to a Zero
# 2 W moves a few tens of kilobytes rather than the whole tree.
set -euo pipefail

TARGET="${1:-}"
REMOTE_DIR="${2:-WalkieTalkie}"
if [ -z "$TARGET" ]; then
    echo "usage: $0 user@host [remote-dir]" >&2
    exit 1
fi

HERE="$(dirname "$(readlink -f "$0")")"
SSH_OPTS=(-o ConnectTimeout=25)   # a Zero 2 W is slow to answer; 10s hangs

echo "==> syncing to ${TARGET}:${REMOTE_DIR}"
# config.yaml is excluded on purpose: it carries this node's radio
# address, which must differ from every other node's. Syncing it would
# quietly give two radios the same address on every deploy, and they
# would then discard each other's traffic as their own echo.
rsync -az --delete \
    --exclude '.git' --exclude '.venv' --exclude '__pycache__' \
    --exclude '*.pyc' --exclude '.pytest_cache' --exclude 'config.yaml' \
    -e "ssh ${SSH_OPTS[*]}" \
    "${HERE}/" "${TARGET}:${REMOTE_DIR}/"

# Seed a config only where there is not one already.
rsync -az --ignore-existing -e "ssh ${SSH_OPTS[*]}" \
    "${HERE}/config.yaml" "${TARGET}:${REMOTE_DIR}/config.yaml"

echo "==> installing dependencies"
ssh "${SSH_OPTS[@]}" "$TARGET" "bash -s" <<REMOTE
set -euo pipefail
cd "${REMOTE_DIR}"
chmod +x run.sh install.sh provision_radio.py 2>/dev/null || true

missing=""
for pkg in serial yaml PIL numpy; do
    python3 -c "import \$pkg" 2>/dev/null || missing="\$missing \$pkg"
done
if [ -n "\$missing" ]; then
    echo "   missing python modules:\$missing"
    echo "   install with: sudo apt install python3-serial python3-yaml python3-pil python3-numpy"
fi

python3 - <<'CHECK'
import ctypes.util
print("   libcodec2:", ctypes.util.find_library("codec2") or "NOT FOUND (sudo apt install libcodec2-1.2)")
CHECK

echo "   audio devices:"
arecord -l 2>/dev/null | grep '^card' || echo "     no capture device"
aplay   -l 2>/dev/null | grep '^card' || echo "     no playback device"
REMOTE

echo
echo "==> deployed. On the Pi:"
echo "    cd ${REMOTE_DIR} && ./install.sh      # register with the whisplay daemon"
echo "    ./run.sh                              # or launch it from the HAT desktop"
