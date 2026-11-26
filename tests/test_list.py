"""Tests for `ygor list` command."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from ygor.cli import app


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _write_manifest(
    store: Path,
    run_hash: str,
    status: str,
    started_at: str,
    *,
    name: str | None = None,
    total: int = 1,
    completed: int | None = None,
    failed: int = 0,
    ended_at: str | None = None,
) -> None:
    run_dir = store / "runs" / run_hash
    run_dir.mkdir(parents=True, exist_ok=True)
    if completed is None:
        completed = total if status == "completed" else 0
    m = {
        "manifest_version": 1,
        "run_hash": run_hash,
        "recipe_path": f"recipes/{name or 'test'}/recipe.yaml",
        "recipe": {"meta": {"name": name} if name else None, "command": ["echo x"]},
        "state": {
            "status": status,
            "started_at": started_at,
            "ended_at": ended_at or (started_at if status in ("completed", "failed") else None),
            "last_updated": started_at,
            "total_executions": total,
            "counts": {
                "completed": completed,
                "failed": failed,
                "running": 0,
                "pending": max(0, total - completed - failed),
            },
        },
        "executions": [],
        "post": [],
    }
    (run_dir / "manifest.json").write_text(json.dumps(m))


def _write_publications(store: Path, *run_hashes: str) -> None:
    store.mkdir(parents=True, exist_ok=True)
    (store / "publications.json").write_text(
        json.dumps(
            {
                "version": 1,
                "runs": {
                    run_hash: {"published": True}
                    for run_hash in run_hashes
                },
            }
        )
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestListCommand:
    def test_no_store_dir(self, tmp_path):
        runner = CliRunner()
        result = runner.invoke(app, ["list", "--store", str(tmp_path / "nonexistent")])
        assert result.exit_code == 0
        assert "No runs found" in result.output

    def test_empty_runs_dir(self, tmp_path):
        store = tmp_path / "store"
        (store / "runs").mkdir(parents=True)
        runner = CliRunner()
        result = runner.invoke(app, ["list", "--store", str(store)])
        assert result.exit_code == 0
        assert "No runs found" in result.output

    def test_single_completed_run_shown(self, tmp_path):
        store = tmp_path / "store"
        _write_manifest(store, "a" * 64, "completed", "2026-01-01T10:00:00+00:00", name="my-exp")
        runner = CliRunner()
        result = runner.invoke(app, ["list", "--store", str(store)])
        assert result.exit_code == 0
        assert "aaaaaaaa" in result.output
        assert "my-exp" in result.output
        assert "completed" in result.output

    def test_run_without_meta_name_uses_recipe_stem(self, tmp_path):
        store = tmp_path / "store"
        _write_manifest(store, "b" * 64, "completed", "2026-01-01T10:00:00+00:00")
        runner = CliRunner()
        result = runner.invoke(app, ["list", "--store", str(store)])
        assert result.exit_code == 0
        assert "test" in result.output  # stem of "recipes/test/recipe.yaml"

    def test_status_filter_shows_only_matching(self, tmp_path):
        store = tmp_path / "store"
        _write_manifest(store, "a" * 64, "completed", "2026-01-01T10:00:00+00:00")
        _write_manifest(store, "b" * 64, "failed", "2026-01-02T10:00:00+00:00")
        runner = CliRunner()
        result = runner.invoke(app, ["list", "--store", str(store), "--status", "completed"])
        assert result.exit_code == 0
        assert "aaaaaaaa" in result.output
        assert "bbbbbbbb" not in result.output

    def test_status_filter_no_match_shows_not_found(self, tmp_path):
        store = tmp_path / "store"
        _write_manifest(store, "a" * 64, "completed", "2026-01-01T10:00:00+00:00")
        runner = CliRunner()
        result = runner.invoke(app, ["list", "--store", str(store), "--status", "running"])
        assert result.exit_code == 0
        assert "No runs found" in result.output

    def test_json_output_structure(self, tmp_path):
        store = tmp_path / "store"
        _write_manifest(store, "a" * 64, "completed", "2026-01-01T10:00:00+00:00", name="exp1", total=5)
        runner = CliRunner()
        result = runner.invoke(app, ["list", "--store", str(store), "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert isinstance(data, list)
        assert len(data) == 1
        r = data[0]
        assert r["hash_short"] == "aaaaaaaa"
        assert r["recipe_name"] == "exp1"
        assert r["status"] == "completed"
        assert r["total_executions"] == 5
        assert r["published"] is False

    def test_json_marks_published_runs(self, tmp_path):
        store = tmp_path / "store"
        run_hash = "a" * 64
        _write_manifest(store, run_hash, "completed", "2026-01-01T10:00:00+00:00")
        _write_publications(store, run_hash)
        runner = CliRunner()
        result = runner.invoke(app, ["list", "--store", str(store), "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data[0]["published"] is True

    def test_table_marks_published_runs(self, tmp_path):
        store = tmp_path / "store"
        run_hash = "a" * 64
        _write_manifest(store, run_hash, "completed", "2026-01-01T10:00:00+00:00")
        _write_publications(store, run_hash)
        runner = CliRunner()
        result = runner.invoke(app, ["list", "--store", str(store)])
        assert result.exit_code == 0
        assert "PUB" in result.output
        line = [l for l in result.output.splitlines() if "aaaaaaaa" in l][0]
        assert " yes " in line

    def test_json_status_filter(self, tmp_path):
        store = tmp_path / "store"
        _write_manifest(store, "a" * 64, "completed", "2026-01-01T10:00:00+00:00")
        _write_manifest(store, "b" * 64, "failed", "2026-01-02T10:00:00+00:00")
        runner = CliRunner()
        result = runner.invoke(app, ["list", "--store", str(store), "--json", "--status", "failed"])
        data = json.loads(result.output)
        assert len(data) == 1
        assert data[0]["hash_short"] == "bbbbbbbb"

    def test_sorted_most_recent_first(self, tmp_path):
        store = tmp_path / "store"
        _write_manifest(store, "a" * 64, "completed", "2026-01-01T10:00:00+00:00")
        _write_manifest(store, "b" * 64, "completed", "2026-01-03T10:00:00+00:00")
        _write_manifest(store, "c" * 64, "completed", "2026-01-02T10:00:00+00:00")
        runner = CliRunner()
        result = runner.invoke(app, ["list", "--store", str(store), "--json"])
        data = json.loads(result.output)
        assert [r["hash_short"] for r in data] == ["bbbbbbbb", "cccccccc", "aaaaaaaa"]

    def test_skips_dir_without_manifest(self, tmp_path):
        store = tmp_path / "store"
        (store / "runs" / ("x" * 64)).mkdir(parents=True)  # no manifest.json
        _write_manifest(store, "a" * 64, "completed", "2026-01-01T10:00:00+00:00")
        runner = CliRunner()
        result = runner.invoke(app, ["list", "--store", str(store)])
        assert result.exit_code == 0
        assert "aaaaaaaa" in result.output
        assert "1 run(s)" in result.output

    def test_skips_corrupt_manifest(self, tmp_path):
        store = tmp_path / "store"
        corrupt_dir = store / "runs" / ("c" * 64)
        corrupt_dir.mkdir(parents=True)
        (corrupt_dir / "manifest.json").write_text("not valid json {{{")
        _write_manifest(store, "a" * 64, "completed", "2026-01-01T10:00:00+00:00")
        runner = CliRunner()
        result = runner.invoke(app, ["list", "--store", str(store)])
        assert result.exit_code == 0
        assert "aaaaaaaa" in result.output

    def test_failed_run_shows_failure_count(self, tmp_path):
        store = tmp_path / "store"
        _write_manifest(
            store, "a" * 64, "failed", "2026-01-01T10:00:00+00:00",
            total=10, completed=7, failed=3,
        )
        runner = CliRunner()
        result = runner.invoke(app, ["list", "--store", str(store), "--json"])
        data = json.loads(result.output)
        assert data[0]["failed"] == 3
        assert data[0]["completed"] == 7

    def test_running_status_present(self, tmp_path):
        store = tmp_path / "store"
        _write_manifest(
            store, "a" * 64, "running", "2026-01-01T10:00:00+00:00",
            total=100, completed=42,
        )
        runner = CliRunner()
        result = runner.invoke(app, ["list", "--store", str(store)])
        assert result.exit_code == 0
        assert "running" in result.output

    def test_run_count_shown(self, tmp_path):
        store = tmp_path / "store"
        _write_manifest(store, "a" * 64, "completed", "2026-01-01T10:00:00+00:00")
        _write_manifest(store, "b" * 64, "failed", "2026-01-02T10:00:00+00:00")
        runner = CliRunner()
        result = runner.invoke(app, ["list", "--store", str(store)])
        assert "2 run(s)" in result.output

    # --- new flags ---

    def test_recipe_filter_exact_match(self, tmp_path):
        store = tmp_path / "store"
        _write_manifest(store, "a" * 64, "completed", "2026-01-01T10:00:00+00:00", name="popsize_sweep")
        _write_manifest(store, "b" * 64, "completed", "2026-01-02T10:00:00+00:00", name="heuristic_compare")
        runner = CliRunner()
        result = runner.invoke(app, ["list", "--store", str(store), "--recipe", "popsize_sweep"])
        assert result.exit_code == 0
        assert "aaaaaaaa" in result.output
        assert "bbbbbbbb" not in result.output

    def test_recipe_filter_case_insensitive(self, tmp_path):
        store = tmp_path / "store"
        _write_manifest(store, "a" * 64, "completed", "2026-01-01T10:00:00+00:00", name="Popsize_Sweep")
        runner = CliRunner()
        result = runner.invoke(app, ["list", "--store", str(store), "--recipe", "popsize_sweep"])
        assert result.exit_code == 0
        assert "aaaaaaaa" in result.output

    def test_since_filter_excludes_older_runs(self, tmp_path):
        store = tmp_path / "store"
        _write_manifest(store, "a" * 64, "completed", "2026-01-01T10:00:00+00:00")
        _write_manifest(store, "b" * 64, "completed", "2026-06-15T10:00:00+00:00")
        runner = CliRunner()
        result = runner.invoke(app, ["list", "--store", str(store), "--since", "2026-06-01"])
        assert result.exit_code == 0
        assert "bbbbbbbb" in result.output
        assert "aaaaaaaa" not in result.output

    def test_since_filter_includes_same_day(self, tmp_path):
        store = tmp_path / "store"
        _write_manifest(store, "a" * 64, "completed", "2026-06-01T00:00:01+00:00")
        runner = CliRunner()
        result = runner.invoke(app, ["list", "--store", str(store), "--since", "2026-06-01"])
        assert result.exit_code == 0
        assert "aaaaaaaa" in result.output

    def test_limit_restricts_output(self, tmp_path):
        store = tmp_path / "store"
        _write_manifest(store, "a" * 64, "completed", "2026-01-01T10:00:00+00:00")
        _write_manifest(store, "b" * 64, "completed", "2026-01-02T10:00:00+00:00")
        _write_manifest(store, "c" * 64, "completed", "2026-01-03T10:00:00+00:00")
        runner = CliRunner()
        result = runner.invoke(app, ["list", "--store", str(store), "--limit", "2"])
        assert result.exit_code == 0
        # most recent two only (b and c sorted desc → c, b)
        assert "cccccccc" in result.output
        assert "bbbbbbbb" in result.output
        assert "aaaaaaaa" not in result.output

    def test_limit_json(self, tmp_path):
        store = tmp_path / "store"
        for i, dt in enumerate(["2026-01-01", "2026-01-02", "2026-01-03"]):
            _write_manifest(store, str(i) * 64, "completed", f"{dt}T10:00:00+00:00")
        runner = CliRunner()
        result = runner.invoke(app, ["list", "--store", str(store), "--json", "--limit", "2"])
        data = json.loads(result.output)
        assert len(data) == 2

    # --- format ---

    def test_progress_dash_for_completed(self, tmp_path):
        store = tmp_path / "store"
        _write_manifest(store, "a" * 64, "completed", "2026-01-01T10:00:00+00:00", total=5)
        runner = CliRunner()
        result = runner.invoke(app, ["list", "--store", str(store)])
        assert result.exit_code == 0
        # completed run should show "-" not "5/5"
        assert " - " in result.output or result.output.endswith("-\n\n")

    def test_progress_counts_for_running(self, tmp_path):
        store = tmp_path / "store"
        _write_manifest(
            store, "a" * 64, "running", "2026-01-01T10:00:00+00:00",
            total=100, completed=42,
        )
        runner = CliRunner()
        result = runner.invoke(app, ["list", "--store", str(store)])
        assert result.exit_code == 0
        assert "42/100" in result.output

    def test_column_order_hash_recipe_started_status_pub_progress(self, tmp_path):
        store = tmp_path / "store"
        _write_manifest(store, "a" * 64, "completed", "2026-01-15T10:00:00+00:00", name="my_exp")
        runner = CliRunner()
        result = runner.invoke(app, ["list", "--store", str(store)])
        line = [l for l in result.output.splitlines() if "aaaaaaaa" in l][0]
        # verify relative column order in the row
        positions = {
            "hash": line.index("aaaaaaaa"),
            "recipe": line.index("my_exp"),
            "started": line.index("2026-01-15"),
            "status": line.index("completed"),
        }
        positions["published"] = line.index("-", positions["status"] + len("completed"))
        assert (
            positions["hash"]
            < positions["recipe"]
            < positions["started"]
            < positions["status"]
            < positions["published"]
        )
