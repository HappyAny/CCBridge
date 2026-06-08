# Telegram Approval Flow Design

## Goal

Let CC Bridge handle Codex app-server approval requests from Telegram instead of automatically cancelling them.

## Scope

The first implementation covers these app-server requests:

- `item/commandExecution/requestApproval`
- `item/fileChange/requestApproval`
- `item/permissions/requestApproval`

`item/tool/requestUserInput` remains an automatic empty-answer response until the bridge has a separate form-style input flow.

## Interaction

When app-server asks for approval and a Telegram chat is bound, Bridge sends an approval message with inline buttons:

```text
Allow once | Allow in session
Deny
```

The message includes request type, thread id, turn id, cwd, reason, and a compact command/permission summary when available.

Callback mappings:

| Button | Command/file response | Permission response |
| --- | --- | --- |
| Allow once | `{"decision":"accept"}` | `{"permissions": requested, "scope":"turn"}` |
| Allow in session | `{"decision":"acceptForSession"}` | `{"permissions": requested, "scope":"session"}` |
| Deny | `{"decision":"decline"}` | `{"permissions": {}, "scope":"turn"}` |

After a decision, Bridge edits the approval message to show the chosen result and removes the inline keyboard. Duplicate or stale callbacks are answered with `Approval expired`.

## State

Pending approvals are in-memory only:

```text
approval_id -> request_id, method, params, chat_id, message_id, text
```

They are not persisted in `state.json` because app-server request ids are only meaningful for the current running process and active turn.

## Failure Handling

If no Telegram chat is bound or sending the approval prompt fails, Bridge denies safely:

- command/file: `decline`
- permissions: empty permission grant with turn scope

This avoids hanging the app-server turn.

## Tests

Coverage includes:

- command approval `Allow once`
- file approval `Deny`
- permissions approval `Allow in session`
- no-bound-chat fallback denial
