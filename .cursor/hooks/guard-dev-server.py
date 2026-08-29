#!/usr/bin/env python3
"""beforeShellExecution: keep bare next/uvicorn off compose ports."""

from __future__ import annotations

import json
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from scripts.portinfo import compose_guard_verdict  # noqa: E402


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except ValueError:
        payload = {}
    command = ""
    if isinstance(payload, dict):
        command = str(payload.get("command") or "")
    verdict = compose_guard_verdict(command)
    json.dump(verdict, sys.stdout)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
