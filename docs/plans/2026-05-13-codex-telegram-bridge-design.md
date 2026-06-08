# CC Bridge Design

## Goal

Run a small background service that receives Telegram messages and forwards them to Codex through `codex app-server`. Codex replies are sent back to Telegram. The service starts by listing previously used Codex projects, then lets the user choose a project and a thread before continuing work in that thread.

## Shape

The bridge is a standalone Python service under `codex-telegram-bridge/`. It starts `codex app-server` over stdio, initializes the app-server protocol, and uses Telegram long polling. It does not reuse the existing `tg.py` bot or `agents.db`; a new local `config.local.py` stores the bot token directly and is ignored by git.

## Selection Flow

On startup, the service calls `thread/list`, groups sessions by `cwd`, and presents numbered projects in both the terminal and Telegram when a chat is bound. After a project is selected, it lists threads for that project, sorted by recent activity. The selected thread is resumed with `thread/resume`.

The Telegram commands are:

- `/start` binds the current chat and shows the current selection step.
- `/project` starts project selection again.
- `/thread` starts thread selection for the current project.
- `/new` creates a new thread in the selected project.
- `/status` shows the active project and thread.
- `/stop` stops the service.
- `/help` shows command help.

## Codex Runtime

The service uses:

```json
{
  "approvalPolicy": "on-request",
  "approvalsReviewer": "auto_review"
}
```

`approvalPolicy` controls when approval is needed. `approvalsReviewer` routes those approvals to auto review. The bridge does not expose the app-server on a network port.

## Streaming

For each Telegram user message, the bridge creates one Codex turn. It sends one placeholder Telegram message, listens for `item/agentMessage/delta`, and edits that same Telegram message with accumulated output.

Edits are throttled to roughly once every 4 seconds to avoid Telegram rate limits while still feeling live. Typing status is also refreshed roughly every 4 seconds. When the turn completes, the final text is edited immediately even if the throttle interval has not elapsed. Long replies are split into multiple Telegram messages at completion.

## State

`state.json` stores:

- bound Telegram chat id
- Telegram update offset
- selected project cwd
- selected thread id

This prevents duplicate Telegram update handling and lets the service restart into the previous selection.

## Security

`config.local.py` stores `ALLOWED_TELEGRAM_CHAT_IDS`. The bridge ignores every Telegram update whose chat id is not in that allowlist before binding, command handling, downloads, or Codex forwarding. `TELEGRAM_CHAT_ID`, when configured, is also treated as allowed.
