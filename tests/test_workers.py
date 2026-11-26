"""Tests for parallel execution (execute_run_parallel)."""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from ygor import manifest as _manifest
from ygor import store as _store
from ygor.expander import build_plan
from ygor.parser import load_recipe
from ygor.runner import execute_run_parallel


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _write_recipe(path: Path, content: str) -> Path:
    r = path / "recipe.yaml"
    r.write_text(content, encoding="utf-8")
    return r


def _setup(tmp_path: Path, content: str, workers: int = 2):
    recipe_file = _write_recipe(tmp_path, content)
    recipe = load_recipe(recipe_file)
    plan = build_plan(recipe)
    run_hash = "cafebabe" * 8
    run_dir = tmp_path / "run"
    _store.create_run_dirs(run_dir)
    if plan.seeds:
        raw = [(pt, s) for pt in plan.param_tuples for s in plan.seeds]
    else:
        raw = [(pt, 0) for pt in plan.param_tuples]
    executions = [(i + 1, pt, s) for i, (pt, s) in enumerate(raw)]
    m = _manifest.build_initial(
        run_hash=run_hash,
        recipe_path="r.yaml",
        recipe_dict={},
        seeds_info=None,
        total_executions=len(executions),
        started_at="t",
    )
    return recipe, executions, run_hash, run_dir, m


# ---------------------------------------------------------------------------
# Parallel correctness
# ---------------------------------------------------------------------------

class TestParallelExecution:
    def test_all_complete_parallel(self, tmp_path):
        content = (
            "command: 'echo {{ param.n }}'\n"
            "parameters:\n"
            "  values:\n"
            "    n: [1, 2, 3, 4, 5]\n"
        )
        recipe, executions, run_hash, run_dir, m = _setup(tmp_path, content, workers=3)
        m = execute_run_parallel(recipe, executions, run_hash, run_dir, m, _manifest.write, workers=3)
        assert m["state"]["counts"]["completed"] == 5
        assert m["state"]["counts"]["failed"] == 0
        assert m["state"]["counts"]["pending"] == 0

    def test_parallel_with_workers_1_same_as_sequential(self, tmp_path):
        content = (
            "command: 'echo {{ param.n }}'\n"
            "parameters:\n"
            "  values:\n"
            "    n: [1, 2, 3]\n"
        )
        recipe, executions, run_hash, run_dir, m = _setup(tmp_path, content, workers=1)
        m = execute_run_parallel(recipe, executions, run_hash, run_dir, m, _manifest.write, workers=1)
        assert m["state"]["counts"]["completed"] == 3

    def test_parallel_faster_than_sequential(self, tmp_path):
        # 6 jobs each sleeping 0.2s; with 3 workers should finish in ~0.4s, not 1.2s
        content = (
            "command: 'sleep 0.2'\n"
            "parameters:\n"
            "  values:\n"
            "    n: [1, 2, 3, 4, 5, 6]\n"
        )
        recipe, executions, run_hash, run_dir, m = _setup(tmp_path, content, workers=3)
        t0 = time.monotonic()
        m = execute_run_parallel(recipe, executions, run_hash, run_dir, m, _manifest.write, workers=3)
        elapsed = time.monotonic() - t0
        assert elapsed < 1.0, f"parallel took {elapsed:.2f}s — expected < 1.0s"
        assert m["state"]["counts"]["completed"] == 6

    def test_parallel_exec_ids_are_unique(self, tmp_path):
        content = (
            "command: 'echo {{ run.exec_id }}'\n"
            "parameters:\n"
            "  values:\n"
            "    n: [1, 2, 3, 4]\n"
        )
        recipe, executions, run_hash, run_dir, m = _setup(tmp_path, content, workers=2)
        m = execute_run_parallel(recipe, executions, run_hash, run_dir, m, _manifest.write, workers=2)
        ids = [e["exec_id"] for e in m["executions"]]
        assert sorted(ids) == [1, 2, 3, 4]

    def test_manifest_counts_consistent_after_parallel(self, tmp_path):
        content = (
            "command: 'echo x'\n"
            "parameters:\n"
            "  values:\n"
            "    n: [1, 2, 3, 4, 5, 6]\n"
        )
        recipe, executions, run_hash, run_dir, m = _setup(tmp_path, content, workers=4)
        m = execute_run_parallel(recipe, executions, run_hash, run_dir, m, _manifest.write, workers=4)
        c = m["state"]["counts"]
        assert c["completed"] + c["failed"] + c["pending"] + c["running"] == 6

    def test_parallel_failures_counted(self, tmp_path):
        content = (
            "command: 'false'\n"
            "parameters:\n"
            "  values:\n"
            "    n: [1, 2, 3]\n"
            "policy:\n"
            "  on-failure: continue\n"
        )
        recipe, executions, run_hash, run_dir, m = _setup(tmp_path, content, workers=2)
        m = execute_run_parallel(recipe, executions, run_hash, run_dir, m, _manifest.write, workers=2)
        assert m["state"]["counts"]["failed"] == 3
        assert m["state"]["counts"]["completed"] == 0

    def test_parallel_abort_on_failure(self, tmp_path):
        content = (
            "command: 'false'\n"
            "parameters:\n"
            "  values:\n"
            "    n: [1, 2, 3, 4, 5]\n"
            "policy:\n"
            "  on-failure: abort\n"
        )
        recipe, executions, run_hash, run_dir, m = _setup(tmp_path, content, workers=2)
        m = execute_run_parallel(recipe, executions, run_hash, run_dir, m, _manifest.write, workers=2)
        assert m["state"]["status"] == "failed"
        # Not all 5 ran (abort stops dispatch)
        total_run = m["state"]["counts"]["completed"] + m["state"]["counts"]["failed"]
        assert total_run < 5

    def test_on_progress_called_for_each(self, tmp_path):
        content = (
            "command: 'echo x'\n"
            "parameters:\n"
            "  values:\n"
            "    n: [1, 2, 3]\n"
        )
        recipe, executions, run_hash, run_dir, m = _setup(tmp_path, content, workers=2)
        seen = []
        execute_run_parallel(
            recipe, executions, run_hash, run_dir, m, _manifest.write,
            workers=2, on_progress=lambda r: seen.append(r.exec_id),
        )
        assert sorted(seen) == [1, 2, 3]

    def test_output_dirs_created_parallel(self, tmp_path):
        content = (
            "command: 'echo x'\n"
            "parameters:\n"
            "  values:\n"
            "    n: [1, 2, 3]\n"
        )
        recipe, executions, run_hash, run_dir, m = _setup(tmp_path, content, workers=3)
        execute_run_parallel(recipe, executions, run_hash, run_dir, m, _manifest.write, workers=3)
        for i in [1, 2, 3]:
            assert (run_dir / "outputs" / f"exec_{i:04d}").is_dir()

    def test_parallel_no_race_in_manifest_write(self, tmp_path):
        """Manifest must be consistent JSON after concurrent writes."""
        content = (
            "command: 'echo x'\n"
            "parameters:\n"
            "  values:\n"
            "    n: [1, 2, 3, 4, 5, 6, 7, 8]\n"
        )
        recipe, executions, run_hash, run_dir, m = _setup(tmp_path, content, workers=4)
        execute_run_parallel(recipe, executions, run_hash, run_dir, m, _manifest.write, workers=4)
        loaded = _manifest.read(run_dir)
        assert loaded["state"]["counts"]["completed"] == 8
