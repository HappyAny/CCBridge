# Telegram History and Summary Design

## Goal

Give the Telegram bridge enough context recovery that resuming an old Codex thread does not feel disorienting.

## Behavior

- `/summary` calls `getConversationSummary` for the selected thread and returns the app-server summary preview and metadata.
- `/history` returns the latest 5 turns.
- `/history N` returns the latest `N` turns, clamped to a safe maximum.
- `/history all` is the reserved full-history path. It pages through `thread/turns/list` with `itemsView: "full"` and currently returns up to the latest 100 turns to avoid flooding Telegram.
- After selecting a thread, the bridge automatically sends a recent-context preview using only the latest turn.

## Formatting

The formatter expands user messages, assistant messages, plans, and reasoning summaries. Tool calls, command executions, and file changes are summarized into compact one-line entries. Long item text is truncated before Telegram chunking.
