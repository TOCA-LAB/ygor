"""Shared helpers for materialising Git repositories into cache directories."""

from __future__ import annotations

import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path

from ygor.compat import cache_marker_exists


class GitCacheError(Exception):
    """Raised when a Git cache entry cannot be materialised."""

    def __init__(self, phase: str, message: str) -> None:
        self.phase = phase
        self.message = message
        super().__init__(f"{phase} failed: {message}")


def materialize_git_checkout(
    *,
    url: str,
    ref: str | None,
    target: Path,
    sentinel_name: str = ".ygor_cloned",
    after_checkout: Callable[[Path], None] | None = None,
) -> Path:
    """Clone ``url`` to ``target`` and optionally checkout ``ref`` atomically."""
    if cache_marker_exists(target, sentinel_name):
        return target

    tmp_dir = target.parent / f".tmp_{target.name}"
    if tmp_dir.exists():
        shutil.rmtree(tmp_dir)
    target.parent.mkdir(parents=True, exist_ok=True)

    phase = "git"
    try:
        subprocess.run(
            ["git", "clone", url, str(tmp_dir)],
            check=True,
            capture_output=True,
            text=True,
        )
        if ref:
            subprocess.run(
                ["git", "-C", str(tmp_dir), "checkout", ref],
                check=True,
                capture_output=True,
                text=True,
            )
        if after_checkout is not None:
            phase = "build"
            after_checkout(tmp_dir)
        tmp_dir.rename(target)
    except subprocess.CalledProcessError as exc:
        if tmp_dir.exists():
            shutil.rmtree(tmp_dir)
        stderr = (exc.stderr or "").strip()
        raise GitCacheError(phase, stderr) from exc
    except Exception as exc:
        if tmp_dir.exists():
            shutil.rmtree(tmp_dir)
        if isinstance(exc, GitCacheError):
            raise
        raise GitCacheError(phase, str(exc)) from exc

    (target / sentinel_name).touch()
    return target
