"""App source resolution: clone app sources into store/sources/."""

from __future__ import annotations

import hashlib
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

from ygor.git_cache import GitCacheError, materialize_git_checkout
from ygor.schema import AppConfig, AppSource

if TYPE_CHECKING:
    from ygor.container import ContainerConfig


# ---------------------------------------------------------------------------
# Error
# ---------------------------------------------------------------------------

class AppSourceError(Exception):
    def __init__(self, name: str, message: str) -> None:
        self.name = name
        self.message = message
        super().__init__(f"app '{name}': {message}")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _source_git_target(source: AppSource, store_root: Path) -> Path:
    key = hashlib.sha256(
        f"{source.url}\0{source.ref or ''}".encode()
    ).hexdigest()[:32]
    return store_root / "sources" / key


# ---------------------------------------------------------------------------
# Per-type resolution
# ---------------------------------------------------------------------------

def _resolve_git_source(
    name: str,
    source: AppSource,
    store_root: Path,
    build: list[str] | None = None,
    on_status: Callable[[str, str], None] | None = None,
    container_config: "ContainerConfig | None" = None,
    docker_config: "ContainerConfig | None" = None,
) -> Path:
    if not source.url:
        raise AppSourceError(name, "git source requires 'url'")

    target = _source_git_target(source, store_root)
    build_config = container_config or docker_config

    def _build(tmp_dir: Path) -> None:
        if build:
            if on_status:
                on_status(name, f"building ({len(build)} step(s))…")
            for cmd in build:
                if on_status:
                    on_status(name, f"$ {cmd}")
                if build_config is not None:
                    _run_build_in_apptainer(cmd, tmp_dir, build_config)
                else:
                    subprocess.run(cmd, shell=True, cwd=tmp_dir, check=True)

    try:
        return materialize_git_checkout(
            url=source.url,
            ref=source.ref,
            target=target,
            after_checkout=_build if build else None,
        )
    except GitCacheError as exc:
        raise AppSourceError(name, f"{exc.phase} failed: {exc.message}") from exc


def _run_build_in_apptainer(cmd: str, cwd: Path, container_config: "ContainerConfig") -> None:
    """Run a single build command inside an Apptainer container."""
    from ygor.container import ContainerConfig as _CC, make_container_exec_args

    cfg = _CC(
        image=container_config.image,
        env_vars=container_config.env_vars,
        mounts=[*container_config.mounts, cwd],
        cpu_limit=container_config.cpu_limit,
        memory_limit=container_config.memory_limit,
        workdir=cwd,
    )
    run_args = make_container_exec_args(cmd, cfg)
    try:
        subprocess.run(run_args, check=True)
    except subprocess.CalledProcessError as exc:
        raise AppSourceError("build", f"apptainer build step failed: exit {exc.returncode}") from exc


_run_build_in_docker = _run_build_in_apptainer


def _resolve_local_source(name: str, source: AppSource) -> Path:
    if not source.path:
        raise AppSourceError(name, "local source requires 'path'")
    p = Path(source.path)
    if not p.exists():
        raise AppSourceError(name, f"local source path not found: {p}")
    return p.resolve()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def ensure_app_sources(
    apps: dict,
    store_root: Path,
    *,
    on_status: Callable[[str, str], None] | None = None,
    container_config: "ContainerConfig | None" = None,
    docker_config: "ContainerConfig | None" = None,
) -> dict[str, Path]:
    """Resolve all app sources that declare a *source* field.

    Returns a mapping ``app_name → resolved local path``.
    If the app declares a ``binary`` the path points to that binary inside
    the source tree; otherwise it points to the source root.
    Raises :class:`AppSourceError` on the first unresolvable source.
    """
    result: dict[str, Path] = {}

    for name, config in apps.items():
        if not isinstance(config, AppConfig) or config.source is None:
            continue

        source = config.source

        if on_status:
            on_status(name, "checking…")

        if source.type == "git":
            source_dir = _resolve_git_source(
                name,
                source,
                store_root,
                config.build,
                on_status,
                container_config=container_config,
                docker_config=docker_config,
            )
        elif source.type == "local":
            source_dir = _resolve_local_source(name, source)
        else:
            raise AppSourceError(name, f"unsupported source type: {source.type!r}")

        path = source_dir / config.binary if config.binary else source_dir

        if on_status:
            on_status(name, f"ready → {path}")

        result[name] = path

    return result
