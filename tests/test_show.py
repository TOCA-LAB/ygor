"""Tests for `ygor show` command."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from ygor.cli import app


def _write_manifest(
    store: Path,
    run_hash: str,
    *,
    status: str = "completed",
    started_at: str = "2026-01-01T10:00:00+00:00",
    ended_at: str | None = "2026-01-01T12:30:00+00:00",
    name: str | None = "my_exp",
    total: int = 10,
    completed: int = 10,
    failed: int = 0,
    commands: list[str] | None = None,
    post: list[dict] | None = None,
    parameters: dict | None = None,
    seeds: dict | None = None,
    plan: dict | None = None,
    host: dict | None = None,
    recipe_lock: str | None = None,
) -> None:
    run_dir = store / "runs" / run_hash
    run_dir.mkdir(parents=True, exist_ok=True)
    m = {
        "manifest_version": 1,
        "run_hash": run_hash,
        "recipe_path": f"recipes/{name or 'test'}/recipe.yaml",
        "recipe": {
            "meta": {"name": name} if name else None,
            "command": commands or ["./solver --seed {{ run.seed }}"],
            "parameters": parameters,
            "post": [p["command"] for p in (post or [])] if post else [],
        },
        "seeds": seeds,
        "host": host or {"hostname": "testhost", "os": "Linux", "cpu_count_logical": 8},
        "state": {
            "status": status,
            "started_at": started_at,
            "ended_at": ended_at,
            "last_updated": started_at,
            "total_executions": total,
            "counts": {
                "completed": completed,
                "failed": failed,
                "running": 0,
                "pending": max(0, total - completed - failed),
            },
        },
        "plan": plan,
        "executions": [],
        "post": post or [],
    }
    (run_dir / "manifest.json").write_text(json.dumps(m))
    if recipe_lock is not None:
        (run_dir / "recipe.lock.yaml").write_text(recipe_lock)


class TestShowCommand:
    def test_shows_hash_and_name(self, tmp_path):
        store = tmp_path / "store"
        _write_manifest(store, "abcdef01" * 8, name="popsize_sweep")
        runner = CliRunner()
        result = runner.invoke(app, ["show", "abcdef01", "--store", str(store)])
        assert result.exit_code == 0
        assert "abcdef01" in result.output
        assert "popsize_sweep" in result.output

    def test_shows_status(self, tmp_path):
        store = tmp_path / "store"
        _write_manifest(store, "abcdef01" * 8, status="completed")
        runner = CliRunner()
        result = runner.invoke(app, ["show", "abcdef01", "--store", str(store)])
        assert result.exit_code == 0
        assert "completed" in result.output

    def test_shows_recipe_path(self, tmp_path):
        store = tmp_path / "store"
        _write_manifest(store, "abcdef01" * 8, name="my_exp")
        runner = CliRunner()
        result = runner.invoke(app, ["show", "abcdef01", "--store", str(store)])
        assert "recipes/my_exp/recipe.yaml" in result.output

    def test_shows_started_and_ended(self, tmp_path):
        store = tmp_path / "store"
        _write_manifest(
            store, "abcdef01" * 8,
            started_at="2026-06-01T09:00:00+00:00",
            ended_at="2026-06-01T11:30:00+00:00",
        )
        runner = CliRunner()
        result = runner.invoke(app, ["show", "abcdef01", "--store", str(store)])
        assert "2026-06-01 09:00" in result.output
        assert "2026-06-01 11:30" in result.output

    def test_shows_duration(self, tmp_path):
        store = tmp_path / "store"
        _write_manifest(
            store, "abcdef01" * 8,
            started_at="2026-06-01T09:00:00+00:00",
            ended_at="2026-06-01T11:30:00+00:00",
        )
        runner = CliRunner()
        result = runner.invoke(app, ["show", "abcdef01", "--store", str(store)])
        assert "2h 30m" in result.output

    def test_shows_execution_counts(self, tmp_path):
        store = tmp_path / "store"
        _write_manifest(store, "abcdef01" * 8, total=100, completed=87, failed=13)
        runner = CliRunner()
        result = runner.invoke(app, ["show", "abcdef01", "--store", str(store)])
        assert "100" in result.output
        assert "87" in result.output
        assert "13" in result.output

    def test_shows_paths(self, tmp_path):
        store = tmp_path / "store"
        _write_manifest(store, "abcdef01" * 8)
        runner = CliRunner()
        result = runner.invoke(app, ["show", "abcdef01", "--store", str(store)])
        assert "outputs" in result.output
        assert "logs" in result.output

    def test_shows_commands(self, tmp_path):
        store = tmp_path / "store"
        _write_manifest(store, "abcdef01" * 8, commands=["./solver --x {{ param.x }}", "./parse.py"])
        runner = CliRunner()
        result = runner.invoke(app, ["show", "abcdef01", "--store", str(store)])
        assert "./solver" in result.output
        assert "./parse.py" in result.output

    def test_shows_post_with_status(self, tmp_path):
        store = tmp_path / "store"
        _write_manifest(
            store, "abcdef01" * 8,
            post=[
                {"command": "./aggregate.py", "status": "completed", "exit_code": 0},
                {"command": "./plot.py",      "status": "failed",    "exit_code": 1},
            ],
        )
        runner = CliRunner()
        result = runner.invoke(app, ["show", "abcdef01", "--store", str(store)])
        assert "./aggregate.py" in result.output
        assert "./plot.py" in result.output
        assert "completed" in result.output
        assert "failed" in result.output

    def test_shows_parameters(self, tmp_path):
        store = tmp_path / "store"
        _write_manifest(
            store, "abcdef01" * 8,
            parameters={
                "strategy": "cartesian",
                "values": {
                    "popsize": [100, 200, 300],
                    "mutation": {"from": 0.1, "to": 0.5, "step": 0.1},
                },
            },
        )
        runner = CliRunner()
        result = runner.invoke(app, ["show", "abcdef01", "--store", str(store)])
        assert "popsize" in result.output
        assert "mutation" in result.output
        assert "0.1" in result.output

    def test_shows_input_patterns(self, tmp_path):
        store = tmp_path / "store"
        _write_manifest(
            store, "abcdef01" * 8,
            parameters={
                "instance": ["dataset:benchmarks/*.mtx", "local/*.json"],
                "values": {"x": [1]},
            },
        )
        runner = CliRunner()
        result = runner.invoke(app, ["show", "abcdef01", "--store", str(store)])
        assert result.exit_code == 0
        assert "Input Patterns" in result.output
        assert "dataset:benchmarks/*.mtx" in result.output
        assert "local/*.json" in result.output

    def test_shows_seeds(self, tmp_path):
        store = tmp_path / "store"
        _write_manifest(
            store, "abcdef01" * 8,
            seeds={"meta_seed": 42, "count": 30, "algorithm": "uniform_int32_via_numpy", "values": []},
        )
        runner = CliRunner()
        result = runner.invoke(app, ["show", "abcdef01", "--store", str(store)])
        assert "42" in result.output
        assert "30" in result.output
        assert "uniform_int32_via_numpy" in result.output

    def test_long_shows_all_used_seeds_and_concrete_instances(self, tmp_path):
        store = tmp_path / "store"
        _write_manifest(
            store, "abcdef01" * 8,
            parameters={"instance": ["dataset:benchmarks/*.mtx"]},
            seeds={"meta_seed": 42, "count": 2, "algorithm": "uniform_int32_via_numpy", "values": [111, 222]},
            plan={
                "executions": [
                    {"exec_id": 1, "params": {"instance": "/data/a.mtx"}, "seed": 111},
                    {"exec_id": 2, "params": {"instance": "/data/a.mtx"}, "seed": 222},
                    {"exec_id": 3, "params": {"instance": "/data/b.mtx"}, "seed": 111},
                ],
            },
        )
        runner = CliRunner()
        result = runner.invoke(app, ["show", "--long", "abcdef01", "--store", str(store)])
        assert result.exit_code == 0
        assert "Seeds Used" in result.output
        assert "111" in result.output
        assert "222" in result.output
        assert "Concrete Instances" in result.output
        assert "/data/a.mtx" in result.output
        assert "/data/b.mtx" in result.output

    def test_show_without_long_hides_concrete_instances(self, tmp_path):
        store = tmp_path / "store"
        _write_manifest(
            store, "abcdef01" * 8,
            parameters={"instance": ["dataset:benchmarks/*.mtx"]},
            plan={
                "executions": [
                    {"exec_id": 1, "params": {"instance": "/data/a.mtx"}, "seed": 111},
                ],
            },
        )
        runner = CliRunner()
        result = runner.invoke(app, ["show", "abcdef01", "--store", str(store)])
        assert result.exit_code == 0
        assert "dataset:benchmarks/*.mtx" in result.output
        assert "Concrete Instances" not in result.output
        assert "/data/a.mtx" not in result.output

    def test_shows_host(self, tmp_path):
        store = tmp_path / "store"
        _write_manifest(
            store, "abcdef01" * 8,
            host={"hostname": "ygor-server-03", "os": "Ubuntu 24.04", "cpu_count_logical": 64},
        )
        runner = CliRunner()
        result = runner.invoke(app, ["show", "abcdef01", "--store", str(store)])
        assert "ygor-server-03" in result.output
        assert "64" in result.output

    def test_json_outputs_raw_manifest(self, tmp_path):
        store = tmp_path / "store"
        _write_manifest(store, "abcdef01" * 8, name="my_exp", total=5)
        runner = CliRunner()
        result = runner.invoke(app, ["show", "abcdef01", "--store", str(store), "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["run_hash"] == "abcdef01" * 8
        assert data["state"]["total_executions"] == 5

    def test_recipe_prints_frozen_recipe_lock(self, tmp_path):
        store = tmp_path / "store"
        recipe_text = (
            "meta:\n"
            "  name: locked\n"
            "command: \"./solver --x {{ param.x }}\"\n"
        )
        _write_manifest(store, "abcdef01" * 8, recipe_lock=recipe_text)
        runner = CliRunner()
        result = runner.invoke(app, ["show", "--recipe-lock", "abcdef01", "--store", str(store)])
        assert result.exit_code == 0
        assert result.output.strip() == recipe_text.strip()

    def test_recipe_falls_back_to_manifest_snapshot(self, tmp_path):
        store = tmp_path / "store"
        _write_manifest(
            store, "abcdef01" * 8,
            commands=["./solver --seed {{ run.seed }}"],
            parameters={"values": {"x": [1, 2]}},
        )
        runner = CliRunner()
        result = runner.invoke(app, ["show", "--lock", "abcdef01", "--store", str(store)])
        assert result.exit_code == 0
        assert "command:" in result.output
        assert "./solver --seed {{ run.seed }}" in result.output
        assert "parameters:" in result.output

    def test_prefix_matching_finds_run(self, tmp_path):
        store = tmp_path / "store"
        _write_manifest(store, "abcdef01" * 8)
        runner = CliRunner()
        result = runner.invoke(app, ["show", "abcd", "--store", str(store)])
        assert result.exit_code == 0
        assert "abcdef01" in result.output

    def test_ambiguous_prefix_exits_1(self, tmp_path):
        store = tmp_path / "store"
        _write_manifest(store, "aaaa0001" * 8)
        _write_manifest(store, "aaaa0002" * 8)
        runner = CliRunner()
        result = runner.invoke(app, ["show", "aaaa", "--store", str(store)])
        assert result.exit_code == 1

    def test_not_found_exits_1(self, tmp_path):
        store = tmp_path / "store"
        runner = CliRunner()
        result = runner.invoke(app, ["show", "deadbeef", "--store", str(tmp_path / "store")])
        assert result.exit_code == 1

    def test_full_hash_matches(self, tmp_path):
        store = tmp_path / "store"
        full = "abcdef01" * 8
        _write_manifest(store, full)
        runner = CliRunner()
        result = runner.invoke(app, ["show", full, "--store", str(store)])
        assert result.exit_code == 0

    def test_show_without_selector_uses_latest_run(self, tmp_path):
        store = tmp_path / "store"
        _write_manifest(store, "a" * 64, started_at="2026-01-01T10:00:00+00:00", name="old")
        _write_manifest(store, "b" * 64, started_at="2026-03-01T10:00:00+00:00", name="new")
        runner = CliRunner()
        result = runner.invoke(app, ["show", "--store", str(store)])
        assert result.exit_code == 0
        assert "bbbbbbbb" in result.output
        assert "new" in result.output

    def test_show_selects_latest_run_by_recipe(self, tmp_path):
        store = tmp_path / "store"
        _write_manifest(store, "a" * 64, started_at="2026-01-01T10:00:00+00:00", name="target")
        _write_manifest(store, "b" * 64, started_at="2026-03-01T10:00:00+00:00", name="other")
        runner = CliRunner()
        result = runner.invoke(app, ["show", "--recipe", "target", "--store", str(store)])
        assert result.exit_code == 0
        assert "aaaaaaaa" in result.output
        assert "target" in result.output

    def test_show_selects_latest_run_by_status(self, tmp_path):
        store = tmp_path / "store"
        _write_manifest(store, "a" * 64, status="failed", started_at="2026-01-01T10:00:00+00:00", name="failed")
        _write_manifest(store, "b" * 64, status="completed", started_at="2026-03-01T10:00:00+00:00", name="done")
        runner = CliRunner()
        result = runner.invoke(app, ["show", "--status", "failed", "--store", str(store)])
        assert result.exit_code == 0
        assert "aaaaaaaa" in result.output
        assert "failed" in result.output
