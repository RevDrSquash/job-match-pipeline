"""Local TaskQueue and stub-handler chain tests."""

from __future__ import annotations

import time

import httpx

from app.handlers import HANDLER_NAMES, get_received
from app.queue import LocalTaskQueue


def _wait_for(predicate, timeout: float = 10.0, interval: float = 0.05) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return
        time.sleep(interval)
    raise AssertionError("condition not met before timeout")


def test_local_queue_delivers_to_stub_handler(local_server: str) -> None:
    queue = LocalTaskQueue(local_server)
    marker = {"job_id": "job-42", "follow_chain": False}

    queue.enqueue("ingest-job", marker)

    def received_ingest() -> bool:
        return any(
            name == "ingest-job" and payload.get("job_id") == "job-42"
            for name, payload in get_received()
        )

    _wait_for(received_ingest)

    events = get_received()
    assert ("ingest-job", marker) in events
    # follow_chain=False must not fan out.
    assert [name for name, _ in events] == ["ingest-job"]


def test_stub_chain_enqueues_end_to_end(local_server: str) -> None:
    queue = LocalTaskQueue(local_server)
    queue.enqueue(
        "fetch-link-list",
        {"run_id": "chain-1", "follow_chain": True},
    )

    def full_chain_seen() -> bool:
        seen = {name for name, _ in get_received()}
        return set(HANDLER_NAMES).issubset(seen)

    _wait_for(full_chain_seen, timeout=15.0)

    order = [name for name, _ in get_received()]
    # Each handler appears at least once, in chain order for the first pass.
    first_index = {name: order.index(name) for name in HANDLER_NAMES}
    assert [first_index[name] for name in HANDLER_NAMES] == list(range(len(HANDLER_NAMES)))

    health = httpx.get(f"{local_server}/health", timeout=5.0)
    assert health.status_code == 200
    assert health.json()["queue_impl"] == "local"


def test_handlers_accept_post_directly(local_server: str) -> None:
    response = httpx.post(
        f"{local_server}/handlers/screen-job",
        json={"match_id": "m-1", "follow_chain": False},
        timeout=5.0,
    )
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["handler"] == "screen-job"
