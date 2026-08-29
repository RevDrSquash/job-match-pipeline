"""Agent-facing launcher and port doctor.

Run as ``python -m scripts.dev <cmd>``. Must stay import-safe on Python 3.9
because Cursor ``stop`` hooks import the kill/record helpers under system
``python3``.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence

from scripts.portinfo import (
    AGENT_API_RANGE,
    AGENT_WEB_RANGE,
    COMPOSE_PORTS,
    NoFreePortError,
    PortReport,
    current_backend,
    find_free_port,
    inspect_port,
    is_port_bindable,
    kill_tree,
    listeners,
    python3_status,
    repo_root,
)

RECORDS_DIRNAME = ".dev-servers"
WEB_READY_TIMEOUT = 45.0
API_READY_TIMEOUT = 20.0


def records_dir(root: Optional[Path] = None) -> Path:
    return (root or repo_root()) / RECORDS_DIRNAME


def record_path(kind: str, port: int, root: Optional[Path] = None) -> Path:
    return records_dir(root) / ("%s-%d.json" % (kind, port))


def load_records(root: Optional[Path] = None) -> List[Dict[str, Any]]:
    folder = records_dir(root)
    if not folder.is_dir():
        return []
    rows: List[Dict[str, Any]] = []
    for path in sorted(folder.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(data, dict):
            data["_path"] = str(path)
            rows.append(data)
    return rows


def write_record(record: Dict[str, Any], root: Optional[Path] = None) -> Path:
    folder = records_dir(root)
    folder.mkdir(parents=True, exist_ok=True)
    path = record_path(str(record["kind"]), int(record["port"]), root)
    payload = dict(record)
    payload.pop("_path", None)
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return path


def stop_recorded_servers(
    include_keep: bool = False,
    *,
    root: Optional[Path] = None,
    kill: Optional[Callable[..., None]] = None,
) -> List[str]:
    """Stop recorded agent servers. Returns human-readable actions taken."""
    killer = kill or kill_tree
    messages: List[str] = []
    for record in load_records(root):
        keep = bool(record.get("keep"))
        if keep and not include_keep:
            continue
        pid = int(record.get("pid") or 0)
        port = int(record.get("port") or 0)
        pgid = record.get("pgid")
        pgid_int = int(pgid) if pgid not in (None, "") else None
        kind = str(record.get("kind") or "server")
        if pid:
            killer(pid, pgid=pgid_int)
            messages.append(
                "stopped %s on port %d (pid %d)" % (kind, port, pid)
            )
        path = Path(str(record.get("_path") or record_path(kind, port, root)))
        try:
            path.unlink()
        except OSError:
            pass
    return messages


def compose_occupant_warnings(
    *,
    root: Optional[Path] = None,
    inspect: Optional[Callable[[int], PortReport]] = None,
) -> List[str]:
    """Warn about foreign (non-compose) listeners on compose ports. Never kill them."""
    warnings: List[str] = []
    inspector = inspect or (lambda port: inspect_port(port, root=root))
    for port, service in COMPOSE_PORTS.items():
        report = inspector(port)
        if report.state != "foreign":
            continue
        detail = _report_owner_line(report)
        warnings.append(
            "compose port %d (%s) is held by a foreign process: %s. "
            "Not killing it. Diagnose with `python -m scripts.dev ports`."
            % (port, service, detail)
        )
    return warnings


def _report_owner_line(report: PortReport) -> str:
    if report.docker_names:
        return "docker %s" % ", ".join(report.docker_names)
    if report.processes:
        proc = report.processes[0]
        cmd = proc.command_line or proc.name or ("pid %d" % proc.pid)
        extra = " ; ".join(report.kill_commands)
        return "%s (%s)" % (cmd, extra) if extra else cmd
    if report.pids:
        return "pid %s" % ", ".join(str(p) for p in report.pids)
    if report.state == "unknown":
        return "busy, owner unknown"
    return report.state


def format_port_report(report: PortReport) -> str:
    bits = ["%d  %s  %s" % (report.port, report.role, report.state)]
    if report.docker_names:
        bits.append("docker=%s" % ",".join(report.docker_names))
    if report.processes:
        proc = report.processes[0]
        label = proc.command_line or proc.name
        if len(label) > 80:
            label = label[:77] + "..."
        bits.append("pid=%d %s" % (proc.pid, label))
        if proc.in_repo:
            bits.append("(in-repo)")
    elif report.pids:
        bits.append("pid=%s" % ",".join(str(p) for p in report.pids))
    if report.kill_commands and report.state == "foreign":
        bits.append("kill: %s" % report.kill_commands[0])
    return "  ".join(bits)


def cmd_ports(_args: argparse.Namespace) -> int:
    ok, path = python3_status()
    if ok:
        print("python3 on PATH: yes (%s)" % path)
    else:
        print(
            "python3 on PATH: NO — Cursor hooks will not run. On Windows copy "
            "python.exe to python3.exe next to it (see README)."
        )
    print()
    print("Compose ports")
    for port in COMPOSE_PORTS:
        print("  " + format_port_report(inspect_port(port)))
    print()
    print("Agent web %d-%d" % (AGENT_WEB_RANGE.start, AGENT_WEB_RANGE.stop - 1))
    _print_range(AGENT_WEB_RANGE)
    print()
    print("Agent API %d-%d" % (AGENT_API_RANGE.start, AGENT_API_RANGE.stop - 1))
    _print_range(AGENT_API_RANGE)
    return 0


def _print_range(port_range: range) -> None:
    occupied = 0
    for port in port_range:
        report = inspect_port(port)
        if report.state == "free":
            continue
        occupied += 1
        print("  " + format_port_report(report))
    if occupied == 0:
        print("  all free")


def diagnose_compose_ports() -> List[PortReport]:
    blocking: List[PortReport] = []
    for port in COMPOSE_PORTS:
        report = inspect_port(port)
        if report.state in ("foreign", "unknown"):
            blocking.append(report)
    return blocking


def cmd_up(args: argparse.Namespace) -> int:
    blocking = diagnose_compose_ports()
    if blocking:
        print("Refusing to start compose; host ports are occupied:", file=sys.stderr)
        for report in blocking:
            print("  " + format_port_report(report), file=sys.stderr)
        print(
            "Run `python -m scripts.dev ports` for the full map, then stop the "
            "foreign process (or `python -m scripts.dev stop` for agent servers).",
            file=sys.stderr,
        )
        return 1
    cmd = ["docker", "compose", "up"]
    if args.build:
        cmd.append("--build")
    extra = list(args.compose_args or [])
    cmd.extend(extra)
    print("Running: %s" % " ".join(cmd))
    try:
        return subprocess_call(cmd)
    except FileNotFoundError:
        print("docker not found on PATH", file=sys.stderr)
        return 1


def subprocess_call(cmd: Sequence[str]) -> int:
    import subprocess

    return subprocess.call(list(cmd))


def cmd_web(args: argparse.Namespace) -> int:
    return _launch_web(keep=bool(args.keep))


def cmd_api(args: argparse.Namespace) -> int:
    return _launch_api(keep=bool(args.keep))


def _frontend_dev_cmd(port: int, root: Path) -> List[str]:
    next_name = "next.cmd" if current_backend() == "windows" else "next"
    next_bin = root / "frontend" / "node_modules" / ".bin" / next_name
    if next_bin.exists():
        return [str(next_bin), "dev", "-p", str(port)]
    npm = "npm.cmd" if current_backend() == "windows" else "npm"
    return [npm, "exec", "--yes", "--", "next", "dev", "-p", str(port)]


def _launch_web(keep: bool, root: Optional[Path] = None) -> int:
    root = root or repo_root()
    try:
        port = find_free_port(AGENT_WEB_RANGE)
    except NoFreePortError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    env = os.environ.copy()
    env.setdefault("API_BASE_URL", "http://localhost:8080")
    cmd = _frontend_dev_cmd(port, root)
    cwd = str(root / "frontend")
    return _spawn_and_record(
        kind="web",
        port=port,
        cmd=cmd,
        cwd=cwd,
        env=env,
        keep=keep,
        ready_timeout=WEB_READY_TIMEOUT,
        url="http://localhost:%d" % port,
        extra_notes="API_BASE_URL=%s" % env["API_BASE_URL"],
        root=root,
    )


def _launch_api(keep: bool, root: Optional[Path] = None) -> int:
    root = root or repo_root()
    try:
        port = find_free_port(AGENT_API_RANGE)
    except NoFreePortError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    cmd = [
        sys.executable,
        "-m",
        "uvicorn",
        "app.main:app",
        "--host",
        "127.0.0.1",
        "--port",
        str(port),
    ]
    return _spawn_and_record(
        kind="api",
        port=port,
        cmd=cmd,
        cwd=str(root),
        env=os.environ.copy(),
        keep=keep,
        ready_timeout=API_READY_TIMEOUT,
        url="http://127.0.0.1:%d" % port,
        extra_notes="",
        root=root,
    )


def _spawn_and_record(
    *,
    kind: str,
    port: int,
    cmd: Sequence[str],
    cwd: str,
    env: Dict[str, str],
    keep: bool,
    ready_timeout: float,
    url: str,
    extra_notes: str,
    root: Path,
) -> int:
    import subprocess

    folder = records_dir(root)
    folder.mkdir(parents=True, exist_ok=True)
    log_path = folder / ("%s-%d.log" % (kind, port))
    popen_kwargs = {
        "args": list(cmd),
        "cwd": cwd,
        "env": env,
    }
    if current_backend() == "windows":
        popen_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        popen_kwargs["start_new_session"] = True
    try:
        with open(log_path, "wb") as log_file:
            popen_kwargs["stdout"] = log_file
            popen_kwargs["stderr"] = subprocess.STDOUT
            proc = subprocess.Popen(**popen_kwargs)
    except FileNotFoundError as exc:
        print("failed to spawn %s: %s" % (cmd[0], exc), file=sys.stderr)
        return 1
    pgid = proc.pid if current_backend() != "windows" else None
    try:
        _wait_until_listening(port, proc, ready_timeout)
    except RuntimeError as exc:
        kill_tree(proc.pid, pgid=pgid)
        err = ""
        try:
            err = log_path.read_text(encoding="utf-8", errors="replace")[-500:]
        except OSError:
            err = ""
        print("failed to start %s: %s" % (kind, exc), file=sys.stderr)
        if err.strip():
            print(err.strip(), file=sys.stderr)
        return 1
    record = {
        "kind": kind,
        "port": port,
        "pid": proc.pid,
        "pgid": pgid,
        "keep": keep,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "command": list(cmd),
        "url": url,
    }
    path = write_record(record, root)
    print("Agent %s server: %s" % (kind, url))
    if extra_notes:
        print(extra_notes)
    print("Record: %s" % path)
    print("Stop with: python -m scripts.dev stop")
    return 0


def _wait_until_listening(port: int, proc: Any, timeout: float) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        code = proc.poll()
        if code is not None:
            raise RuntimeError("process exited with %s before binding %d" % (code, port))
        if listeners(port) or not is_port_bindable(port):
            return
        time.sleep(0.2)
    raise RuntimeError("timed out waiting for port %d to listen" % port)


def cmd_status(_args: argparse.Namespace) -> int:
    rows = load_records()
    if not rows:
        print("No recorded agent servers (.dev-servers/ is empty).")
        return 0
    for record in rows:
        port = int(record.get("port") or 0)
        pid = int(record.get("pid") or 0)
        alive = _pid_alive(pid)
        listening = bool(listeners(port)) if port else False
        keep = "keep" if record.get("keep") else "ephemeral"
        state = "listening" if listening else ("alive" if alive else "dead")
        print(
            "%s  port %d  pid %d  %s  %s  %s"
            % (
                record.get("kind"),
                port,
                pid,
                state,
                keep,
                record.get("url") or "",
            )
        )
    return 0


def _pid_alive(pid: int) -> bool:
    if not pid:
        return False
    if current_backend() == "windows":
        import ctypes

        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if handle:
            kernel32.CloseHandle(handle)
            return True
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def cmd_stop(args: argparse.Namespace) -> int:
    messages = stop_recorded_servers(include_keep=bool(args.all))
    if not messages:
        print("No recorded agent servers to stop.")
        return 0
    for line in messages:
        print(line)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m scripts.dev",
        description=(
            "Launch agent-only Next/API servers on reserved ports, inspect who "
            "owns compose ports, and preflight docker compose."
        ),
    )
    sub = parser.add_subparsers(dest="command", required=True)

    web = sub.add_parser("web", help="next dev on the first free agent web port (3200-3209)")
    web.add_argument(
        "--keep",
        action="store_true",
        help="do not auto-stop this server when the agent turn ends",
    )
    web.set_defaults(func=cmd_web)

    api = sub.add_parser("api", help="uvicorn on the first free agent API port (8180-8189)")
    api.add_argument(
        "--keep",
        action="store_true",
        help="do not auto-stop this server when the agent turn ends",
    )
    api.set_defaults(func=cmd_api)

    ports = sub.add_parser("ports", help="show who owns compose and agent ports")
    ports.set_defaults(func=cmd_ports)

    up = sub.add_parser(
        "up",
        help="preflight compose ports, then run docker compose up",
    )
    up.add_argument("--build", action="store_true", help="pass --build to docker compose")
    up.add_argument(
        "compose_args",
        nargs="*",
        help="extra arguments forwarded to docker compose up",
    )
    up.set_defaults(func=cmd_up)

    status = sub.add_parser("status", help="list recorded agent servers")
    status.set_defaults(func=cmd_status)

    stop = sub.add_parser("stop", help="stop recorded agent servers")
    stop.add_argument(
        "--all",
        action="store_true",
        help="also stop servers launched with --keep",
    )
    stop.set_defaults(func=cmd_stop)
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
