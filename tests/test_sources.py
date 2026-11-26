"""Tests for app source resolution (src/ygor/sources.py)."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from pydantic import ValidationError

from ygor.schema import AppConfig, AppSource, AptConfig, PythonConfig
from ygor.sources import (
    AppSourceError,
    _resolve_local_source,
    _source_git_target,
    ensure_app_sources,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_local_git_repo(tmp_path: Path) -> tuple[Path, str]:
    """Create a local git repo with one commit; return (repo_path, commit_hash)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    env = {"GIT_CONFIG_NOSYSTEM": "1", "HOME": str(tmp_path)}
    for cmd in [
        ["git", "init", str(repo)],
        ["git", "-C", str(repo), "config", "user.email", "t@t.com"],
        ["git", "-C", str(repo), "config", "user.name", "T"],
    ]:
        subprocess.run(cmd, check=True, capture_output=True, env=env)
    (repo / "src.c").write_text("int main() { return 0; }")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True, capture_output=True, env=env)
    subprocess.run(
        ["git", "-C", str(repo), "commit", "-m", "init"],
        check=True, capture_output=True, env=env,
    )
    result = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        check=True, capture_output=True, text=True, env=env,
    )
    return repo, result.stdout.strip()


def _git_config(url: str, ref: str, binary: str | None = None) -> AppConfig:
    data: dict = {"source": {"type": "git", "url": url, "ref": ref}}
    if binary:
        data["binary"] = binary
    return AppConfig.model_validate(data)


# ---------------------------------------------------------------------------
# Local source
# ---------------------------------------------------------------------------

class TestResolveLocalSource:
    def test_existing_path(self, tmp_path):
        src = AppSource.model_validate({"type": "local", "path": str(tmp_path)})
        result = _resolve_local_source("myapp", src)
        assert result == tmp_path.resolve()

    def test_missing_path_raises(self, tmp_path):
        src = AppSource.model_validate({"type": "local", "path": str(tmp_path / "noexist")})
        with pytest.raises(AppSourceError, match="local source path not found"):
            _resolve_local_source("myapp", src)

    def test_missing_path_field_raises(self):
        src = AppSource.model_validate({"type": "local"})
        with pytest.raises(AppSourceError, match="requires 'path'"):
            _resolve_local_source("myapp", src)


# ---------------------------------------------------------------------------
# Git source
# ---------------------------------------------------------------------------

class TestResolveGitSource:
    def test_clone_and_checkout(self, tmp_path):
        repo, commit = _make_local_git_repo(tmp_path)
        result = ensure_app_sources(
            {"myapp": _git_config(f"file://{repo}", commit)},
            tmp_path / "store",
        )
        cloned = Path(result["myapp"])
        assert (cloned / "src.c").exists()
        assert (cloned / ".ygor_cloned").exists()

    def test_skips_if_sentinel_exists(self, tmp_path):
        store = tmp_path / "store"
        src = AppSource.model_validate({"type": "git", "url": "file:///fake", "ref": "abc123"})
        target = _source_git_target(src, store)
        target.mkdir(parents=True)
        (target / ".ygor_cloned").touch()

        result = ensure_app_sources(
            {"myapp": _git_config("file:///fake", "abc123")},
            store,
        )
        assert Path(result["myapp"]) == target

    def test_invalid_ref_raises(self, tmp_path):
        repo, _ = _make_local_git_repo(tmp_path)
        with pytest.raises(AppSourceError, match="git failed"):
            ensure_app_sources(
                {"myapp": _git_config(f"file://{repo}", "nonexistent-branch-xyz")},
                tmp_path / "store",
            )

    def test_invalid_url_raises(self, tmp_path):
        with pytest.raises(AppSourceError, match="git failed"):
            ensure_app_sources(
                {"myapp": _git_config("file:///nonexistent/repo", "main")},
                tmp_path / "store",
            )

    def test_build_commands_executed(self, tmp_path):
        repo, commit = _make_local_git_repo(tmp_path)
        config = AppConfig.model_validate({
            "source": {"type": "git", "url": f"file://{repo}", "ref": commit},
            "build": ["touch built_marker.txt"],
        })
        result = ensure_app_sources({"myapp": config}, tmp_path / "store")
        assert (Path(result["myapp"]) / "built_marker.txt").exists()

    def test_build_commands_run_in_order(self, tmp_path):
        repo, commit = _make_local_git_repo(tmp_path)
        config = AppConfig.model_validate({
            "source": {"type": "git", "url": f"file://{repo}", "ref": commit},
            "build": [
                "echo step1 > order.txt",
                "echo step2 >> order.txt",
            ],
        })
        result = ensure_app_sources({"myapp": config}, tmp_path / "store")
        content = (Path(result["myapp"]) / "order.txt").read_text()
        assert content.splitlines() == ["step1", "step2"]

    def test_failing_build_raises(self, tmp_path):
        repo, commit = _make_local_git_repo(tmp_path)
        config = AppConfig.model_validate({
            "source": {"type": "git", "url": f"file://{repo}", "ref": commit},
            "build": ["exit 1"],
        })
        with pytest.raises(AppSourceError, match="build failed"):
            ensure_app_sources({"myapp": config}, tmp_path / "store")

    def test_failing_build_cleans_up_tmp_dir(self, tmp_path):
        repo, commit = _make_local_git_repo(tmp_path)
        store = tmp_path / "store"
        config = AppConfig.model_validate({
            "source": {"type": "git", "url": f"file://{repo}", "ref": commit},
            "build": ["exit 1"],
        })
        with pytest.raises(AppSourceError):
            ensure_app_sources({"myapp": config}, store)
        tmp_files = list((store / "sources").glob(".tmp_*"))
        assert tmp_files == [], "tmp directory should be cleaned up after build failure"

    def test_build_skipped_if_sentinel_exists(self, tmp_path):
        store = tmp_path / "store"
        src = AppSource.model_validate({"type": "git", "url": "file:///fake", "ref": "abc123"})
        target = _source_git_target(src, store)
        target.mkdir(parents=True)
        (target / ".ygor_cloned").touch()

        config = AppConfig.model_validate({
            "source": {"type": "git", "url": "file:///fake", "ref": "abc123"},
            "build": ["touch should_not_exist.txt"],
        })
        ensure_app_sources({"myapp": config}, store)
        assert not (target / "should_not_exist.txt").exists()

    def test_binary_path_appended(self, tmp_path):
        repo, commit = _make_local_git_repo(tmp_path)
        result = ensure_app_sources(
            {"myapp": _git_config(f"file://{repo}", commit, binary="build/bin/solver")},
            tmp_path / "store",
        )
        assert str(result["myapp"]).endswith("build/bin/solver")


# ---------------------------------------------------------------------------
# ensure_app_sources — filtering and integration
# ---------------------------------------------------------------------------

class TestEnsureAppSources:
    def test_empty_apps(self, tmp_path):
        assert ensure_app_sources({}, tmp_path) == {}

    def test_ignores_apt_config(self, tmp_path):
        apt = AptConfig.model_validate({"packages": ["build-essential"]})
        assert ensure_app_sources({"apt": apt}, tmp_path) == {}

    def test_ignores_python_config(self, tmp_path):
        py = PythonConfig.model_validate({"requirements": "requirements.txt"})
        assert ensure_app_sources({"python": py}, tmp_path) == {}

    def test_ignores_app_config_without_source(self, tmp_path):
        app = AppConfig.model_validate({"build": ["make"]})
        assert ensure_app_sources({"myapp": app}, tmp_path) == {}

    def test_mixed_apps_only_resolves_with_source(self, tmp_path):
        repo, commit = _make_local_git_repo(tmp_path)
        apps = {
            "apt": AptConfig.model_validate({"packages": ["gcc"]}),
            "python": PythonConfig.model_validate({"requirements": "req.txt"}),
            "solver": _git_config(f"file://{repo}", commit),
        }
        result = ensure_app_sources(apps, tmp_path / "store")
        assert set(result.keys()) == {"solver"}

    def test_on_status_callback(self, tmp_path):
        repo, commit = _make_local_git_repo(tmp_path)
        statuses: list[tuple[str, str]] = []
        ensure_app_sources(
            {"myapp": _git_config(f"file://{repo}", commit)},
            tmp_path / "store",
            on_status=lambda n, m: statuses.append((n, m)),
        )
        assert any(n == "myapp" for n, _ in statuses)

    def test_unsupported_source_type_raises(self, tmp_path):
        with pytest.raises(ValidationError):
            AppConfig.model_validate({"source": {"type": "s3", "url": "s3://bucket/key"}})

    def test_local_source(self, tmp_path):
        src_dir = tmp_path / "myapp"
        src_dir.mkdir()
        config = AppConfig.model_validate({"source": {"type": "local", "path": str(src_dir)}})
        result = ensure_app_sources({"myapp": config}, tmp_path / "store")
        assert Path(result["myapp"]) == src_dir.resolve()


# ---------------------------------------------------------------------------
# ygor run — app source injected into command context
# ---------------------------------------------------------------------------

class TestYgorRunWithAppSources:
    def test_relative_store_produces_absolute_path(self, tmp_path, monkeypatch):
        """Relative --store must still produce absolute app paths inside commands."""
        from typer.testing import CliRunner
        from ygor.cli import app

        monkeypatch.chdir(tmp_path)

        src_dir = tmp_path / "mysolver"
        src_dir.mkdir()

        out_file = tmp_path / "result.txt"
        recipe_content = (
            f"environment:\n"
            f"  apps:\n"
            f"    mysolver:\n"
            f"      source:\n"
            f"        type: local\n"
            f"        path: '{src_dir}'\n"
            f"command: \"echo {{{{ app['mysolver'] }}}} > {out_file}\"\n"
        )
        (tmp_path / "recipe.yaml").write_text(recipe_content)

        runner = CliRunner()
        result = runner.invoke(
            app, ["run", str(tmp_path / "recipe.yaml"), "--store", "store", "--yes"]
        )
        assert result.exit_code == 0, result.output
        written = out_file.read_text().strip()
        from pathlib import Path as _Path
        assert _Path(written).is_absolute(), f"expected absolute path, got: {written!r}"

    def test_app_source_path_in_command(self, tmp_path):
        from typer.testing import CliRunner
        from ygor.cli import app

        src_dir = tmp_path / "mysolver"
        src_dir.mkdir()

        out_file = tmp_path / "result.txt"
        recipe_content = (
            f"environment:\n"
            f"  apps:\n"
            f"    mysolver:\n"
            f"      source:\n"
            f"        type: local\n"
            f"        path: '{src_dir}'\n"
            f"command: \"echo {{{{ app['mysolver'] }}}} > {out_file}\"\n"
        )
        recipe_file = tmp_path / "recipe.yaml"
        recipe_file.write_text(recipe_content)

        runner = CliRunner()
        result = runner.invoke(
            app,
            ["run", str(recipe_file), "--store", str(tmp_path / "store"), "--yes"],
        )
        assert result.exit_code == 0, result.output
        assert str(src_dir) in out_file.read_text()
