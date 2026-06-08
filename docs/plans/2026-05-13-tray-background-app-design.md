# CC Bridge Tray Design

## Goal

Run the existing Telegram bridge as a Windows tray background app without changing the current CLI entry point. The tray icon shows whether Codex is idle, actively processing a reply, or in an error state.

## Approach

Add a separate `tray_app.pyw` entry point that owns the tray UI and starts `BridgeService.run(enable_console=False)` in a background thread. The CLI command `python cc_bridge.py` remains unchanged.

The tray layer uses `pystray` for the Windows notification area and Pillow to load generated PNG assets from `assets/`. A code-drawn fallback icon remains available if the assets are missing.

## States

- Idle: `assets/codex-idle.png`.
- Busy: `assets/codex-running.png` while `BridgeService.turn_busy` is set or queued turns exist.
- Error: red icon if startup or runtime fails.

The icon is refreshed on a short timer. Status text includes mode, selected project, selected thread, queue size, busy flag, and the latest error if any.

## Menu

- Status: sends the current status to the bound Telegram chat when available and also refreshes the tray title.
- Open Folder: opens the bridge folder.
- Quit: sets `stop_event`, stops `codex app-server`, and exits the tray process.

## Logging

Because `.pyw` runs without a console, stdout and stderr are redirected to `bridge.log`. This preserves startup errors and non-fatal Telegram/Codex errors.

## Startup

Add `run_tray.bat` so the app can be launched by double-clicking. It prefers `pythonw.exe` when available, falling back to `python.exe`.
