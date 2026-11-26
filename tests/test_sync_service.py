"""Tests for sync service helpers."""

from __future__ import annotations

import json
from pathlib import Path

from ygor.sync_service import merge_publication_indexes, published_hashes, sync_stores


def _write_run(store: Path, run_hash: str) -> None:
    run_dir = store / "runs" / run_hash
    run_dir.mkdir(parents=True)
    (run_dir / "manifest.json").write_text(json.dumps({"run_hash": run_hash}), encoding="utf-8")


def _publish_index(store: Path, run_hash: str) -> None:
    store.mkdir(parents=True, exist_ok=True)
    (store / "publications.json").write_text(
        json.dumps({"version": 1, "runs": {run_hash: {"published": True}}}),
        encoding="utf-8",
    )


def test_published_hashes_defaults_missing_published_to_true():
    assert published_hashes({"runs": {"a": {}, "b": {"published": False}}}) == {"a"}


def test_merge_publication_indexes_prefers_local_metadata():
    merged = merge_publication_indexes(
        {"runs": {"a": {"recipe": "local"}}},
        {"runs": {"a": {"recipe": "remote"}, "b": {"recipe": "remote"}}},
    )
    assert merged["runs"]["a"]["recipe"] == "local"
    assert merged["runs"]["b"]["recipe"] == "remote"


def test_sync_stores_local_push(tmp_path: Path):
    local = tmp_path / "local"
    remote = tmp_path / "remote"
    run_hash = "a" * 64
    _write_run(local, run_hash)
    _publish_index(local, run_hash)

    actions: list[tuple[str, str]] = []
    result = sync_stores(
        store=local,
        remote_spec=str(remote),
        dry_run=False,
        on_action=lambda action, hash_: actions.append((action, hash_)),
    )

    assert result.push_hashes == [run_hash]
    assert result.pull_hashes == []
    assert actions == [("push", run_hash)]
    assert (remote / "runs" / run_hash / "manifest.json").exists()
