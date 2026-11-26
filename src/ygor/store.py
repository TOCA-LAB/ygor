"""Store directory management and run-hash computation."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any


PUBLICATIONS_FILE = "publications.json"


def compute_run_hash(recipe_text: str, meta_seed: int | None, started_at: str) -> str:
    payload = "\0".join([recipe_text, str(meta_seed), started_at])
    return hashlib.sha256(payload.encode()).hexdigest()


def run_dir_path(store_root: Path, run_hash: str) -> Path:
    return store_root / "runs" / run_hash


def publications_path(store_root: Path) -> Path:
    return store_root / PUBLICATIONS_FILE


def read_publications(store_root: Path) -> dict[str, Any]:
    path = publications_path(store_root)
    if not path.exists():
        return {"version": 1, "runs": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {"version": 1, "runs": {}}
    if not isinstance(data, dict):
        return {"version": 1, "runs": {}}
    runs = data.get("runs")
    if not isinstance(runs, dict):
        data["runs"] = {}
    data.setdefault("version", 1)
    return data


def write_publications(store_root: Path, data: dict[str, Any]) -> None:
    store_root.mkdir(parents=True, exist_ok=True)
    path = publications_path(store_root)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(path)


def create_run_dirs(run_dir: Path) -> None:
    for sub in [
        "outputs",
        "streams",
        "logs",
        ".experiment",
    ]:
        (run_dir / sub).mkdir(parents=True, exist_ok=True)


def exec_output_dir(run_dir: Path, exec_id: int) -> Path:
    return run_dir / "outputs" / f"exec_{exec_id:04d}"


def exec_stream_dir(run_dir: Path, exec_id: int) -> Path:
    return run_dir / "streams" / f"exec_{exec_id:04d}"


def write_recipe_lock(run_dir: Path, recipe_text: str) -> None:
    (run_dir / "recipe.lock.yaml").write_text(recipe_text, encoding="utf-8")


def write_lock_file(run_dir: Path) -> None:
    import os, socket
    (run_dir / ".experiment" / "lock").write_text(
        f"{os.getpid()}\n{socket.gethostname()}\n", encoding="utf-8"
    )


def remove_lock_file(run_dir: Path) -> None:
    lock = run_dir / ".experiment" / "lock"
    if lock.exists():
        lock.unlink()


def read_lock_file(run_dir: Path) -> tuple[int, str] | None:
    """Return (pid, hostname) from the lock file, or None if absent/malformed."""
    lock = run_dir / ".experiment" / "lock"
    if not lock.exists():
        return None
    try:
        lines = lock.read_text(encoding="utf-8").splitlines()
        return int(lines[0]), lines[1]
    except (IndexError, ValueError):
        return None


def pause_file_path(run_dir: Path) -> Path:
    return run_dir / ".experiment" / "pause_requested"


def force_pause_file_path(run_dir: Path) -> Path:
    return run_dir / ".experiment" / "force_pause_requested"


def create_pause_request(run_dir: Path) -> None:
    pause_file_path(run_dir).touch()


def create_force_pause_request(run_dir: Path) -> None:
    force_pause_file_path(run_dir).touch()


def clear_pause_request(run_dir: Path) -> None:
    for p in (pause_file_path(run_dir), force_pause_file_path(run_dir)):
        if p.exists():
            p.unlink()


def is_pause_requested(run_dir: Path) -> bool:
    return pause_file_path(run_dir).exists()


def is_force_pause_requested(run_dir: Path) -> bool:
    return force_pause_file_path(run_dir).exists()
