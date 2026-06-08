# Project Refactor Design

## Goal

Move the bridge from a single large script into a package layout while preserving the existing entry point, tray launcher, Telegram behavior, and HTTP API.

## Shape

- Keep `cc_bridge.py` as a compatibility wrapper.
- Put implementation modules under `cc_bridge/`.
- Keep `cc_bridge.py` as the compatibility script for `python cc_bridge.py`.
- Split `BridgeService` into mixins for turns, threads, model selection, diagnostics, router integration, Telegram handlers, and app-server event handling.
- Keep HTTP routing in `http/routes.py` and the server machinery in `http/server.py`.
- Add tests with fake app-server and fake HTTP bridge objects.

## Follow-Up

The next safe split is to replace broad wildcard imports in mixin modules with explicit imports and to shrink `core/threads.py` further if thread administration keeps growing.
