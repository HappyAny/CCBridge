# Codex Auth Switch Design

## Goal

Add a fast account switch path for CC Bridge by copying prepared `auth.json` files from `D:\Backups\codex-auth\<account>\auth.json` into the global Codex auth location.

## User Flow

- `/switch` lists available accounts from the backup directory and keeps a short pending selection list.
- After `/switch`, a bare number such as `1` selects that account.
- `/switch user@example.com` or `/switch 1` switches directly.
- HTTP callers can use `GET /auth/accounts` and `POST /auth/switch`.

## Switch Sequence

1. Refuse the switch if any Codex turn is active.
2. Stop the current `codex app-server` process.
3. Read the current global auth file from `%USERPROFILE%\.codex\auth.json`.
4. Detect the current account from the auth JSON, matching backup file hash, or saved bridge state.
5. Back up the current auth to `D:\Backups\codex-auth\<current-account>\auth.json`.
6. Copy the selected backup auth into the global auth path.
7. Clear stale app-server turn/event routing state.
8. Restart `codex app-server`.
9. Save the selected account name in `state.json`.

## Safety Rules

- Never print or return auth file contents.
- Only switch to accounts discovered under the configured backup root.
- Do not accept arbitrary file paths from Telegram or HTTP.
- If restarting with the target auth fails, restore the previous global auth and try to restart with the previous account.
- Unknown current accounts are backed up into timestamped `unknown-...` directories rather than overwriting an existing account directory.

## Scope

This feature switches Codex authentication only. It does not switch Telegram bot configuration, bridge `state.json`, MCP config, skills, hooks, or project/thread selections.
