# Backend Switching

CC Bridge is a single Telegram manager bot for two local agent backends:

- `codex`: the default backend. It talks to `codex app-server` through JSON-RPC over stdio.
- `claude`: the Claude Code backend. It shells out to the local `claude` CLI and adapts Claude Code sessions to the same bridge-facing operations where possible.

Only one backend is active at a time. The Telegram bot token and allowed chat ids are shared by the manager bot; they are not switched with the backend.

## Telegram Commands

```text
/backend          show the current backend and switching shortcuts
/backend codex    switch to Codex
/backend claude   switch to Claude Code
/codex            shortcut for /backend codex
/claude           shortcut for /backend claude
```

Switching is refused while a turn is active or queued. Use `/interrupt` first if the current backend is still running work.

If the selected backend fails to start, CC Bridge keeps the manager bot alive and reports the backend error in `/status` and `/backend`. This lets the user switch to the other backend from Telegram instead of losing control of the bot.

## HTTP API

```text
GET  /backend
POST /backend {"backend": "codex"}
POST /backend {"backend": "claude"}
```

`GET /status` also reports:

```json
{
  "backend": "codex",
  "backendLabel": "Codex",
  "backendStarted": true,
  "backendError": null
}
```

## State Isolation

`state.json` stores backend-specific state under `backend_states`. These keys are isolated per backend:

```text
selected_cwd
selected_thread_id
selected_model
selected_effort
thread_model_settings
model_picker_targets
telegram_message_bindings
telegram_thread_labels
codex_auth_account
```

This means switching from Codex to Claude and back should restore each backend's own project, thread, model, Telegram reply bindings, and labels.

## Backend Capabilities

Codex backend is the full app-server path. It supports app-server APIs such as `thread/list`, `thread/start`, `turn/start`, `turn/steer`, `model/list`, `thread/goal/*`, diagnostics, apps, plugins, skills, hooks, and Codex auth switching.

Claude backend is a compatibility adapter. It supports thread/session listing, start/resume/read, turns through `claude -p --output-format stream-json`, basic model choices, local goal metadata, archive/unarchive, fork, compact, and history from Claude transcript JSONL files. Some Codex-only APIs intentionally return an error or reduced data.

`/switch` is Codex auth switching only. It is rejected on the Claude backend with a prompt to use `/codex` first.

## Process Ownership

Telegram `getUpdates` allows only one long-polling consumer per bot token. Running old `codex-bridge`, old `claudecode-bridge`, and new `cc-bridge` at the same time causes:

```text
Telegram getUpdates: Conflict: terminated by other getUpdates request
```

On Windows, `run_tray.vbs` stops known legacy Python tray processes before starting `cc_bridge.tray`:

```text
cc_bridge
codex_telegram_bridge
claudecode_telegram_bridge
```

At runtime, repeated Telegram polling conflicts stop CC Bridge polling after three consecutive conflicts instead of logging forever.

## Checks

Use these commands before starting the tray:

```powershell
cd path\to\cc-bridge
python -m cc_bridge --check
python -m cc_bridge --check --backend claude
python -B -m unittest discover -s tests
```

`--check` starts only the selected backend and lists projects. It does not start Telegram polling.
