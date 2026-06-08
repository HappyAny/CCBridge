# CC Bridge

[简体中文](README.zh-CN.md)

CC Bridge is a local Telegram control plane for Codex and Claude Code.

It runs on your machine, listens to one Telegram bot, and forwards chat messages to either a Codex `app-server` thread or a Claude Code session. You can switch backends, pick projects and threads, stream replies, approve tool use, interrupt stuck work, send files, fetch generated images, and expose the same controls through a local HTTP API.

The default backend is Codex. Switch from Telegram with `/backend`, `/codex`, or `/claude`.

## Features

- One Telegram bot can control Codex and Claude Code.
- Codex support uses `codex app-server` over JSON-RPC stdio.
- Claude support adapts local `claude` CLI sessions to the same bridge-facing flow where possible.
- Project, thread, model, fast tier, history, fork, archive, rollback, compact, summary, and goal commands are available from Telegram.
- Text, image, and file uploads are accepted. Generated images and files from the latest turn can be listed and sent back with `/images` and `/files`.
- Codex approval prompts are delivered to Telegram with inline buttons: allow once, allow in session, or deny.
- `/interrupt` denies pending approvals for the target thread, interrupts the active backend turn, and clears queued messages that have not started.
- A local HTTP control server mirrors the main Telegram operations for scripts and desktop tools.
- Tray launchers are included for Windows, macOS, and Linux.
- Runtime state, logs, downloads, local config, locks, and auth files are ignored by git.

## Requirements

- Python 3.10 or newer.
- A Telegram bot token from BotFather.
- At least one allowed Telegram chat id.
- Codex CLI with `codex app-server` if you use the Codex backend.
- Claude Code CLI if you use the Claude backend.

Python packages are listed in [`requirements.txt`](requirements.txt).

## Quick Start

```powershell
cd path\to\cc-bridge
python -m pip install -r requirements.txt
copy config.local.example.py config.local.py
```

Edit `config.local.py`:

```python
BOT_TOKEN = "telegram-bot-token"

ALLOWED_TELEGRAM_CHAT_IDS = [
    123456789,
]
```

Run the bridge:

```powershell
python -m cc_bridge
```

Open Telegram and send `/start` or `/project` to the bot.

## Checks

Check local config and backend startup without starting Telegram polling:

```powershell
python -m cc_bridge --check
python -m cc_bridge --check --backend claude
```

Run diagnostics without starting Telegram polling:

```powershell
python -m cc_bridge --doctor
python -m cc_bridge --doctor --backend claude
```

Run tests:

```powershell
python -B -m unittest discover -s tests
```

## Tray Mode

Use the launcher for your platform:

- Windows: double-click `CC Bridge.bat`.
- macOS: run `start_tray.command`.
- Linux: run `start_tray.sh`.

The Windows launcher calls `run_tray.vbs`, which starts `pythonw.exe -m cc_bridge.tray` without leaving a terminal window open. On startup it stops older `cc_bridge`, `codex_telegram_bridge`, and `claudecode_telegram_bridge` Python tray processes so the Telegram bot token has only one `getUpdates` consumer.

CC Bridge also takes a single-instance lock at `cc_bridge.lock`. If the lock file remains after a crash, verify that no CC Bridge process is running before deleting it.

Tray logs go to `bridge.log`. On startup, `bridge.log` rotates at 512 KB and keeps three backups. `approval_audit.log` uses the same rotation before audit writes.

## Configuration

`config.local.py` is required and ignored by git. Start from [`config.local.example.py`](config.local.example.py).

```python
BOT_TOKEN = "telegram-bot-token"

# Optional. Leave empty when ALLOWED_TELEGRAM_CHAT_IDS is set.
TELEGRAM_CHAT_ID = None

# Required. Only these Telegram chats can control the bridge.
ALLOWED_TELEGRAM_CHAT_IDS = [
    123456789,
]

# Optional local HTTP control API.
HTTP_CONTROL_HOST = "127.0.0.1"
HTTP_CONTROL_PORT = 8765
HTTP_CONTROL_TOKEN = ""
```

The bridge ignores every Telegram update whose chat id is not allowed. The HTTP server binds to localhost by default. If you bind it to a non-loopback host, `HTTP_CONTROL_TOKEN` is required.

## Backend Switching

Only one backend is active at a time. Telegram bot credentials and allowed chat ids are shared by the manager bot; they do not change when you switch backends.

```text
/backend          show the active backend and switching shortcuts
/backend codex    switch to Codex
/backend claude   switch to Claude Code
/codex            shortcut for /backend codex
/claude           shortcut for /backend claude
```

Switching is refused while a turn is active or queued. Use `/interrupt` first.

If a backend fails to start, CC Bridge keeps the manager bot online and reports the error in `/status` and `/backend`, so you can retry or switch from Telegram.

Codex and Claude keep separate selected project, thread, model, reply binding, and label state in `state.json`. See [`docs/backend-switching.md`](docs/backend-switching.md) for the details.

## Telegram Commands

Common commands:

| Command | Purpose |
| --- | --- |
| `/start` | Bind the chat and show project selection. |
| `/backend` | Show or switch the Codex/Claude backend. |
| `/project` | Choose a project. Shows the latest 5 by default; repeat or use `/project all` for all. |
| `/thread` | Choose a thread in the current project. Shows the latest 5 by default; repeat or use `/thread all` for all. |
| `/new` | Create a new thread in the current project. |
| `/status` | Show backend, project, thread, active turn, queue, approvals, and goal state. |
| `/queue <text>` | Queue a message instead of steering the active turn. |
| `/interrupt` | Interrupt the active backend turn and deny pending approvals for that thread. |
| `/stop` | Stop the bridge service. |
| `/help` | Show command help. |

Thread and history commands:

| Command | Purpose |
| --- | --- |
| `/threadinfo` | Show current thread details. Use `/threadinfo full` to include turns. |
| `/rename <name>` | Rename the current thread. |
| `/archive` | Archive the selected thread. In project view, `/archive 1` archives all unarchived threads in project 1. In thread view, `/archive 1 2` archives those threads. |
| `/unarchive <threadId>` | Restore an archived thread and select it when possible. |
| `/rollback 1` | Roll back Codex thread history. It does not revert files in the working tree. |
| `/compact` | Start thread compaction. |
| `/fork [name]` | Fork the current thread, optionally rename the fork, and select it. |
| `/summary` | Read the app-server conversation summary. |
| `/history` | Show the last 5 turns. Use `/history 10` or `/history all`. |

Codex and agent commands:

| Command | Purpose |
| --- | --- |
| `/goal` | Read the current goal. |
| `/goal <objective>` | Set an active thread goal. |
| `/goal clear` | Clear the current goal. |
| `/goal end` | Pause the goal, interrupt the active turn, then clear the goal. |
| `/goal status paused` | Change goal status. Supported statuses are `active`, `paused`, `blocked`, `usageLimited`, `budgetLimited`, and `complete`. |
| `/goal budget 100000 [objective]` | Set a token budget, optionally with a new objective. |
| `/approvals` | Show pending app-server approval prompts. |
| `/approvals deny 1 2` | Deny selected approvals. |
| `/approvals deny all` | Deny all pending approvals. |
| `/limits` | Show account rate limits. |
| `/mcp` | Show MCP server status. Use `/mcp full` for full detail. |
| `/review` | Start an inline review for uncommitted changes. |
| `/diff` | Show git diff to remote. |
| `/config` | Show backend config. Use `/config full` to include config layers. |
| `/skills` | Show available skills. Use `/skills reload` to bypass cache. |
| `/hooks` | Show configured hooks. |
| `/apps` | Show apps. Use `/apps refresh` to bypass app caches. |
| `/plugins` | Show plugins. Use `/plugins <name>` to read one plugin. |

Media, model, and auth commands:

| Command | Purpose |
| --- | --- |
| `/images` | List images from the latest turn. Use `/images 1` or `/images all` to send them to Telegram. |
| `/files` | List files from the latest turn. Use `/files 1` or `/files all` to send them to Telegram. |
| `/model` | Choose model and reasoning effort with Telegram buttons. |
| `/fast` | Show or set fast service tier. Use `/fast on`, `/fast off`, or `/fast status`. |
| `/switch` | List or switch prepared Codex `auth.json` accounts. Codex backend only. |
| `/doctor` | Run bridge diagnostics. |

Slash commands only apply at the beginning of a Telegram message or caption. Text such as `please /interrupt this` is sent to the active backend as normal input.

## Messages, Files, and Approvals

Normal Telegram messages steer the active turn when the backend supports same-turn steering. Use `/queue <message>` when you want the text to run as a new turn after the active one finishes.

Telegram images are sent to Codex as `localImage`. For Claude Code, image paths are included in the prompt. Other uploaded files are downloaded under `downloads/` and passed as local file mentions plus a path in the prompt.

When Codex app-server requests approval for a command, file change, or permission grant, CC Bridge sends an inline Telegram prompt:

```text
Allow once | Allow in session
Deny
```

Pending approvals expire after 10 minutes and are denied safely. If no Telegram chat is bound, approval requests are denied safely instead of hanging the turn.

Approval prompts redact common token, bearer, password, secret, and API key patterns before sending to Telegram. Approval decisions are logged as metadata-only JSON lines in `approval_audit.log`; command bodies and permission payloads are not written to that log.

## HTTP Control API

CC Bridge starts a local HTTP control server for scripts and desktop integrations. By default it listens on `http://127.0.0.1:8765` and does not send proactive Telegram messages.

Useful endpoints:

```text
GET  /health
GET  /help
GET  /commands
GET  /status
GET  /doctor
GET  /backend
POST /backend      {"backend": "codex"} or {"backend": "claude"}
GET  /projects
GET  /threads?cwd=C:\path\to\some-project
POST /project      {"index": 1} or {"cwd": "..."}
POST /thread       {"index": 1} or {"threadId": "...", "cwd": "..."}
POST /new          {"cwd": "..."} or {}
GET  /threadinfo?includeTurns=0
POST /rename       {"name": "new title"}
POST /archive      {}
POST /unarchive    {"threadId": "...", "select": true}
POST /rollback     {"numTurns": 1}
POST /compact      {}
GET  /goal
POST /goal         {"objective": "...", "status": "active", "tokenBudget": 100000}
POST /goal/clear   {}
GET  /summary
GET  /models
POST /model        {"model": "gpt-5.4", "effort": "medium"}
GET  /fast
POST /fast         {"mode": "on"} or {"mode": "off"}
POST /message      {"text": "hello", "timeoutSeconds": 300}
POST /queue        {"text": "run after the active turn", "timeoutSeconds": 300}
POST /interrupt    {}
GET  /limits
GET  /mcp?detail=toolsAndAuthOnly&limit=50
POST /review       {"target": {"type": "uncommittedChanges"}, "delivery": "inline"}
GET  /diff
GET  /config?includeLayers=1
GET  /skills?forceReload=0
GET  /hooks
POST /fork         {"name": "experiment", "select": true}
GET  /apps?forceRefetch=0&limit=50
GET  /plugins
GET  /plugins?pluginName=github
GET  /auth/accounts
POST /auth/switch  {"account": "user@example.com"} or {"index": 1}
POST /stop         {}
GET  /history?limit=5
GET  /history?all=1
```

`POST /message` runs a synchronous HTTP turn and returns the final text. If a turn is already active, it tries `turn/steer` unless you pass `"steer": false`. `POST /queue` waits for the current turn lock and then starts a new HTTP turn.

If `HTTP_CONTROL_TOKEN` is set, pass it as `Authorization: Bearer <token>` or `X-Codex-Bridge-Token: <token>`.

`/backend` and `POST /backend` switch only the local bridge backend. They do not change the Telegram bot token, allowed chat ids, or Codex auth account.

## Codex Auth Switching

`/switch` scans prepared Codex auth backups under:

```text
D:\Backups\codex-auth\<account>\auth.json
```

Use `/switch` to list accounts, then send a number, or use `/switch <account>` directly.

Switching is refused unless both the current Codex account and the requested target account are present in that backup list. If the requested account is already active, CC Bridge reports that it is unchanged and does not restart app-server.

When switching is allowed, CC Bridge stops app-server, backs up the current global `%USERPROFILE%\.codex\auth.json` into the current account backup entry, copies the selected auth file into place, and restarts app-server. If Codex is replying, switch is refused; use `/interrupt` first.

## Project Layout

```text
cc_bridge/
  __main__.py             CLI entry: python -m cc_bridge
  main.py                 CLI args and startup checks
  config.py               config.local.py loading and constants
  state.py                state.json load/save
  request_parsing.py      HTTP and command payload parsing
  tray.py                 tray icon app

  appserver/              Codex JSON-RPC and Claude CLI adapters
  assets/                 tray icons
  core/                   bridge service, turns, threads, models, diagnostics
  formatting/             Telegram and HTTP text formatters
  http/                   local HTTP control server
  platform/               Windows, macOS, and Linux process helpers
  telegram/               Bot API client, handlers, commands, Markdown

docs/                     design notes and compatibility docs
scripts/                  helper scripts
tests/                    unittest coverage
```

## Documentation

- [`docs/backend-switching.md`](docs/backend-switching.md) explains backend switching and state isolation.
- [`docs/telegram-routing.md`](docs/telegram-routing.md) explains Telegram routing, startup context, reply routing, multi-thread execution, and thread-level model configuration.
- [`docs/app-server-api.md`](docs/app-server-api.md) records Codex app-server JSON-RPC compatibility notes.

## Security Notes

- Keep `config.local.py` out of git. It contains the Telegram bot token and allowed chat ids.
- Keep `state.json`, `downloads/`, `bridge.log`, `approval_audit.log`, `cc_bridge.lock`, and auth files out of git.
- Keep the HTTP server on loopback unless you set `HTTP_CONTROL_TOKEN`.
- Approval audit logs contain metadata only. They do not store command bodies or permission payloads.

## License

MIT. See [`LICENSE`](LICENSE).

## Development

Before opening a pull request or publishing the repo, run:

```powershell
python -B -m unittest discover -s tests
python -m cc_bridge --doctor
```

Use `python -m cc_bridge --check` when you only need to verify local config and backend startup.
