"""Tests for shared Git cache helpers."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from ygor.git_cache import GitCacheError, materialize_git_checkout


def _make_local_git_repo(tmp_path: Path) -> tuple[Path, str]:
    repo = tmp_path / "repo"
    repo.mkdir()
    env = {"GIT_CONFIG_NOSYSTEM": "1", "HOME": str(tmp_path)}
    for cmd in [
        ["git", "init", str(repo)],
        ["git", "-C", str(repo), "config", "user.email", "t@t.com"],
        ["git", "-C", str(repo), "config", "user.name", "T"],
    ]:
        subprocess.run(cmd, check=True, capture_output=True, env=env)
    (repo / "data.txt").write_text("content")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True, capture_output=True, env=env)
    subprocess.run(["git", "-C", str(repo), "commit", "-m", "init"], check=True, capture_output=True, env=env)
    result = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
        env=env,
    )
    return repo, result.stdout.strip()


def test_materialize_git_checkout(tmp_path: Path):
    repo, commit = _make_local_git_repo(tmp_path)
    target = tmp_path / "cache" / "entry"
    result = materialize_git_checkout(url=f"file://{repo}", ref=commit, target=target)
    assert result == target
    assert (target / "data.txt").read_text() == "content"
    assert (target / ".ygor_cloned").exists()


def test_materialize_git_checkout_uses_sentinel(tmp_path: Path):
    target = tmp_path / "cache" / "entry"
    target.mkdir(parents=True)
    (target / ".ygor_cloned").touch()
    result = materialize_git_checkout(url="file:///does-not-exist", ref="main", target=target)
    assert result == target


def test_materialize_git_checkout_cleans_tmp_on_failure(tmp_path: Path):
    target = tmp_path / "cache" / "entry"
    with pytest.raises(GitCacheError, match="git failed"):
        materialize_git_checkout(url="file:///does-not-exist", ref="main", target=target)
    assert not (target.parent / f".tmp_{target.name}").exists()
