"""Publish/sync service for local and rsync-backed stores."""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ygor import store as _store


class SyncError(Exception):
    """Raised when a sync operation cannot be completed."""


@dataclass(frozen=True)
class SyncResult:
    push_hashes: list[str]
    pull_hashes: list[str]


def is_rsync_remote(remote: str) -> bool:
    if "://" in remote:
        return True
    head = remote.split("/", 1)[0]
    return ":" in head or (":" in remote and "@" in remote.split(":", 1)[0])


def sync_stores(
    *,
    store: Path,
    remote_spec: str,
    dry_run: bool,
    on_action: Callable[[str, str], None] | None = None,
) -> SyncResult:
    """Synchronize publishable local runs with a remote store."""
    local_index = _store.read_publications(store)
    local_published = published_hashes(local_index)

    if is_rsync_remote(remote_spec):
        remote_index = read_remote_publications_rsync(remote_spec)
        remote_published = published_hashes(remote_index)
        push_hashes = sorted(
            run_hash for run_hash in local_published
            if (store / "runs" / run_hash).is_dir() and run_hash not in remote_published
        )
        pull_hashes = sorted(
            run_hash for run_hash in remote_published
            if not (store / "runs" / run_hash).exists()
        )
        for run_hash in push_hashes:
            _emit(on_action, "push", run_hash)
            copy_run_rsync(
                str(store / "runs" / run_hash),
                f"{remote_spec.rstrip('/')}/runs/{run_hash}",
                dry_run=dry_run,
            )
        for run_hash in pull_hashes:
            _emit(on_action, "pull", run_hash)
            copy_run_rsync(
                f"{remote_spec.rstrip('/')}/runs/{run_hash}",
                str(store / "runs" / run_hash),
                dry_run=dry_run,
            )
        merged = merge_publication_indexes(local_index, remote_index)
        if not dry_run:
            _store.write_publications(store, merged)
            write_remote_publications_rsync(remote_spec, merged, dry_run=False)
        return SyncResult(push_hashes=push_hashes, pull_hashes=pull_hashes)

    remote_store = Path(remote_spec).expanduser()
    if not remote_store.is_absolute():
        remote_store = remote_store.resolve()
    remote_index = _store.read_publications(remote_store)
    remote_published = published_hashes(remote_index)
    push_hashes = sorted(
        run_hash for run_hash in local_published
        if (store / "runs" / run_hash).is_dir()
        and not (remote_store / "runs" / run_hash).exists()
    )
    pull_hashes = sorted(
        run_hash for run_hash in remote_published
        if (remote_store / "runs" / run_hash).is_dir()
        and not (store / "runs" / run_hash).exists()
    )
    for run_hash in push_hashes:
        _emit(on_action, "push", run_hash)
        copy_run_local(store / "runs" / run_hash, remote_store / "runs" / run_hash, dry_run=dry_run)
    for run_hash in pull_hashes:
        _emit(on_action, "pull", run_hash)
        copy_run_local(remote_store / "runs" / run_hash, store / "runs" / run_hash, dry_run=dry_run)

    merged = merge_publication_indexes(local_index, remote_index)
    if not dry_run:
        _store.write_publications(store, merged)
        _store.write_publications(remote_store, merged)
    return SyncResult(push_hashes=push_hashes, pull_hashes=pull_hashes)


def _emit(on_action: Callable[[str, str], None] | None, action: str, run_hash: str) -> None:
    if on_action is not None:
        on_action(action, run_hash)


def copy_run_local(src: Path, dst: Path, *, dry_run: bool) -> None:
    if dry_run:
        return
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(
        src,
        dst,
        dirs_exist_ok=True,
        ignore=shutil.ignore_patterns(".experiment"),
    )


def copy_run_rsync(src: str, dst: str, *, dry_run: bool) -> None:
    args = ["rsync", "-a", "--exclude", ".experiment/"]
    if dry_run:
        args.append("--dry-run")
    args += [src.rstrip("/") + "/", dst.rstrip("/") + "/"]
    try:
        subprocess.run(args, check=True)
    except FileNotFoundError as exc:
        raise SyncError("rsync is required for non-local sync remotes.") from exc
    except subprocess.CalledProcessError as exc:
        raise SyncError(f"rsync failed with exit code {exc.returncode}.") from exc


def read_remote_publications_rsync(remote: str) -> dict[str, Any]:
    with tempfile.TemporaryDirectory() as tmp:
        target = Path(tmp) / _store.PUBLICATIONS_FILE
        args = ["rsync", "-a", f"{remote.rstrip('/')}/{_store.PUBLICATIONS_FILE}", str(target)]
        try:
            result = subprocess.run(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except FileNotFoundError as exc:
            raise SyncError("rsync is required for non-local sync remotes.") from exc
        if result.returncode != 0:
            return {"version": 1, "runs": {}}
        return json.loads(target.read_text(encoding="utf-8"))


def write_remote_publications_rsync(remote: str, data: dict[str, Any], *, dry_run: bool) -> None:
    if dry_run:
        return
    with tempfile.TemporaryDirectory() as tmp:
        source = Path(tmp) / _store.PUBLICATIONS_FILE
        source.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        args = ["rsync", "-a", str(source), f"{remote.rstrip('/')}/"]
        try:
            subprocess.run(args, check=True)
        except FileNotFoundError as exc:
            raise SyncError("rsync is required for non-local sync remotes.") from exc
        except subprocess.CalledProcessError as exc:
            raise SyncError(f"rsync failed with exit code {exc.returncode}.") from exc


def published_hashes(index: dict[str, Any]) -> set[str]:
    runs = index.get("runs") if isinstance(index.get("runs"), dict) else {}
    return {
        run_hash
        for run_hash, entry in runs.items()
        if isinstance(entry, dict) and entry.get("published", True)
    }


def merge_publication_indexes(local: dict[str, Any], remote: dict[str, Any]) -> dict[str, Any]:
    merged = {"version": 1, "runs": {}}
    for source in (remote, local):
        runs = source.get("runs") if isinstance(source.get("runs"), dict) else {}
        for run_hash, entry in runs.items():
            if isinstance(entry, dict):
                merged["runs"][run_hash] = {**entry, "published": entry.get("published", True)}
    return merged
