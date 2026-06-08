#!/bin/bash
# CC Bridge — macOS Tray Launcher
# Double-click in Finder to start. Runs in background via Terminal.
cd "$(dirname "$0")" || exit 1
echo "Starting CC Bridge (tray mode)..."
python3 -m cc_bridge.tray
