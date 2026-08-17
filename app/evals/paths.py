"""Resolve versioned eval set directories without hard-coding install layout."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any


def repo_root() -> Path:
    """``app/evals/paths.py`` → repo root when running from a checkout."""
    return Path(__file__).resolve().parents[2]


def find_sets_root(explicit: Path | None = None) -> Path:
    if explicit is not None:
        return explicit.expanduser().resolve()
    env = os.environ.get("EVALS_SETS_DIR")
    if env:
        return Path(env).expanduser().resolve()
    candidates = (
        Path.cwd() / "evals" / "sets",
        repo_root() / "evals" / "sets",
    )
    for candidate in candidates:
        if _has_manifest(candidate):
            return candidate.resolve()
    return candidates[0].resolve()


def find_results_dir(explicit: Path | None = None) -> Path:
    if explicit is not None:
        return explicit.expanduser().resolve()
    env = os.environ.get("EVALS_RESULTS_DIR")
    if env:
        return Path(env).expanduser().resolve()
    checkout = repo_root() / "evals" / "results"
    if (repo_root() / "evals").is_dir():
        return checkout
    return (Path.cwd() / "evals" / "results").resolve()


def load_set(sets_root: Path, version: str | None) -> tuple[str, dict[str, Any], Path]:
    """Return ``(version, manifest, set_dir)`` for the requested or latest set."""
    if version:
        set_dir = sets_root / version
    else:
        versions = sorted(
            path
            for path in sets_root.iterdir()
            if path.is_dir() and (path / "manifest.json").is_file()
        )
        if not versions:
            raise FileNotFoundError(f"no eval set manifests under {sets_root}")
        set_dir = versions[-1]
    manifest_path = set_dir / "manifest.json"
    if not manifest_path.is_file():
        raise FileNotFoundError(f"missing eval manifest: {manifest_path}")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(manifest, dict):
        raise ValueError("eval manifest must be a JSON object")
    resolved = str(manifest.get("version") or set_dir.name)
    return resolved, manifest, set_dir


def read_json(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return data


def resolve_labeled_path(label_file: Path, relative: str) -> Path:
    path = (label_file.parent / relative).resolve()
    if not path.is_file():
        raise FileNotFoundError(f"labeled file not found: {relative}")
    return path


def _has_manifest(sets_root: Path) -> bool:
    if not sets_root.is_dir():
        return False
    return any(sets_root.glob("*/manifest.json"))
