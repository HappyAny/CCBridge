# Turn Steer and Interrupt Design

## Goal

Let Telegram input affect an active Codex turn when possible, while preserving an explicit way to queue follow-up work.

## Behavior

When the bridge is ready and a Codex turn is active, normal Telegram messages call `turn/steer` with the current `threadId`, `expectedTurnId`, and user input. If there is no active turn, the selected thread changed, or steering fails, the input falls back to the local queue.

`/queue message` bypasses steering and always adds `message` to the local queue. Image and file captions may also start with `/queue`; in that case the uploaded item is queued with the caption body as its prompt.

`/interrupt` sends `turn/interrupt` for the active `threadId` and `turnId`, immediately final-edits the Telegram reply with collected output, and drains queued messages that have not started yet. It does not roll back the thread or create a replacement turn. The later `turn/completed` notification may update the first reply message again, but duplicate tail chunks are suppressed after an immediate interrupt final edit.

Slash commands only apply when the raw Telegram message or caption starts with `/`. A slash command appearing later in the text is treated as normal user input.

## Telegram Menu

On startup, the bridge calls `setMyCommands` with every supported command. Failure to register commands is logged as non-fatal so it does not block the service.
