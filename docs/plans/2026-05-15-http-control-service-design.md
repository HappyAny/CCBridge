# HTTP Control Service Design

## Goal

Expose a local HTTP control surface so programs on the same machine can drive the Codex app-server bridge without going through Telegram.

## Shape

- The HTTP service runs inside the bridge process and shares the existing app-server connection, selected project, selected thread, model, and effort.
- It listens on `127.0.0.1:8765` by default.
- If configured to bind to a non-loopback host, `HTTP_CONTROL_TOKEN` is required.
- The HTTP path never sends Telegram messages, placeholders, or edits.

## API

- `GET /health`
- `GET /status`
- `GET /projects`
- `GET /threads?cwd=...`
- `POST /project`
- `POST /thread`
- `POST /new`
- `GET /threadinfo`
- `POST /rename`
- `POST /archive`
- `POST /unarchive`
- `POST /rollback`
- `POST /compact`
- `GET /goal`
- `POST /goal`
- `POST /goal/clear`
- `GET /summary`
- `POST /message`
- `POST /queue`
- `POST /interrupt`
- `GET /models`
- `POST /model`
- `GET /limits`
- `GET /mcp`
- `POST /review`
- `GET /diff`
- `GET /config`
- `GET /skills`
- `GET /hooks`
- `POST /fork`
- `GET /apps`
- `GET /plugins`
- `POST /stop`
- `GET /history`

`POST /message` runs a synchronous Codex turn and returns JSON with the final text. If a turn is already active, the request can steer the active turn when `steer` is true.
`POST /queue` never steers. It waits for the turn lock and then runs a new HTTP turn.

## Concurrency

Telegram queued turns and HTTP turns share a process-level turn lock so two new turns cannot consume app-server events at the same time. Steering an active turn remains allowed.

## App-Server Coverage

The bridge exposes the app-server calls needed for thread administration and bridge operation:

- `thread/read`, `thread/name/set`, `thread/archive`, `thread/unarchive`, `thread/rollback`, `thread/compact/start`
- `thread/goal/get`, `thread/goal/set`, `thread/goal/clear`
- `account/rateLimits/read`
- `mcpServerStatus/list`
- `review/start`
- `gitDiffToRemote`
- `config/read`, `configRequirements/read`, `modelProvider/capabilities/read`
- `skills/list`, `hooks/list`
- `thread/fork`
- `app/list`
- `plugin/list`, `plugin/read`

Rollback is intentionally documented as thread-history-only. It does not revert files that Codex already changed in the working directory.
