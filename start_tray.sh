#!/bin/bash
# CC Bridge — Linux Tray Launcher
# chmod +x start_tray.sh && ./start_tray.sh
cd "$(dirname "$(readlink -f "$0")")" || exit 1
echo "Starting CC Bridge (tray mode)..."
python3 -m cc_bridge.tray
