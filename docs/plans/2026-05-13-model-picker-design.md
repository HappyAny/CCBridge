# Telegram Model Picker Design

## Goal

Let the user change the Codex model and reasoning effort from Telegram without hard-coding the available model list.

## Behavior

`/model` calls app-server `model/list` with `includeHidden: false` and renders the current result as Telegram inline keyboard buttons. Button labels use `displayName`; callback data uses a short hash of the model id so it stays under Telegram's callback data limit.

After a model is selected, the bridge reloads the model list, saves the selected model plus that model's default reasoning effort, and edits the same Telegram message to show effort buttons from `supportedReasoningEfforts`. Clicking an effort saves `selected_model` and `selected_effort` to `state.json`.

Each future `turn/start` includes the saved `model` and `effort`. Active turns are not changed; use `/interrupt` before sending the next prompt if an immediate switch is needed.

## Safety

Callback queries are subject to the same allowed chat id check as messages. If the model or effort list changes between button rendering and selection, the bridge asks the user to run `/model` again.
