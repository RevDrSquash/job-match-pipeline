#!/usr/bin/env python3
"""stop hook: reap recorded agent servers; warn about compose-port squatters.

Fails open: any exception prints ``{}`` so the agent can still finish.
Never returns ``followup_message`` — compose occupying 3100 is the normal
state and must not start another agent turn. Foreign occupants are reported
via ``user_message`` (and stderr) rather than killed.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from scripts.dev import compose_occupant_warnings, stop_recorded_servers  # noqa: E402


def main() -> int:
    try:
        sys.stdin.read()
        stopped = stop_recorded_servers(include_keep=False)
        warnings = compose_occupant_warnings()
        for line in stopped:
            sys.stderr.write(line + "\n")
        for line in warnings:
            sys.stderr.write(line + "\n")
        out = {}
        if warnings:
            out["user_message"] = "\n".join(warnings)
        json.dump(out, sys.stdout)
        sys.stdout.write("\n")
    except Exception as exc:  # noqa: BLE001 — fail open
        sys.stderr.write("sweep-dev-servers failed: %s\n" % exc)
        sys.stdout.write("{}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
