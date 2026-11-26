"""Tests for `ygor latest` command."""

from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

import ygor.cli as cli_module
from ygor.cli import app


def _write_manifest(
    store: Path,
    run_hash: str,
    status: str,
    started_at: str,
    *,
    name: str | None = None,
    recipe_path: str | None = None,
) -> None:
    run_dir = store / "runs" / run_hash
    run_dir.mkdir(parents=True, exist_ok=True)
    m = {
        "manifest_version": 1,
        "run_hash": run_hash,
        "recipe_path": recipe_path or f"recipes/{name or 'test'}/recipe.yaml",
        "recipe": {"meta": {"name": name} if name else None, "command": ["echo x"]},
        "state": {
            "status": status,
            "started_at": started_at,
            "ended_at": started_at,
            "last_updated": started_at,
            "total_executions": 1,
            "counts": {"completed": 1 if status == "completed" else 0, "failed": 0, "running": 0, "pending": 0},
        },
        "executions": [],
        "post": [],
    }
    (run_dir / "manifest.json").write_text(json.dumps(m))


class TestLatestCommand:
    def test_returns_hash_of_most_recent_completed(self, tmp_path):
        store = tmp_path / "store"
        _write_manifest(store, "a" * 64, "completed", "2026-01-01T10:00:00+00:00", name="popsize_sweep")
        _write_manifest(store, "b" * 64, "completed", "2026-03-01T10:00:00+00:00", name="popsize_sweep")
        runner = CliRunner()
        result = runner.invoke(app, ["latest", "popsize_sweep", "--store", str(store)])
        assert result.exit_code == 0
        assert result.output.strip() == "b" * 8

    def test_ignores_running_without_any_status(self, tmp_path):
        store = tmp_path / "store"
        _write_manifest(store, "a" * 64, "running",   "2026-03-01T10:00:00+00:00", name="popsize_sweep")
        _write_manifest(store, "b" * 64, "completed", "2026-01-01T10:00:00+00:00", name="popsize_sweep")
        runner = CliRunner()
        result = runner.invoke(app, ["latest", "popsize_sweep", "--store", str(store)])
        assert result.exit_code == 0
        assert result.output.strip() == "b" * 8

    def test_any_status_returns_most_recent_regardless(self, tmp_path):
        store = tmp_path / "store"
        _write_manifest(store, "a" * 64, "running",   "2026-03-01T10:00:00+00:00", name="popsize_sweep")
        _write_manifest(store, "b" * 64, "completed", "2026-01-01T10:00:00+00:00", name="popsize_sweep")
        runner = CliRunner()
        result = runner.invoke(app, ["latest", "popsize_sweep", "--store", str(store), "--any-status"])
        assert result.exit_code == 0
        assert result.output.strip() == "a" * 8

    def test_no_completed_run_exits_1(self, tmp_path):
        store = tmp_path / "store"
        _write_manifest(store, "a" * 64, "running", "2026-01-01T10:00:00+00:00", name="popsize_sweep")
        runner = CliRunner()
        result = runner.invoke(app, ["latest", "popsize_sweep", "--store", str(store)])
        assert result.exit_code == 1

    def test_no_run_for_recipe_exits_1(self, tmp_path):
        store = tmp_path / "store"
        _write_manifest(store, "a" * 64, "completed", "2026-01-01T10:00:00+00:00", name="other_exp")
        runner = CliRunner()
        result = runner.invoke(app, ["latest", "popsize_sweep", "--store", str(store)])
        assert result.exit_code == 1

    def test_matches_recipe_file_name(self, tmp_path):
        store = tmp_path / "store"
        _write_manifest(
            store,
            "a" * 64,
            "completed",
            "2026-01-01T10:00:00+00:00",
            recipe_path="naive.yaml",
        )
        runner = CliRunner()
        result = runner.invoke(app, ["latest", "naive.yaml", "--store", str(store)])
        assert result.exit_code == 0
        assert result.output.strip() == "a" * 8

    def test_empty_store_exits_1(self, tmp_path):
        runner = CliRunner()
        result = runner.invoke(app, ["latest", "popsize_sweep", "--store", str(tmp_path / "store")])
        assert result.exit_code == 1

    def test_ignores_other_recipes(self, tmp_path):
        store = tmp_path / "store"
        _write_manifest(store, "a" * 64, "completed", "2026-01-01T10:00:00+00:00", name="popsize_sweep")
        _write_manifest(store, "b" * 64, "completed", "2026-03-01T10:00:00+00:00", name="heuristic_compare")
        runner = CliRunner()
        result = runner.invoke(app, ["latest", "popsize_sweep", "--store", str(store)])
        assert result.exit_code == 0
        assert result.output.strip() == "a" * 8

    def test_output_is_exactly_8_chars(self, tmp_path):
        store = tmp_path / "store"
        _write_manifest(store, "abcdef01" * 8, "completed", "2026-01-01T10:00:00+00:00", name="my_exp")
        runner = CliRunner()
        result = runner.invoke(app, ["latest", "my_exp", "--store", str(store)])
        assert result.exit_code == 0
        assert result.output.strip() == "abcdef01"


class TestOpenCommand:
    def test_open_accepts_run_hash_prefix(self, tmp_path, monkeypatch):
        store = tmp_path / "store"
        run_hash = "a" * 64
        _write_manifest(store, run_hash, "completed", "2026-01-01T10:00:00+00:00", name="popsize_sweep")
        expected = store / "runs" / run_hash
        opened: list[Path] = []
        monkeypatch.setattr(cli_module, "_open_directory", lambda path: opened.append(path))

        runner = CliRunner()
        result = runner.invoke(app, ["open", "aaaaaaaa", "--store", str(store)])

        assert result.exit_code == 0
        assert opened == [expected]

    def test_open_without_selector_uses_latest_run(self, tmp_path, monkeypatch):
        store = tmp_path / "store"
        old_hash = "a" * 64
        new_hash = "b" * 64
        _write_manifest(store, old_hash, "completed", "2026-01-01T10:00:00+00:00", name="old")
        _write_manifest(store, new_hash, "completed", "2026-03-01T10:00:00+00:00", name="new")
        expected = store / "runs" / new_hash
        opened: list[Path] = []
        monkeypatch.setattr(cli_module, "_open_directory", lambda path: opened.append(path))

        runner = CliRunner()
        result = runner.invoke(app, ["open", "--store", str(store)])

        assert result.exit_code == 0
        assert opened == [expected]

    def test_opens_directory_of_most_recent_run(self, tmp_path, monkeypatch):
        store = tmp_path / "store"
        old_hash = "a" * 64
        new_hash = "b" * 64
        _write_manifest(store, old_hash, "running", "2026-01-01T10:00:00+00:00", name="popsize_sweep")
        _write_manifest(store, new_hash, "completed", "2026-03-01T10:00:00+00:00", name="popsize_sweep")
        expected = store / "runs" / new_hash
        opened: list[Path] = []
        monkeypatch.setattr(cli_module, "_open_directory", lambda path: opened.append(path))

        runner = CliRunner()
        result = runner.invoke(app, ["open", "popsize_sweep", "--store", str(store)])

        assert result.exit_code == 0
        assert opened == [expected]

    def test_open_selects_latest_run_by_recipe_option(self, tmp_path, monkeypatch):
        store = tmp_path / "store"
        target_hash = "a" * 64
        other_hash = "b" * 64
        _write_manifest(store, target_hash, "completed", "2026-01-01T10:00:00+00:00", name="target")
        _write_manifest(store, other_hash, "completed", "2026-03-01T10:00:00+00:00", name="other")
        expected = store / "runs" / target_hash
        opened: list[Path] = []
        monkeypatch.setattr(cli_module, "_open_directory", lambda path: opened.append(path))

        runner = CliRunner()
        result = runner.invoke(app, ["open", "--recipe", "target", "--store", str(store)])

        assert result.exit_code == 0
        assert opened == [expected]
        assert result.output.strip() == str(expected)

    def test_open_uses_running_when_it_is_most_recent(self, tmp_path, monkeypatch):
        store = tmp_path / "store"
        running_hash = "a" * 64
        completed_hash = "b" * 64
        _write_manifest(store, running_hash, "running", "2026-03-01T10:00:00+00:00", name="popsize_sweep")
        _write_manifest(store, completed_hash, "completed", "2026-01-01T10:00:00+00:00", name="popsize_sweep")
        expected = store / "runs" / running_hash
        opened: list[Path] = []
        monkeypatch.setattr(cli_module, "_open_directory", lambda path: opened.append(path))

        runner = CliRunner()
        result = runner.invoke(app, ["open", "popsize_sweep", "--store", str(store)])

        assert result.exit_code == 0
        assert opened == [expected]

    def test_open_accepts_recipe_file_name(self, tmp_path, monkeypatch):
        store = tmp_path / "store"
        run_hash = "a" * 64
        _write_manifest(
            store,
            run_hash,
            "completed",
            "2026-01-01T10:00:00+00:00",
            recipe_path="naive.yaml",
        )
        expected = store / "runs" / run_hash
        opened: list[Path] = []
        monkeypatch.setattr(cli_module, "_open_directory", lambda path: opened.append(path))

        runner = CliRunner()
        result = runner.invoke(app, ["open", "naive.yaml", "--store", str(store)])

        assert result.exit_code == 0
        assert opened == [expected]
