"""Manifest (manifest.json) build, write and read."""

from __future__ import annotations

import csv
import json
import os
import platform
import socket
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

MANIFEST_VERSION = 1
TOOL_VERSION = "ygor 0.2.0"
SEEDS_ALGORITHM = "uniform_int32_via_numpy"
_EXECUTION_EXPORT_FIELDS = [
    "run_hash",
    "exec_id",
    "status",
    "exit_code",
    "duration_sec",
    "started_at",
    "ended_at",
    "seed",
    "retry_count",
    "failure_reason",
    "output_dir",
    "stdout_path",
    "stderr_path",
    "log_path",
]


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _host_info() -> dict[str, Any]:
    info: dict[str, Any] = {
        "hostname": socket.gethostname(),
        "os": platform.platform(),
        "kernel": platform.release(),
        "cpu_count_logical": os.cpu_count(),
    }
    try:
        mem_bytes = os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")  # type: ignore[attr-defined]
        info["memory_total_gb"] = round(mem_bytes / (1024**3), 1)
    except (AttributeError, ValueError, OSError):
        pass
    return info


def build_initial(
    *,
    run_hash: str,
    recipe_path: str,
    recipe_dict: dict[str, Any],
    seeds_info: dict[str, Any] | None,
    total_executions: int,
    started_at: str,
) -> dict[str, Any]:
    return {
        "manifest_version": MANIFEST_VERSION,
        "tool_version": TOOL_VERSION,
        "run_hash": run_hash,
        "recipe_path": recipe_path,
        "recipe": recipe_dict,
        "seeds": seeds_info,
        "host": _host_info(),
        "state": {
            "status": "running",
            "started_at": started_at,
            "ended_at": None,
            "last_updated": started_at,
            "total_executions": total_executions,
            "counts": {
                "completed": 0,
                "failed": 0,
                "running": 0,
                "pending": total_executions,
            },
        },
        "executions": [],
        "post": [],
    }


def write_manifest(manifest: dict[str, Any], run_dir: Path) -> None:
    """Write only manifest.json atomically."""
    manifest["state"]["last_updated"] = _now_iso()
    path = run_dir / "manifest.json"
    tmp = path.with_suffix(".tmp")
    tmp.write_text(
        json.dumps(manifest, indent=2, default=str),
        encoding="utf-8",
    )
    tmp.replace(path)


def write(manifest: dict[str, Any], run_dir: Path) -> None:
    write_manifest(manifest, run_dir)
    write_execution_exports(manifest, run_dir)


def read(run_dir: Path) -> dict[str, Any]:
    return json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))


def _log_path(exec_id: Any) -> str:
    try:
        return f"logs/exec_{int(exec_id):04d}.log"
    except (TypeError, ValueError):
        return ""


def _cell(value: Any) -> Any:
    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        return json.dumps(value, sort_keys=True)
    return value


def _relative_path(value: Any, run_dir: Path) -> Any:
    if not isinstance(value, (str, Path)):
        return value
    path = Path(value)
    if not path.is_absolute():
        return path.as_posix()
    try:
        return Path(os.path.relpath(path, run_dir)).as_posix()
    except ValueError:
        return str(value)


def _export_params(params: dict[str, Any], run_dir: Path) -> dict[str, Any]:
    exported = dict(params)
    if "instance" in exported:
        exported["instance"] = _relative_path(exported["instance"], run_dir)
    return exported


def _execution_export_rows(manifest: dict[str, Any], run_dir: Path) -> tuple[list[str], list[dict[str, Any]]]:
    executions = manifest.get("executions") or []
    if not isinstance(executions, list):
        executions = []

    param_keys: list[str] = []
    seen_params: set[str] = set()
    for item in executions:
        params = item.get("params") if isinstance(item, dict) else None
        if not isinstance(params, dict):
            continue
        for key in params:
            if key not in seen_params:
                seen_params.add(key)
                param_keys.append(key)

    rows: list[dict[str, Any]] = []
    run_hash = manifest.get("run_hash") or ""
    for item in executions:
        if not isinstance(item, dict):
            continue
        exec_id = item.get("exec_id")
        raw_params = item.get("params") if isinstance(item.get("params"), dict) else {}
        params = _export_params(raw_params, run_dir)
        row = {
            "run_hash": run_hash,
            "exec_id": exec_id,
            "status": item.get("status"),
            "exit_code": item.get("exit_code"),
            "duration_sec": item.get("duration_sec"),
            "started_at": item.get("started_at"),
            "ended_at": item.get("ended_at"),
            "seed": item.get("seed"),
            "retry_count": item.get("retry_count"),
            "failure_reason": item.get("failure_reason"),
            "output_dir": item.get("output_dir") or "",
            "stdout_path": item.get("stdout_path") or "",
            "stderr_path": item.get("stderr_path") or "",
            "log_path": _log_path(exec_id),
            "params": params,
        }
        for key in param_keys:
            row[f"param.{key}"] = params.get(key)
        rows.append(row)

    return [*_EXECUTION_EXPORT_FIELDS, *(f"param.{key}" for key in param_keys)], rows


def write_execution_exports(manifest: dict[str, Any], run_dir: Path) -> None:
    """Write analysis-friendly execution tables derived from the manifest."""
    fields, rows = _execution_export_rows(manifest, run_dir)

    csv_path = run_dir / "executions.csv"
    csv_tmp = csv_path.with_suffix(".tmp")
    with csv_tmp.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({field: _cell(row.get(field)) for field in fields})
    csv_tmp.replace(csv_path)

    jsonl_path = run_dir / "executions.jsonl"
    jsonl_tmp = jsonl_path.with_suffix(".tmp")
    with jsonl_tmp.open("w", encoding="utf-8") as fh:
        for row in rows:
            payload = {field: row.get(field) for field in _EXECUTION_EXPORT_FIELDS}
            payload["params"] = row.get("params") or {}
            fh.write(json.dumps(payload, sort_keys=True, default=str) + "\n")
    jsonl_tmp.replace(jsonl_path)
