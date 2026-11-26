"""Command executor: single-process and parallel (thread-pool) variants."""

from __future__ import annotations

import re
import os
import signal
import subprocess
import threading
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

if TYPE_CHECKING:
    from ygor.container import ContainerConfig

from jinja2.sandbox import SandboxedEnvironment

from ygor.schema import OnFailure, Recipe

_DURATION_RE = re.compile(r"^(\d+(?:\.\d+)?)(s|m|h|d)$")
_UNITS = {"s": 1.0, "m": 60.0, "h": 3600.0, "d": 86400.0}
_FORCE_PAUSED_REASON = "force-paused"


def parse_duration_seconds(s: str | None) -> float | None:
    if s is None:
        return None
    m = _DURATION_RE.match(s)
    if not m:
        raise ValueError(f"cannot parse duration: {s!r}")
    return float(m.group(1)) * _UNITS[m.group(2)]


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# Records
# ---------------------------------------------------------------------------

@dataclass
class ExecRecord:
    exec_id: int
    params: dict[str, Any]
    seed: int
    status: str = "pending"
    started_at: str | None = None
    ended_at: str | None = None
    duration_sec: float | None = None
    exit_code: int | None = None
    failure_reason: str | None = None
    retry_count: int = 0
    output_dir: str = ""
    stdout_path: str = ""
    stderr_path: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "exec_id": self.exec_id,
            "params": self.params,
            "seed": self.seed,
            "status": self.status,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "duration_sec": self.duration_sec,
            "exit_code": self.exit_code,
            "failure_reason": self.failure_reason,
            "retry_count": self.retry_count,
            "output_dir": self.output_dir,
            "stdout_path": self.stdout_path,
            "stderr_path": self.stderr_path,
        }


@dataclass
class PostRecord:
    command: str
    status: str = "pending"
    started_at: str | None = None
    ended_at: str | None = None
    exit_code: int | None = None
    failure_reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "command": self.command,
            "status": self.status,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "exit_code": self.exit_code,
            "failure_reason": self.failure_reason,
        }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _render(commands: list[str], context: dict[str, Any]) -> list[str]:
    env = SandboxedEnvironment()
    return [env.from_string(cmd).render(**context) for cmd in commands]


def _execution_context(
    *,
    params: dict[str, Any],
    exec_id: int,
    seed: int,
    run_hash: str,
    run_dir: Path,
    out_dir: Path,
    dataset_paths: dict[str, str],
    app_paths: dict[str, str] | None,
    recipe_dir: Path | None,
) -> dict[str, Any]:
    return {
        "param": params,
        "run": {
            "output_dir": str(out_dir),
            "run_dir": str(run_dir),
            "seed": seed,
            "exec_id": exec_id,
            "hash": run_hash[:8],
        },
        "dataset": dataset_paths,
        "app": app_paths or {},
        "apps": app_paths or {},
        "recipe": {
            "dir": str(recipe_dir or Path.cwd()),
        },
    }


def _post_context(
    *,
    run_hash: str,
    run_dir: Path,
    dataset_paths: dict[str, str] | None,
    app_paths: dict[str, str] | None,
    recipe_dir: Path | None,
) -> dict[str, Any]:
    return {
        "param": {},
        "run": {
            "run_dir": str(run_dir),
            "hash": run_hash[:8],
        },
        "dataset": dataset_paths or {},
        "app": app_paths or {},
        "apps": app_paths or {},
        "recipe": {
            "dir": str(recipe_dir or Path.cwd()),
        },
    }


def _mark_execution_started(manifest: dict[str, Any]) -> None:
    counts = manifest["state"]["counts"]
    counts["pending"] -= 1
    counts["running"] += 1


def _upsert_execution_record(manifest: dict[str, Any], record: ExecRecord) -> None:
    existing = {e["exec_id"]: e for e in (manifest.get("executions") or [])}
    existing[record.exec_id] = record.to_dict()
    manifest["executions"] = [existing[k] for k in sorted(existing.keys())]


def _mark_execution_finished(
    manifest: dict[str, Any],
    record: ExecRecord,
    *,
    persist_record: bool = True,
) -> None:
    counts = manifest["state"]["counts"]
    counts["running"] -= 1
    if record.status == "force_paused":
        counts["pending"] += 1
    elif record.status == "completed":
        counts["completed"] += 1
    else:
        counts["failed"] += 1
    if persist_record:
        _upsert_execution_record(manifest, record)


def _mark_aborted(manifest: dict[str, Any]) -> None:
    remaining = manifest["state"]["counts"]["pending"]
    if remaining > 0:
        manifest["state"]["counts"]["pending"] = 0
    manifest["state"]["status"] = "failed"
    manifest["state"]["ended_at"] = _now_iso()


def _run_shell(
    cmd: str,
    cwd: Path,
    log_path: Path,
    timeout: float | None,
    attempt: int,
    container_config: "ContainerConfig | None" = None,
    docker_config: "ContainerConfig | None" = None,
    force_pause_requested: Callable[[], bool] | None = None,
    stdout_path: Path | None = None,
    stderr_path: Path | None = None,
) -> tuple[int, str]:
    """Run one shell command, capturing process streams when requested.

    When *container_config* is provided the command is executed inside an
    Apptainer container; otherwise it runs as a plain subprocess.
    Returns (exit_code, failure_reason).
    """
    process: subprocess.Popen | None = None
    log_fh = None
    stdout_fh = None
    stderr_fh = None
    container_config = container_config or docker_config
    try:
        with log_path.open("ab") as log_fh:
            if force_pause_requested and force_pause_requested():
                return -2, _FORCE_PAUSED_REASON

            stdout_fh = stdout_path.open("ab") if stdout_path is not None else log_fh
            stderr_fh = stderr_path.open("ab") if stderr_path is not None else None

            if container_config is not None:
                from ygor.container import ContainerConfig as _CC, make_container_exec_args
                cfg_with_workdir = _CC(
                    image=container_config.image,
                    env_vars=container_config.env_vars,
                    mounts=container_config.mounts,
                    cpu_limit=container_config.cpu_limit,
                    memory_limit=container_config.memory_limit,
                    workdir=cwd,
                )
                run_args = make_container_exec_args(cmd, cfg_with_workdir)
                header = f"=== attempt {attempt + 1} (apptainer:{container_config.image}) ===\n$ {cmd}\n"
                log_fh.write(header.encode())
                log_fh.flush()
                process = subprocess.Popen(
                    run_args,
                    stdout=stdout_fh,
                    stderr=stderr_fh if stderr_fh is not None else subprocess.STDOUT,
                    start_new_session=True,
                )
            else:
                header = f"=== attempt {attempt + 1} ===\n$ {cmd}\n"
                log_fh.write(header.encode())
                log_fh.flush()
                process = subprocess.Popen(
                    cmd,
                    shell=True,
                    stdout=stdout_fh,
                    stderr=stderr_fh if stderr_fh is not None else subprocess.STDOUT,
                    cwd=cwd,
                    start_new_session=True,
                )

            started = time.monotonic()
            while True:
                return_code = process.poll()
                if return_code is not None:
                    return return_code, ""

                if force_pause_requested and force_pause_requested():
                    _terminate_process_group(process)
                    return -2, _FORCE_PAUSED_REASON

                if timeout is not None and (time.monotonic() - started) >= timeout:
                    _terminate_process_group(process)
                    return -1, "timeout"

                time.sleep(0.05)
    except (OSError, ValueError) as exc:
        if process and process.poll() is None:
            _terminate_process_group(process)
        return -1, f"subprocess error: {exc}"
    finally:
        if stdout_fh is not None and stdout_fh is not log_fh:
            stdout_fh.close()
        if stderr_fh is not None:
            stderr_fh.close()


def _terminate_process_group(process: subprocess.Popen) -> None:
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    except Exception:
        process.terminate()

    try:
        process.wait(timeout=2)
        return
    except subprocess.TimeoutExpired:
        pass

    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        return
    except Exception:
        process.kill()
    try:
        process.wait(timeout=2)
    except subprocess.TimeoutExpired:
        pass


def _run_one(
    exec_id: int,
    params: dict[str, Any],
    seed: int,
    recipe: Recipe,
    run_dir: Path,
    run_hash: str,
    timeout: float | None,
    max_retries: int,
    dataset_paths: dict[str, str],
    app_paths: dict[str, str] | None = None,
    recipe_dir: Path | None = None,
    container_config: "ContainerConfig | None" = None,
    force_pause_requested: Callable[[], bool] | None = None,
) -> ExecRecord:
    from ygor.store import exec_output_dir, exec_stream_dir

    out_dir = exec_output_dir(run_dir, exec_id)
    out_dir.mkdir(parents=True, exist_ok=True)
    stream_dir = exec_stream_dir(run_dir, exec_id)
    stream_dir.mkdir(parents=True, exist_ok=True)
    log_path = run_dir / "logs" / f"exec_{exec_id:04d}.log"
    stdout_path = stream_dir / "stdout.log"
    stderr_path = stream_dir / "stderr.log"

    context = _execution_context(
        params=params,
        exec_id=exec_id,
        seed=seed,
        run_hash=run_hash,
        run_dir=run_dir,
        out_dir=out_dir,
        dataset_paths=dataset_paths,
        app_paths=app_paths,
        recipe_dir=recipe_dir,
    )

    record = ExecRecord(
        exec_id=exec_id,
        params=params,
        seed=seed,
        output_dir=str(out_dir.relative_to(run_dir)),
        stdout_path=str(stdout_path.relative_to(run_dir)),
        stderr_path=str(stderr_path.relative_to(run_dir)),
    )

    for attempt in range(max_retries + 1):
        record.started_at = _now_iso()
        t0 = time.monotonic()

        try:
            rendered = _render(recipe.command, context)
        except Exception as exc:
            record.ended_at = _now_iso()
            record.duration_sec = round(time.monotonic() - t0, 3)
            record.exit_code = -1
            record.failure_reason = f"template error: {exc}"
            record.retry_count = attempt
            record.status = "failed"
            return record

        exit_code = 0
        failure_reason = ""
        for cmd in rendered:
            code, reason = _run_shell(
                cmd,
                out_dir,
                log_path,
                timeout,
                attempt,
                container_config=container_config,
                force_pause_requested=force_pause_requested,
                stdout_path=stdout_path,
                stderr_path=stderr_path,
            )
            if code != 0:
                exit_code = code
                failure_reason = reason or f"exit {code}"
                break

        record.ended_at = _now_iso()
        record.duration_sec = round(time.monotonic() - t0, 3)
        record.exit_code = exit_code
        record.retry_count = attempt

        if failure_reason == _FORCE_PAUSED_REASON:
            record.status = "force_paused"
            record.failure_reason = failure_reason
            return record

        if exit_code == 0:
            record.status = "completed"
            return record

        if attempt < max_retries:
            continue

        record.status = "failed"
        record.failure_reason = failure_reason
        return record

    record.status = "failed"  # unreachable, satisfies type checker
    return record


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

WriteManifestFn = Callable[[dict[str, Any], Path], None]
ProgressFn = Callable[[ExecRecord], None]


def execute_run(
    recipe: Recipe,
    executions: list[tuple[int, dict[str, Any], int]],
    run_hash: str,
    run_dir: Path,
    manifest: dict[str, Any],
    write_manifest: WriteManifestFn,
    *,
    on_progress: ProgressFn | None = None,
    dataset_paths: dict[str, str] | None = None,
    app_paths: dict[str, str] | None = None,
    recipe_dir: Path | None = None,
    container_config: "ContainerConfig | None" = None,
    docker_config: "ContainerConfig | None" = None,
) -> dict[str, Any]:
    """Execute all (exec_id, param, seed) triples sequentially, updating the manifest in place."""
    timeout = parse_duration_seconds(recipe.policy.timeout_per_run)
    max_retries = recipe.policy.retries
    on_failure = recipe.policy.on_failure
    ds = dataset_paths or {}
    container_config = container_config or docker_config

    for pos, (exec_id, params, seed) in enumerate(executions):
        _mark_execution_started(manifest)
        write_manifest(manifest, run_dir)

        record = _run_one(
            exec_id, params, seed, recipe, run_dir, run_hash, timeout,
            max_retries, ds, app_paths, recipe_dir, container_config,
        )

        _mark_execution_finished(manifest, record)
        write_manifest(manifest, run_dir)

        if on_progress:
            on_progress(record)

        if record.status == "failed" and on_failure == OnFailure.abort:
            _mark_aborted(manifest)
            write_manifest(manifest, run_dir)
            return manifest

    return manifest


def execute_run_parallel(
    recipe: Recipe,
    executions: list[tuple[int, dict[str, Any], int]],
    run_hash: str,
    run_dir: Path,
    manifest: dict[str, Any],
    write_manifest: WriteManifestFn,
    *,
    workers: int = 1,
    on_progress: ProgressFn | None = None,
    dataset_paths: dict[str, str] | None = None,
    app_paths: dict[str, str] | None = None,
    recipe_dir: Path | None = None,
    container_config: "ContainerConfig | None" = None,
    docker_config: "ContainerConfig | None" = None,
) -> dict[str, Any]:
    """Execute (exec_id, param, seed) triples with up to *workers* concurrent threads.

    Respects a pause sentinel file at ``.experiment/pause_requested``:
    once detected, no new executions are dispatched; running ones finish
    normally and the manifest state is set to ``"paused"``.
    """
    from ygor.store import is_pause_requested, is_force_pause_requested, clear_pause_request

    timeout = parse_duration_seconds(recipe.policy.timeout_per_run)
    max_retries = recipe.policy.retries
    on_failure = recipe.policy.on_failure
    ds = dataset_paths or {}
    container_config = container_config or docker_config

    lock = threading.Lock()
    abort_event = threading.Event()
    pause_event = threading.Event()
    force_pause_event = threading.Event()

    execution_iter = iter(executions)

    def _submit_next(executor: ThreadPoolExecutor, active: dict) -> bool:
        try:
            exec_id, params, seed = next(execution_iter)
        except StopIteration:
            return False

        with lock:
            _mark_execution_started(manifest)

        future = executor.submit(
            _run_one, exec_id, params, seed, recipe, run_dir, run_hash,
            timeout, max_retries, ds, app_paths, recipe_dir, container_config,
            lambda: force_pause_event.is_set() or is_force_pause_requested(run_dir),
        )
        active[future] = exec_id
        return True

    with ThreadPoolExecutor(max_workers=workers) as executor:
        active_futures: dict = {}

        for _ in range(workers):
            if is_force_pause_requested(run_dir):
                force_pause_event.set()
                break
            if is_pause_requested(run_dir):
                pause_event.set()
                break
            if not _submit_next(executor, active_futures):
                break

        while active_futures:
            done, _ = wait(active_futures.keys(), return_when=FIRST_COMPLETED)

            for future in done:
                active_futures.pop(future, None)
                record: ExecRecord = future.result()

                with lock:
                    _mark_execution_finished(
                        manifest,
                        record,
                        persist_record=record.status != "force_paused",
                    )
                    if record.status == "force_paused":
                        force_pause_event.set()
                    write_manifest(manifest, run_dir)

                if on_progress and record.status != "force_paused":
                    on_progress(record)

                # Check for pause after every completed execution.
                if not force_pause_event.is_set() and is_force_pause_requested(run_dir):
                    force_pause_event.set()
                if not pause_event.is_set() and is_pause_requested(run_dir):
                    pause_event.set()

                if record.status == "failed" and on_failure == OnFailure.abort:
                    abort_event.set()

            if abort_event.is_set() or pause_event.is_set() or force_pause_event.is_set():
                continue

            while len(active_futures) < workers:
                if is_force_pause_requested(run_dir):
                    force_pause_event.set()
                    break
                if is_pause_requested(run_dir):
                    pause_event.set()
                    break
                if not _submit_next(executor, active_futures):
                    break

            if not active_futures:
                break

    clear_pause_request(run_dir)

    if abort_event.is_set():
        _mark_aborted(manifest)
        write_manifest(manifest, run_dir)
    elif pause_event.is_set() or force_pause_event.is_set():
        manifest["state"]["status"] = "paused"
        write_manifest(manifest, run_dir)

    return manifest


def execute_post(
    recipe: Recipe,
    run_hash: str,
    run_dir: Path,
    manifest: dict[str, Any],
    write_manifest: WriteManifestFn,
    *,
    dataset_paths: dict[str, str] | None = None,
    app_paths: dict[str, str] | None = None,
    recipe_dir: Path | None = None,
    on_status: Callable[[str], None] | None = None,
    container_config: "ContainerConfig | None" = None,
    docker_config: "ContainerConfig | None" = None,
) -> dict[str, Any]:
    """Run all post commands once, after all executions finish."""
    container_config = container_config or docker_config
    context = _post_context(
        run_hash=run_hash,
        run_dir=run_dir,
        dataset_paths=dataset_paths,
        app_paths=app_paths,
        recipe_dir=recipe_dir,
    )
    log_path = run_dir / "logs" / "post.log"
    post_records: list[PostRecord] = []

    for cmd_template in recipe.post:
        try:
            cmd = SandboxedEnvironment().from_string(cmd_template).render(**context)
        except Exception as exc:
            if on_status:
                on_status(f"template error: {exc}")
            rec = PostRecord(
                command=cmd_template,
                status="failed",
                started_at=_now_iso(),
                ended_at=_now_iso(),
                exit_code=-1,
                failure_reason=f"template error: {exc}",
            )
            post_records.append(rec)
            manifest["post"] = [r.to_dict() for r in post_records]
            write_manifest(manifest, run_dir)
            continue

        if on_status:
            on_status(f"$ {cmd}")
        rec = PostRecord(command=cmd, status="running", started_at=_now_iso())
        exit_code, failure_reason = _run_shell(cmd, run_dir, log_path, None, 0, container_config)
        rec.status = "completed" if exit_code == 0 else "failed"
        rec.ended_at = _now_iso()
        rec.exit_code = exit_code
        if failure_reason:
            rec.failure_reason = failure_reason
            if on_status:
                on_status(f"failed: {failure_reason}")
        post_records.append(rec)
        manifest["post"] = [r.to_dict() for r in post_records]
        write_manifest(manifest, run_dir)

    return manifest
