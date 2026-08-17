"""CLI wiring for match run."""

from __future__ import annotations

from unittest.mock import patch

import httpx

from app.cli import _build_parser, main


def test_parser_accepts_match_run_modes() -> None:
    parser = _build_parser()
    incremental = parser.parse_args(["match", "run", "--mode", "incremental"])
    assert incremental.mode == "incremental"
    dirty = parser.parse_args(
        ["match", "run", "--mode", "dirty", "--dirty-cap", "3", "--since", "2026-01-01T00:00:00Z"]
    )
    assert dirty.mode == "dirty"
    assert dirty.dirty_profile_cap == 3
    assert dirty.since == "2026-01-01T00:00:00Z"


def test_match_run_posts_to_handler() -> None:
    response = httpx.Response(
        200,
        json={"status": "ok", "handler": "match-batch", "action": "completed"},
        request=httpx.Request("POST", "http://127.0.0.1:9/handlers/match-batch"),
    )
    with patch("app.cli.httpx.post", return_value=response) as post:
        assert main(["match", "run", "--mode", "incremental", "--base-url", "http://127.0.0.1:9"]) == 0
    post.assert_called_once()
    args, kwargs = post.call_args
    assert args[0] == "http://127.0.0.1:9/handlers/match-batch"
    assert kwargs["json"]["mode"] == "incremental"


def test_match_run_nonzero_on_http_error() -> None:
    with patch("app.cli.httpx.post", side_effect=httpx.ConnectError("down")):
        assert main(["match", "run", "--mode", "dirty", "--base-url", "http://127.0.0.1:9"]) == 1
