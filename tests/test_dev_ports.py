"""Port doctor parsers, free-port selection, and compose-port guard verdicts."""

from __future__ import annotations

import socket
from pathlib import Path

import pytest

from scripts.dev import (
    compose_occupant_warnings,
    load_records,
    stop_recorded_servers,
    write_record,
)
from scripts.portinfo import (
    classify_port_state,
    command_points_at_repo,
    compose_guard_verdict,
    descendants_from_pairs,
    docker_matches_service,
    extract_port_flag,
    find_free_port,
    infer_dev_server,
    inspect_port,
    is_port_bindable,
    local_addr_port,
    parse_cim_process,
    parse_docker_ps,
    parse_excluded_portrange,
    parse_lsof_pids,
    parse_netstat_listeners,
    parse_pid_ppid_pairs,
    parse_ps_process,
    ports_in_ranges,
    published_host_ports,
    suggested_kill_command,
)

WIN_NETSTAT = """\
Active Connections

  Proto  Local Address          Foreign Address        State           PID
  TCP    0.0.0.0:3100           0.0.0.0:0              LISTENING       61728
  TCP    127.0.0.1:3100         0.0.0.0:0              LISTENING       61728
  TCP    [::]:3100              [::]:0                 LISTENING       61728
  TCP    0.0.0.0:8080           0.0.0.0:0              LISTENING       44112
  TCP    192.168.1.8:54321      1.2.3.4:443            ESTABLISHED     99
  TCP    127.0.0.1:3100         127.0.0.1:54321        TIME_WAIT       0
"""

MAC_LSOF_PIDS = """\
61728
"""

MAC_PS = (
    " 61728  1000 Sat Aug 23 21:04:11 2026 "
    "/c/Program Files/nodejs/node C:/Users/trist/AppData/Roaming/npm/"
    "node_modules/npm/bin/npm-cli.js run dev\n"
)

WIN_EXCLUDED = """\
Protocol tcp Port Exclusion Ranges

Start Port    End Port
----------    --------
      3000        3099
     *5357        5357
      3200        3200

* - Administered port exclusions.
"""

DOCKER_PS = """\
{"ID":"abc","Image":"job-match-pipeline-web",\
"Ports":"0.0.0.0:3100->3000/tcp, [::]:3100->3000/tcp",\
"Names":"job-match-pipeline-web-1"}
{"ID":"def","Image":"pgvector/pgvector:pg18",\
"Ports":"0.0.0.0:5433->5432/tcp","Names":"job-match-pipeline-db-1"}
{"ID":"ghi","Image":"job-match-pipeline-app","Ports":"8080/tcp",\
"Names":"job-match-pipeline-app-1"}
"""

CIM_JSON = (
    '{"ProcessId":61728,"ParentProcessId":4120,"Name":"node.exe",'
    '"CommandLine":"node  E:\\\\dev\\\\job-match-pipeline\\\\frontend'
    '\\\\node_modules\\\\next\\\\dist\\\\bin\\\\next dev -p 3100",'
    '"CreationDate":"20260823210411.000000-420"}'
)


def test_parse_netstat_listeners_only_listening() -> None:
    pids = parse_netstat_listeners(WIN_NETSTAT, 3100)
    assert pids == [61728]
    assert parse_netstat_listeners(WIN_NETSTAT, 8080) == [44112]
    assert parse_netstat_listeners(WIN_NETSTAT, 5433) == []


def test_local_addr_port_ipv4_and_ipv6() -> None:
    assert local_addr_port("0.0.0.0:3100") == 3100
    assert local_addr_port("[::]:3100") == 3100
    assert local_addr_port("127.0.0.1:8080") == 8080


def test_parse_lsof_and_ps() -> None:
    assert parse_lsof_pids(MAC_LSOF_PIDS) == [61728]
    info = parse_ps_process(MAC_PS)
    assert info is not None
    assert info.pid == 61728
    assert info.ppid == 1000
    assert info.start_time == "Sat Aug 23 21:04:11 2026"
    assert "npm-cli.js" in info.command_line


def test_parse_cim_process_marks_in_repo() -> None:
    info = parse_cim_process(CIM_JSON, root=Path(r"E:\dev\job-match-pipeline"))
    assert info is not None
    assert info.pid == 61728
    assert info.name == "node.exe"
    assert info.in_repo is True


def test_command_points_at_repo_slash_variants() -> None:
    root = Path("/Users/dev/job-match-pipeline")
    assert command_points_at_repo("node /Users/dev/job-match-pipeline/frontend", root)
    assert not command_points_at_repo("node /opt/homebrew/bin/node", root)


def test_excluded_portrange_and_find_free_port() -> None:
    ranges = parse_excluded_portrange(WIN_EXCLUDED)
    assert (3000, 3099) in ranges
    assert (5357, 5357) in ranges
    blocked = ports_in_ranges(ranges, 3200, 3209)
    assert blocked == {3200}

    taken = {3201, 3202}

    def bindable(port: int) -> bool:
        return port not in taken and port not in blocked

    assert find_free_port(range(3200, 3210), is_bindable=bindable, excluded=blocked) == 3203


def test_find_free_port_exhausted() -> None:
    with pytest.raises(Exception, match="no free port"):
        find_free_port(range(3200, 3203), is_bindable=lambda _p: False, excluded=set())


def test_is_port_bindable_ephemeral_socket() -> None:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind(("0.0.0.0", 0))
        sock.listen(1)
        port = sock.getsockname()[1]
        assert is_port_bindable(port) is False
    finally:
        sock.close()
    assert is_port_bindable(port) is True


def test_docker_ps_published_ports() -> None:
    mapping = parse_docker_ps(DOCKER_PS)
    assert mapping[3100] == ["job-match-pipeline-web-1"]
    assert mapping[5433] == ["job-match-pipeline-db-1"]
    assert 8080 not in mapping
    assert published_host_ports("0.0.0.0:3100->3000/tcp, [::]:3100->3000/tcp") == [3100]
    assert docker_matches_service("job-match-pipeline-web-1", "web")
    assert docker_matches_service("job-match-pipeline-app-1", "app")
    assert not docker_matches_service("job-match-pipeline-web-1", "app")


def test_classify_compose_vs_foreign() -> None:
    assert classify_port_state(3100, [], ["job-match-pipeline-web-1"]) == "compose"
    assert classify_port_state(3100, [61728], []) == "foreign"
    assert classify_port_state(3100, [], [], bindable=True) == "free"
    assert classify_port_state(3100, [], [], bindable=False) == "unknown"
    assert classify_port_state(3100, [], ["unrelated-nginx-1"]) == "foreign"


def test_inspect_port_windows_fixture() -> None:
    report = inspect_port(
        3100,
        backend="windows",
        listener_output=WIN_NETSTAT,
        docker_output="",
        describe_output=CIM_JSON,
        bindable=False,
        root=Path(r"E:\dev\job-match-pipeline"),
    )
    assert report.state == "foreign"
    assert report.pids == [61728]
    assert report.processes[0].in_repo is True
    assert report.kill_commands == ["taskkill /PID 61728 /T /F"]


def test_inspect_port_compose_owner_from_docker() -> None:
    report = inspect_port(
        3100,
        backend="posix",
        listener_output="1234\n",
        docker_output=DOCKER_PS,
        bindable=False,
    )
    assert report.state == "compose"
    assert report.docker_names == ["job-match-pipeline-web-1"]


def test_descendants_and_pid_ppid_parsers() -> None:
    ps_text = "  100  1\n  200  100\n  201  100\n  300  200\n"
    pairs = parse_pid_ppid_pairs(ps_text)
    assert set(descendants_from_pairs(pairs, 100)) == {200, 201, 300}
    cim = '[{"ProcessId":200,"ParentProcessId":100},{"ProcessId":300,"ParentProcessId":200}]'
    assert set(descendants_from_pairs(parse_pid_ppid_pairs(cim), 100)) == {200, 300}


def test_suggested_kill_command() -> None:
    assert suggested_kill_command(9, "windows") == "taskkill /PID 9 /T /F"
    assert suggested_kill_command(9, "posix") == "kill 9"


def test_extract_port_flag_last_wins() -> None:
    assert extract_port_flag("next dev -p 3100") == 3100
    assert extract_port_flag("next dev -p 3100 -p 3201") == 3201
    assert extract_port_flag("uvicorn app.main:app --port=8080") == 8080
    assert extract_port_flag("next dev") is None


def test_infer_dev_server_defaults_and_scripts_dev() -> None:
    assert infer_dev_server("npm run dev") == ("npm", 3100)
    assert infer_dev_server("npm start") == ("npm", 3100)
    assert infer_dev_server("npx next dev") == ("next", 3000)
    assert infer_dev_server("next dev -p 3100") == ("next", 3100)
    assert infer_dev_server("uvicorn app.main:app --reload") == ("uvicorn", 8000)
    assert infer_dev_server("python -m scripts.dev web") is None
    assert infer_dev_server("python -m scripts.dev api") is None


def test_guard_denies_next_on_3100_asks_uvicorn_on_8080() -> None:
    deny = compose_guard_verdict("npm run dev")
    assert deny["permission"] == "deny"
    assert "3200-3209" in deny["agent_message"]

    deny_next = compose_guard_verdict("npx next dev -p 3100")
    assert deny_next["permission"] == "deny"

    ask = compose_guard_verdict("uvicorn app.main:app --host 0.0.0.0 --port 8080")
    assert ask["permission"] == "ask"
    assert "8180-8189" in ask["agent_message"]

    allow_agent = compose_guard_verdict("npm run dev -- -p 3201")
    assert allow_agent == {"permission": "allow"}

    allow_next_default = compose_guard_verdict("next dev")
    assert allow_next_default == {"permission": "allow"}

    allow_uvicorn_default = compose_guard_verdict("uvicorn app.main:app --reload")
    assert allow_uvicorn_default == {"permission": "allow"}

    allow_launcher = compose_guard_verdict("python -m scripts.dev web")
    assert allow_launcher == {"permission": "allow"}


def test_stop_recorded_skips_keep_unless_all(tmp_path: Path) -> None:
    write_record(
        {"kind": "web", "port": 3200, "pid": 11, "pgid": 11, "keep": False},
        root=tmp_path,
    )
    write_record(
        {"kind": "api", "port": 8180, "pid": 22, "pgid": 22, "keep": True},
        root=tmp_path,
    )
    killed: list[tuple[int, int | None]] = []

    def fake_kill(pid: int, pgid: int | None = None, **_kwargs: object) -> None:
        killed.append((pid, pgid))

    messages = stop_recorded_servers(include_keep=False, root=tmp_path, kill=fake_kill)
    assert killed == [(11, 11)]
    assert any("3200" in m for m in messages)
    remaining = load_records(tmp_path)
    assert len(remaining) == 1
    assert remaining[0]["keep"] is True

    stop_recorded_servers(include_keep=True, root=tmp_path, kill=fake_kill)
    assert load_records(tmp_path) == []


def test_compose_occupant_warnings_skip_compose_and_free(tmp_path: Path) -> None:
    from scripts.portinfo import PortReport

    def fake_inspect(port: int) -> PortReport:
        if port == 3100:
            return PortReport(
                port=3100,
                role="compose-web",
                state="foreign",
                pids=[61728],
                kill_commands=["taskkill /PID 61728 /T /F"],
            )
        if port == 8080:
            return PortReport(
                port=8080,
                role="compose-app",
                state="compose",
                docker_names=["job-match-pipeline-app-1"],
            )
        return PortReport(port=port, role="compose-db", state="free")

    warnings = compose_occupant_warnings(root=tmp_path, inspect=fake_inspect)
    assert len(warnings) == 1
    assert "3100" in warnings[0]
    assert "Not killing" in warnings[0]
