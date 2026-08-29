"""Port map, listener inspection, and compose-vs-foreign classification.

Stdlib only and import-safe on Python 3.9: Cursor hooks run this under the
system ``python3`` (macOS Xcode CLT can still be 3.9). Keep syntax at 3.9 —
no ``match``/``case``, no runtime ``list[int]()`` constructions.

Platform dispatch (netstat vs lsof, CIM vs ps, SO_REUSEADDR, excluded Hyper-V
ranges) sits behind ``backend=`` / ``run=`` / ``output=`` seams so parsers stay
pure functions over captured text.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional, Sequence, Tuple

# Single source of truth for host port ownership. Compose publishes these;
# agents must never bind them with a bare next/uvicorn.
COMPOSE_PORTS: Dict[int, str] = {
    3100: "web",
    8080: "app",
    5433: "db",
}

AGENT_WEB_RANGE = range(3200, 3210)
AGENT_API_RANGE = range(8180, 8190)

NEXT_DEFAULT_PORT = 3000
# frontend/package.json "dev" / "start" scripts pass ``-p 3100``.
NPM_DEV_DEFAULT_PORT = 3100
UVICORN_DEFAULT_PORT = 8000

_PORT_FLAG = re.compile(r"(?:^|\s)(?:-p|--port)(?:\s+|=)(\d+)")
_NEXT_CMD = re.compile(r"\bnext(?:\.js)?\s+(dev|start)\b")
_NPM_DEV_CMD = re.compile(r"\bnpm(?:\.cmd)?\s+(?:run\s+)?(dev|start)\b")
_UVICORN_CMD = re.compile(r"\buvicorn\b")
_SCRIPTS_DEV = re.compile(r"\bscripts\.dev\b")
_PS_LINE = re.compile(
    r"^\s*(\d+)\s+(\d+)\s+(\w{3}\s+\w{3}\s+\d{1,2}\s+\d{2}:\d{2}:\d{2}\s+\d{4})\s+(.*)$"
)
_PUBLISHED_PORT = re.compile(r":(\d+)->")


RunFn = Callable[..., str]


@dataclass
class ProcessInfo:
    pid: int
    name: str
    command_line: str
    start_time: str = ""
    ppid: int = 0
    in_repo: bool = False


@dataclass
class PortReport:
    port: int
    role: str
    state: str
    pids: List[int] = field(default_factory=list)
    processes: List[ProcessInfo] = field(default_factory=list)
    docker_names: List[str] = field(default_factory=list)
    kill_commands: List[str] = field(default_factory=list)


def repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def current_backend(name: Optional[str] = None) -> str:
    if name:
        return name
    if sys.platform == "win32":
        return "windows"
    return "posix"


def python3_status() -> Tuple[bool, str]:
    path = shutil.which("python3") or ""
    return (bool(path), path)


def port_role(port: int) -> str:
    service = COMPOSE_PORTS.get(port)
    if service:
        return "compose-%s" % service
    if port in AGENT_WEB_RANGE:
        return "agent-web"
    if port in AGENT_API_RANGE:
        return "agent-api"
    return "other"


def suggested_kill_command(pid: int, backend: Optional[str] = None) -> str:
    if current_backend(backend) == "windows":
        return "taskkill /PID %d /T /F" % pid
    return "kill %d" % pid


# --- bind probe -------------------------------------------------------------


def parse_excluded_portrange(text: str) -> List[Tuple[int, int]]:
    """Parse ``netsh interface ipv4 show excludedportrange`` tables."""
    ranges: List[Tuple[int, int]] = []
    for raw in text.splitlines():
        parts = raw.replace("*", " ").split()
        if len(parts) < 2 or not parts[0].isdigit() or not parts[1].isdigit():
            continue
        start, end = int(parts[0]), int(parts[1])
        if start > 0 and end >= start:
            ranges.append((start, end))
    return ranges


def ports_in_ranges(
    ranges: Sequence[Tuple[int, int]], lo: int, hi: int
) -> set:
    out = set()
    for start, end in ranges:
        a = max(start, lo)
        b = min(end, hi)
        if a <= b:
            out.update(range(a, b + 1))
    return out


def is_port_bindable(port: int, host: str = "0.0.0.0") -> bool:
    """Return True if this process can bind ``host:port``.

    ``SO_REUSEADDR`` is POSIX-only. On Windows it can succeed against a port
    that is already listening, which would report a busy port as free.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        if sys.platform != "win32":
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((host, port))
        return True
    except OSError:
        return False
    finally:
        sock.close()


def windows_excluded_ports(
    lo: int,
    hi: int,
    *,
    output: Optional[str] = None,
    run: Optional[RunFn] = None,
) -> set:
    if output is None:
        runner = run or _run
        output = runner(
            [
                "netsh",
                "interface",
                "ipv4",
                "show",
                "excludedportrange",
                "protocol=tcp",
            ]
        )
    return ports_in_ranges(parse_excluded_portrange(output), lo, hi)


class NoFreePortError(RuntimeError):
    """No bindable port remained in the requested range."""


def find_free_port(
    port_range: Iterable[int],
    *,
    is_bindable: Optional[Callable[[int], bool]] = None,
    excluded: Optional[Iterable[int]] = None,
    backend: Optional[str] = None,
    run: Optional[RunFn] = None,
) -> int:
    ports = list(port_range)
    if not ports:
        raise NoFreePortError("empty port range")
    blocked = set(excluded or [])
    if excluded is None and current_backend(backend) == "windows":
        blocked.update(windows_excluded_ports(min(ports), max(ports), run=run))
    probe = is_bindable or is_port_bindable
    for port in ports:
        if port in blocked:
            continue
        if probe(port):
            return port
    raise NoFreePortError(
        "no free port in %d-%d (excluded/busy: %s)"
        % (ports[0], ports[-1], sorted(blocked) or "all busy")
    )


# --- listeners --------------------------------------------------------------


def local_addr_port(local_addr: str) -> Optional[int]:
    if local_addr.startswith("["):
        close = local_addr.rfind("]")
        if close == -1 or close + 2 > len(local_addr):
            return None
        try:
            return int(local_addr[close + 2 :])
        except ValueError:
            return None
    colon = local_addr.rfind(":")
    if colon == -1:
        return None
    try:
        return int(local_addr[colon + 1 :])
    except ValueError:
        return None


def parse_netstat_listeners(text: str, port: int) -> List[int]:
    pids: List[int] = []
    for line in text.splitlines():
        parts = line.split()
        if len(parts) < 4 or parts[0].upper() != "TCP":
            continue
        if "LISTENING" not in [p.upper() for p in parts]:
            continue
        if local_addr_port(parts[1]) != port:
            continue
        try:
            pid = int(parts[-1])
        except ValueError:
            continue
        if pid and pid not in pids:
            pids.append(pid)
    return pids


def parse_lsof_pids(text: str) -> List[int]:
    pids: List[int] = []
    for line in text.splitlines():
        token = line.strip()
        if token.isdigit():
            pid = int(token)
            if pid not in pids:
                pids.append(pid)
    return pids


def listeners(
    port: int,
    *,
    backend: Optional[str] = None,
    output: Optional[str] = None,
    run: Optional[RunFn] = None,
) -> List[int]:
    kind = current_backend(backend)
    if output is not None:
        if kind == "windows":
            return parse_netstat_listeners(output, port)
        return parse_lsof_pids(output)
    runner = run or _run
    if kind == "windows":
        text = runner(["netstat", "-ano", "-p", "tcp"])
        return parse_netstat_listeners(text, port)
    text, missing = _run_allow_missing(
        ["lsof", "-nP", "-iTCP:%d" % port, "-sTCP:LISTEN", "-t"],
        run=runner,
    )
    if missing:
        return []
    return parse_lsof_pids(text)


# --- process description ----------------------------------------------------


def command_points_at_repo(command_line: str, root: Optional[Path] = None) -> bool:
    if not command_line:
        return False
    raw = root or repo_root()
    variants = []
    for candidate in (raw, raw.resolve()):
        text = str(candidate)
        variants.extend(
            [text, text.replace("\\", "/"), text.replace("/", "\\"), candidate.as_posix()]
        )
    haystack = command_line.lower()
    return any(v.lower() in haystack for v in variants if v)


def parse_cim_process(text: str, root: Optional[Path] = None) -> Optional[ProcessInfo]:
    blob = text.strip()
    if not blob:
        return None
    try:
        data = json.loads(blob)
    except ValueError:
        return None
    if isinstance(data, list):
        if not data:
            return None
        data = data[0]
    if not isinstance(data, dict):
        return None
    try:
        pid = int(data.get("ProcessId") or 0)
    except (TypeError, ValueError):
        return None
    if not pid:
        return None
    command = str(data.get("CommandLine") or "")
    info = ProcessInfo(
        pid=pid,
        name=str(data.get("Name") or ""),
        command_line=command,
        start_time=str(data.get("CreationDate") or ""),
        ppid=_as_int(data.get("ParentProcessId")),
        in_repo=command_points_at_repo(command, root),
    )
    return info


def parse_ps_process(text: str, root: Optional[Path] = None) -> Optional[ProcessInfo]:
    for line in text.splitlines():
        match = _PS_LINE.match(line)
        if not match:
            continue
        command = match.group(4).strip()
        pid = int(match.group(1))
        name = Path(command.split(" ", 1)[0]).name if command else ""
        return ProcessInfo(
            pid=pid,
            ppid=int(match.group(2)),
            start_time=match.group(3),
            command_line=command,
            name=name,
            in_repo=command_points_at_repo(command, root),
        )
    return None


def describe(
    pid: int,
    *,
    backend: Optional[str] = None,
    output: Optional[str] = None,
    run: Optional[RunFn] = None,
    root: Optional[Path] = None,
) -> Optional[ProcessInfo]:
    kind = current_backend(backend)
    if output is not None:
        if kind == "windows":
            return parse_cim_process(output, root)
        return parse_ps_process(output, root)
    runner = run or _run
    if kind == "windows":
        script = (
            "Get-CimInstance Win32_Process -Filter \"ProcessId=%d\" | "
            "Select-Object ProcessId,ParentProcessId,Name,CommandLine,CreationDate | "
            "ConvertTo-Json -Compress"
        ) % pid
        text = runner(
            ["powershell", "-NoProfile", "-Command", script],
            timeout=20,
        )
        return parse_cim_process(text, root)
    text = runner(
        ["ps", "-o", "pid=,ppid=,lstart=,command=", "-p", str(pid)],
    )
    return parse_ps_process(text, root)


# --- docker -----------------------------------------------------------------


def parse_docker_ps(text: str) -> Dict[int, List[str]]:
    """Map published host port -> container name(s)."""
    mapping: Dict[int, List[str]] = {}
    for line in text.splitlines():
        blob = line.strip()
        if not blob:
            continue
        try:
            row = json.loads(blob)
        except ValueError:
            continue
        if not isinstance(row, dict):
            continue
        names = row.get("Names") or row.get("names") or ""
        if isinstance(names, list):
            names = ",".join(str(n) for n in names)
        names = str(names)
        ports = str(row.get("Ports") or row.get("ports") or "")
        for port in published_host_ports(ports):
            mapping.setdefault(port, [])
            if names and names not in mapping[port]:
                mapping[port].append(names)
    return mapping


def published_host_ports(ports_field: str) -> List[int]:
    found: List[int] = []
    for match in _PUBLISHED_PORT.finditer(ports_field or ""):
        port = int(match.group(1))
        if port not in found:
            found.append(port)
    return found


def docker_matches_service(container_name: str, service: str) -> bool:
    return bool(
        re.search(r"(?:^|-)%s(?:-\d+)?$" % re.escape(service), container_name or "")
    )


def docker_owner(
    port: int,
    *,
    output: Optional[str] = None,
    run: Optional[RunFn] = None,
) -> List[str]:
    if output is None:
        runner = run or _run
        text, missing = _run_allow_missing(
            ["docker", "ps", "--format", "{{json .}}"],
            run=runner,
        )
        if missing:
            return []
        output = text
    return parse_docker_ps(output).get(port, [])


def classify_port_state(
    port: int,
    pids: Sequence[int],
    docker_names: Sequence[str],
    *,
    bindable: Optional[bool] = None,
) -> str:
    """Return ``free``, ``compose``, ``foreign``, or ``unknown``."""
    expected = COMPOSE_PORTS.get(port)
    if docker_names:
        if expected and any(docker_matches_service(n, expected) for n in docker_names):
            return "compose"
        return "foreign"
    if pids:
        return "foreign"
    if bindable is False:
        return "unknown"
    return "free"


def inspect_port(
    port: int,
    *,
    backend: Optional[str] = None,
    run: Optional[RunFn] = None,
    listener_output: Optional[str] = None,
    docker_output: Optional[str] = None,
    describe_output: Optional[str] = None,
    bindable: Optional[bool] = None,
    root: Optional[Path] = None,
) -> PortReport:
    pids = listeners(port, backend=backend, output=listener_output, run=run)
    names = docker_owner(port, output=docker_output, run=run)
    if bindable is None and not pids and not names:
        bindable = is_port_bindable(port)
    state = classify_port_state(port, pids, names, bindable=bindable)
    processes: List[ProcessInfo] = []
    if pids and describe_output is not None:
        info = describe(
            pids[0], backend=backend, output=describe_output, root=root
        )
        if info:
            processes.append(info)
    elif pids and describe_output is None:
        for pid in pids:
            info = describe(pid, backend=backend, run=run, root=root)
            if info:
                processes.append(info)
    kind = current_backend(backend)
    kills = [suggested_kill_command(pid, kind) for pid in pids]
    return PortReport(
        port=port,
        role=port_role(port),
        state=state,
        pids=list(pids),
        processes=processes,
        docker_names=list(names),
        kill_commands=kills,
    )


# --- process trees ----------------------------------------------------------


def parse_pid_ppid_pairs(text: str) -> List[Tuple[int, int]]:
    """Parse ``ps -ax -o pid=,ppid=`` or a JSON list of CIM process dicts."""
    blob = text.strip()
    if blob.startswith("[") or blob.startswith("{"):
        try:
            data = json.loads(blob)
        except ValueError:
            data = None
        if isinstance(data, dict):
            data = [data]
        pairs: List[Tuple[int, int]] = []
        if isinstance(data, list):
            for row in data:
                if not isinstance(row, dict):
                    continue
                pid = _as_int(row.get("ProcessId") or row.get("pid"))
                ppid = _as_int(row.get("ParentProcessId") or row.get("ppid"))
                if pid:
                    pairs.append((pid, ppid))
        return pairs
    pairs = []
    for line in text.splitlines():
        parts = line.split()
        if len(parts) < 2 or not parts[0].isdigit() or not parts[1].isdigit():
            continue
        pairs.append((int(parts[0]), int(parts[1])))
    return pairs


def descendants_from_pairs(pairs: Sequence[Tuple[int, int]], root_pid: int) -> List[int]:
    by_parent: Dict[int, List[int]] = {}
    for pid, ppid in pairs:
        by_parent.setdefault(ppid, []).append(pid)
    out: List[int] = []
    stack = list(by_parent.get(root_pid, []))
    seen = set()
    while stack:
        pid = stack.pop()
        if pid in seen or pid == root_pid:
            continue
        seen.add(pid)
        out.append(pid)
        stack.extend(by_parent.get(pid, []))
    return out


def process_parent_pairs(
    *,
    backend: Optional[str] = None,
    output: Optional[str] = None,
    run: Optional[RunFn] = None,
) -> List[Tuple[int, int]]:
    if output is not None:
        return parse_pid_ppid_pairs(output)
    runner = run or _run
    if current_backend(backend) == "windows":
        script = (
            "Get-CimInstance Win32_Process | "
            "Select-Object ProcessId,ParentProcessId | ConvertTo-Json -Compress"
        )
        text = runner(
            ["powershell", "-NoProfile", "-Command", script],
            timeout=30,
        )
        return parse_pid_ppid_pairs(text)
    text = runner(["ps", "-ax", "-o", "pid=,ppid="])
    return parse_pid_ppid_pairs(text)


def kill_tree(
    pid: int,
    pgid: Optional[int] = None,
    *,
    backend: Optional[str] = None,
    run: Optional[RunFn] = None,
    pairs: Optional[Sequence[Tuple[int, int]]] = None,
) -> None:
    """Kill a process and its children.

    Recorded POSIX launches pass ``pgid`` from ``start_new_session``. Orphans
    found only by port must not use ``killpg`` — they may share the terminal's
    process group.
    """
    kind = current_backend(backend)
    runner = run or _run
    if kind == "windows":
        runner(["taskkill", "/PID", str(pid), "/T", "/F"])
        return
    if pgid:
        _kill_pg(pgid)
        return
    tree = [pid]
    tree.extend(descendants_from_pairs(pairs or process_parent_pairs(run=run), pid))
    for target in tree:
        _kill_pid(target, signal.SIGTERM)
    time.sleep(0.3)
    for target in tree:
        _kill_pid(target, signal.SIGKILL)


def _kill_pg(pgid: int) -> None:
    try:
        os.killpg(pgid, signal.SIGTERM)
    except (OSError, ProcessLookupError):
        return
    time.sleep(0.4)
    try:
        os.killpg(pgid, 0)
    except (OSError, ProcessLookupError):
        return
    try:
        os.killpg(pgid, signal.SIGKILL)
    except (OSError, ProcessLookupError):
        return


def _kill_pid(pid: int, sig: int) -> None:
    try:
        os.kill(pid, sig)
    except (OSError, ProcessLookupError):
        return


# --- shell-command guard ----------------------------------------------------


def extract_port_flag(command: str) -> Optional[int]:
    matches = _PORT_FLAG.findall(command or "")
    if not matches:
        return None
    return int(matches[-1])


def infer_dev_server(command: str) -> Optional[Tuple[str, int]]:
    """Return ``(kind, port)`` for next/npm/uvicorn shell commands, else None."""
    text = command or ""
    if _SCRIPTS_DEV.search(text):
        return None
    if _NPM_DEV_CMD.search(text):
        return ("npm", extract_port_flag(text) or NPM_DEV_DEFAULT_PORT)
    if _NEXT_CMD.search(text):
        return ("next", extract_port_flag(text) or NEXT_DEFAULT_PORT)
    if _UVICORN_CMD.search(text):
        return ("uvicorn", extract_port_flag(text) or UVICORN_DEFAULT_PORT)
    return None


def compose_guard_verdict(command: str) -> dict:
    """Permission decision for ``beforeShellExecution``.

    * ``next`` / ``npm run dev`` on 3100: deny (the outage that started this).
    * ``uvicorn`` on 8080: ask (documented host-API + compose-db workflow).
    * any other compose port for those tools: deny.
    * non-compose ports and ``python -m scripts.dev``: allow.
    """
    inferred = infer_dev_server(command)
    if inferred is None:
        return {"permission": "allow"}
    kind, port = inferred
    if port not in COMPOSE_PORTS:
        return {"permission": "allow"}
    service = COMPOSE_PORTS[port]
    if kind == "uvicorn" and port == 8080:
        return {
            "permission": "ask",
            "user_message": (
                "Port 8080 belongs to docker compose (app). Approve only to run "
                "the host API against compose db, as documented in README local "
                "development. Agents should prefer `python -m scripts.dev api` "
                "(binds 8180-8189)."
            ),
            "agent_message": (
                "Port 8080 is compose-owned (app). For a temporary API use "
                "`python -m scripts.dev api` (binds 8180-8189), then "
                "`python -m scripts.dev stop`. Do not leave uvicorn running on 8080."
            ),
        }
    replacement = (
        "`python -m scripts.dev web` (binds 3200-3209)"
        if kind in ("next", "npm")
        else "`python -m scripts.dev api` (binds 8180-8189)"
    )
    message = (
        "Port %d belongs to docker compose (%s). Use %s instead of binding "
        "compose ports with a bare next/npm/uvicorn command."
        % (port, service, replacement)
    )
    return {
        "permission": "deny",
        "user_message": message,
        "agent_message": message + " Stop anything you start with `python -m scripts.dev stop`.",
    }


# --- subprocess helpers -----------------------------------------------------


def _as_int(value: object) -> int:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0


def _run(
    args: Sequence[str],
    timeout: int = 15,
) -> str:
    try:
        proc = subprocess.run(
            list(args),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return ""
    return proc.stdout or ""


def _run_allow_missing(
    args: Sequence[str],
    *,
    run: RunFn,
    timeout: int = 15,
) -> Tuple[str, bool]:
    """Like ``_run`` but reports whether the binary was missing.

    Custom ``run`` callables are assumed to never raise FileNotFoundError.
    """
    if run is not _run:
        try:
            return run(list(args), timeout=timeout), False
        except TypeError:
            return run(list(args)), False
    try:
        proc = subprocess.run(
            list(args),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError:
        return "", True
    except (subprocess.TimeoutExpired, OSError):
        return "", False
    return proc.stdout or "", False
