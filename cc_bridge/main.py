from __future__ import annotations

import sys

from .config import redact_token
from .core.service import BridgeService

def main() -> int:
    if len(sys.argv) > 1 and sys.argv[1] == "--doctor":
        try:
            service = BridgeService()
        except Exception as exc:
            print(f"CC Bridge doctor\nConfig load failed:\n{redact_token(str(exc))}")
            return 1
        backend = _backend_arg(sys.argv[2:])
        if backend:
            backend = service._normalize_backend(backend)
            service._restore_backend_state(backend)
            service.codex = service._create_backend_client(backend)
        print(service.doctor_text())
        return 0

    if len(sys.argv) > 1 and sys.argv[1] == "--check":
        service = BridgeService()
        backend = _backend_arg(sys.argv[2:])
        if backend:
            backend = service._normalize_backend(backend)
            service._restore_backend_state(backend)
            service.codex = service._create_backend_client(backend)
        print(f"Checking local config and {service._backend_label()} backend...")
        try:
            service.codex.start()
        except Exception as exc:
            service.codex.stop()
            print(f"Check failed:\n{redact_token(str(exc))}")
            return 1
        try:
            projects = service._load_projects()
            print(f"OK: loaded {len(projects)} project(s)")
            for project in projects[:10]:
                print(f"{project.index}. {project.cwd} ({project.count} threads)")
        finally:
            service.codex.stop()
        return 0

    service = BridgeService()
    try:
        service.run()
    except KeyboardInterrupt:
        service.stop_event.set()
        service.codex.stop()
        print("\nInterrupted.")
    return 0


def _backend_arg(args: list[str]) -> str:
    for index, arg in enumerate(args):
        if arg == "--backend" and index + 1 < len(args):
            return args[index + 1]
        if arg.startswith("--backend="):
            return arg.split("=", 1)[1]
    return ""


if __name__ == "__main__":
    raise SystemExit(main())
