"""Tests for ygor status, ygor pause, and ygor resume commands."""

from __future__ import annotations

import json
import os
import time
import threading
from pathlib import Path

import pytest
from typer.testing import CliRunner

from ygor import manifest as _manifest
from ygor import store as _store
from ygor.cli import app

cli = CliRunner()

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _write_recipe(path: Path, content: str) -> Path:
    r = path / "recipe.yaml"
    r.write_text(content, encoding="utf-8")
    return r


def _invoke(args: list[str]) -> object:
    return cli.invoke(app, args)


def _load_manifest(store_path: Path) -> dict:
    runs = list((store_path / "runs").iterdir())
    assert len(runs) >= 1
    # Return the most recent run (first alphabetically by hash — only one in most tests)
    runs.sort(key=lambda p: p.name)
    return _manifest.read(runs[-1])


def _run_dir(store_path: Path) -> Path:
    runs = list((store_path / "runs").iterdir())
    runs.sort(key=lambda p: p.name)
    return runs[-1]


# ---------------------------------------------------------------------------
# ygor status
# ---------------------------------------------------------------------------

class TestStatusCommand:
    def test_status_completed_run(self, tmp_path):
        recipe_file = _write_recipe(tmp_path, 'command: "echo x"\n')
        store = tmp_path / "store"
        _invoke(["run", str(recipe_file), "--store", str(store), "--yes"])
        m = _load_manifest(store)
        run_hash = m["run_hash"]

        result = _invoke(["status", run_hash[:8], "--store", str(store)])
        assert result.exit_code == 0
        assert "completed" in result.output

    def test_status_shows_counts(self, tmp_path):
        content = (
            "command: 'echo x'\n"
            "parameters:\n"
            "  values:\n"
            "    n: [1, 2, 3]\n"
        )
        recipe_file = _write_recipe(tmp_path, content)
        store = tmp_path / "store"
        _invoke(["run", str(recipe_file), "--store", str(store), "--yes"])
        m = _load_manifest(store)

        result = _invoke(["status", m["run_hash"][:8], "--store", str(store)])
        assert result.exit_code == 0
        assert "3" in result.output

    def test_status_json_output(self, tmp_path):
        recipe_file = _write_recipe(tmp_path, 'command: "echo x"\n')
        store = tmp_path / "store"
        _invoke(["run", str(recipe_file), "--store", str(store), "--yes"])
        m = _load_manifest(store)

        result = _invoke(["status", "--json", m["run_hash"][:8], "--store", str(store)])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert "status" in data
        assert "counts" in data

    def test_status_unknown_hash(self, tmp_path):
        store = tmp_path / "store"
        store.mkdir(parents=True)
        result = _invoke(["status", "deadbeef", "--store", str(store)])
        assert result.exit_code == 1

    def test_status_no_args_picks_latest(self, tmp_path):
        recipe_file = _write_recipe(tmp_path, 'command: "echo x"\n')
        store = tmp_path / "store"
        _invoke(["run", str(recipe_file), "--store", str(store), "--yes"])

        result = _invoke(["status", "--store", str(store)])
        assert result.exit_code == 0
        assert "completed" in result.output

    def test_status_selects_latest_run_by_recipe(self, tmp_path):
        store = tmp_path / "store"
        target_hash = "a" * 64
        other_hash = "b" * 64
        for run_hash, recipe_name, started_at in (
            (target_hash, "target", "2026-01-01T10:00:00+00:00"),
            (other_hash, "other", "2026-03-01T10:00:00+00:00"),
        ):
            run_dir = store / "runs" / run_hash
            _store.create_run_dirs(run_dir)
            m = _manifest.build_initial(
                run_hash=run_hash,
                recipe_path=f"recipes/{recipe_name}/recipe.yaml",
                recipe_dict={"meta": {"name": recipe_name}, "command": ["echo x"]},
                seeds_info=None,
                total_executions=1,
                started_at=started_at,
            )
            m["state"]["status"] = "completed"
            m["state"]["counts"]["completed"] = 1
            _manifest.write(m, run_dir)

        result = _invoke(["status", "--recipe", "target", "--store", str(store)])
        assert result.exit_code == 0
        assert "aaaaaaaa" in result.output
        assert "target" in result.output

    def test_status_selects_latest_run_by_status(self, tmp_path):
        store = tmp_path / "store"
        for run_hash, status, started_at in (
            ("a" * 64, "failed", "2026-01-01T10:00:00+00:00"),
            ("b" * 64, "completed", "2026-03-01T10:00:00+00:00"),
        ):
            run_dir = store / "runs" / run_hash
            _store.create_run_dirs(run_dir)
            m = _manifest.build_initial(
                run_hash=run_hash,
                recipe_path=f"recipes/{status}/recipe.yaml",
                recipe_dict={"meta": {"name": status}, "command": ["echo x"]},
                seeds_info=None,
                total_executions=1,
                started_at=started_at,
            )
            m["state"]["status"] = status
            _manifest.write(m, run_dir)

        result = _invoke(["status", "--status", "failed", "--store", str(store)])
        assert result.exit_code == 0
        assert "aaaaaaaa" in result.output
        assert "failed" in result.output


# ---------------------------------------------------------------------------
# ygor pause (sentinel file)
# ---------------------------------------------------------------------------

class TestPauseFile:
    def test_create_and_clear_pause_request(self, tmp_path):
        run_dir = tmp_path / "run"
        _store.create_run_dirs(run_dir)
        assert not _store.is_pause_requested(run_dir)
        _store.create_pause_request(run_dir)
        assert _store.is_pause_requested(run_dir)
        _store.clear_pause_request(run_dir)
        assert not _store.is_pause_requested(run_dir)

    def test_create_and_clear_force_pause_request(self, tmp_path):
        run_dir = tmp_path / "run"
        _store.create_run_dirs(run_dir)
        assert not _store.is_force_pause_requested(run_dir)
        _store.create_force_pause_request(run_dir)
        assert _store.is_force_pause_requested(run_dir)
        _store.clear_pause_request(run_dir)
        assert not _store.is_force_pause_requested(run_dir)

    def test_pause_command_creates_sentinel(self, tmp_path):
        # Set up a fake "running" manifest so pause can find a running run
        store = tmp_path / "store"
        run_hash = "a" * 64
        run_dir = store / "runs" / run_hash
        _store.create_run_dirs(run_dir)
        m = _manifest.build_initial(
            run_hash=run_hash, recipe_path="r.yaml", recipe_dict={},
            seeds_info=None, total_executions=10, started_at="t",
        )
        m["state"]["status"] = "running"
        _manifest.write(m, run_dir)

        result = _invoke(["pause", run_hash[:8], "--store", str(store)])
        assert result.exit_code == 0
        assert _store.is_pause_requested(run_dir)

    def test_pause_force_command_creates_force_sentinel(self, tmp_path):
        store = tmp_path / "store"
        run_hash = "f" * 64
        run_dir = store / "runs" / run_hash
        _store.create_run_dirs(run_dir)
        m = _manifest.build_initial(
            run_hash=run_hash, recipe_path="r.yaml", recipe_dict={},
            seeds_info=None, total_executions=10, started_at="t",
        )
        m["state"]["status"] = "running"
        _manifest.write(m, run_dir)

        result = _invoke(["pause", run_hash[:8], "--store", str(store), "--force"])
        assert result.exit_code == 0
        assert _store.is_force_pause_requested(run_dir)
        assert not _store.is_pause_requested(run_dir)

    def test_pause_rejects_non_running(self, tmp_path):
        store = tmp_path / "store"
        run_hash = "b" * 64
        run_dir = store / "runs" / run_hash
        _store.create_run_dirs(run_dir)
        m = _manifest.build_initial(
            run_hash=run_hash, recipe_path="r.yaml", recipe_dict={},
            seeds_info=None, total_executions=3, started_at="t",
        )
        m["state"]["status"] = "completed"
        _manifest.write(m, run_dir)

        result = _invoke(["pause", run_hash[:8], "--store", str(store)])
        assert result.exit_code == 1
        assert "completed" in result.output


# ---------------------------------------------------------------------------
# Pause during execution (integration)
# ---------------------------------------------------------------------------

class TestPauseDuringExecution:
    def test_parallel_stops_when_pause_requested(self, tmp_path):
        from ygor import manifest as _manifest
        from ygor import store as _store
        from ygor.runner import execute_run_parallel
        from ygor.expander import build_plan
        from ygor.parser import load_recipe

        # 10 slow executions; we request pause after a short delay
        content = (
            "command: 'sleep 0.1'\n"
            "parameters:\n"
            "  values:\n"
            "    n: [1, 2, 3, 4, 5, 6, 7, 8, 9, 10]\n"
        )
        recipe_file = tmp_path / "recipe.yaml"
        recipe_file.write_text(content)
        recipe = load_recipe(recipe_file)
        plan = build_plan(recipe)
        run_hash = "deadcafe" * 8
        run_dir = tmp_path / "run"
        _store.create_run_dirs(run_dir)
        raw = [(pt, 0) for pt in plan.param_tuples]
        executions = [(i + 1, pt, s) for i, (pt, s) in enumerate(raw)]
        m = _manifest.build_initial(
            run_hash=run_hash, recipe_path="r.yaml", recipe_dict={},
            seeds_info=None, total_executions=len(executions), started_at="t",
        )

        # Request pause after 0.15s (some will have started, some won't)
        def _request_pause():
            time.sleep(0.15)
            _store.create_pause_request(run_dir)

        t = threading.Thread(target=_request_pause, daemon=True)
        t.start()

        m = execute_run_parallel(
            recipe, executions, run_hash, run_dir, m, _manifest.write, workers=3
        )
        t.join(timeout=2.0)

        assert m["state"]["status"] == "paused"
        total_run = m["state"]["counts"]["completed"] + m["state"]["counts"]["failed"]
        assert total_run < 10, "Expected pause to stop before all 10 ran"
        assert m["state"]["counts"]["running"] == 0
        assert m["state"]["counts"]["pending"] == 10 - total_run
        assert len(m["executions"]) == total_run
        # Pause sentinel is cleaned up
        assert not _store.is_pause_requested(run_dir)

    def test_force_pause_kills_running_and_leaves_pending(self, tmp_path):
        from ygor import manifest as _manifest
        from ygor import store as _store
        from ygor.runner import execute_run_parallel
        from ygor.expander import build_plan
        from ygor.parser import load_recipe

        content = (
            "command: 'sleep 5'\n"
            "parameters:\n"
            "  values:\n"
            "    n: [1, 2, 3, 4, 5]\n"
        )
        recipe_file = tmp_path / "recipe.yaml"
        recipe_file.write_text(content)
        recipe = load_recipe(recipe_file)
        plan = build_plan(recipe)
        run_hash = "forceabc" * 8
        run_dir = tmp_path / "run"
        _store.create_run_dirs(run_dir)
        raw = [(pt, 0) for pt in plan.param_tuples]
        executions = [(i + 1, pt, s) for i, (pt, s) in enumerate(raw)]
        m = _manifest.build_initial(
            run_hash=run_hash, recipe_path="r.yaml", recipe_dict={},
            seeds_info=None, total_executions=len(executions), started_at="t",
        )

        def _request_force_pause():
            time.sleep(0.2)
            _store.create_force_pause_request(run_dir)

        t = threading.Thread(target=_request_force_pause, daemon=True)
        t.start()

        t0 = time.monotonic()
        m = execute_run_parallel(
            recipe, executions, run_hash, run_dir, m, _manifest.write, workers=3
        )
        elapsed = time.monotonic() - t0
        t.join(timeout=2.0)

        assert elapsed < 2.5
        assert m["state"]["status"] == "paused"
        assert m["state"]["counts"] == {
            "completed": 0,
            "failed": 0,
            "running": 0,
            "pending": 5,
        }
        assert m["executions"] == []
        assert not _store.is_force_pause_requested(run_dir)


# ---------------------------------------------------------------------------
# ygor resume
# ---------------------------------------------------------------------------

class TestResumeCommand:
    def _start_and_pause(self, tmp_path, content: str) -> tuple[Path, str]:
        """Run a recipe that will pause (sets pause file before start)."""
        from ygor import manifest as _manifest
        from ygor import store as _store
        from ygor.expander import build_plan
        from ygor.parser import load_recipe
        from ygor.runner import execute_run_parallel

        store = tmp_path / "store"
        recipe_file = _write_recipe(tmp_path, content)
        recipe = load_recipe(recipe_file)
        plan = build_plan(recipe)
        run_hash = "cafef00d" * 8
        run_dir = store / "runs" / run_hash
        _store.create_run_dirs(run_dir)

        raw = [(pt, 0) for pt in plan.param_tuples]
        executions = [(i + 1, pt, s) for i, (pt, s) in enumerate(raw)]
        m = _manifest.build_initial(
            run_hash=run_hash, recipe_path=str(recipe_file),
            recipe_dict=recipe.model_dump(by_alias=True, mode="json"),
            seeds_info=None, total_executions=len(executions), started_at="t",
        )
        m["plan"] = {
            "executions": [
                {"exec_id": exec_id, "params": params, "seed": seed}
                for exec_id, params, seed in executions
            ]
        }
        m["resolved"] = {"datasets": {}, "apps": {}, "docker": None}

        # Write recipe lock
        _store.write_recipe_lock(run_dir, recipe_file.read_text())

        # Pause immediately — only 2 workers, pause after first batch
        _store.create_pause_request(run_dir)
        m = execute_run_parallel(
            recipe, executions, run_hash, run_dir, m, _manifest.write, workers=1
        )
        assert m["state"]["status"] == "paused"
        _manifest.write(m, run_dir)
        return store, run_hash

    def test_resume_completes_paused_run(self, tmp_path):
        content = (
            "command: 'echo {{ param.n }}'\n"
            "parameters:\n"
            "  values:\n"
            "    n: [1, 2, 3, 4, 5]\n"
        )
        store, run_hash = self._start_and_pause(tmp_path, content)
        result = _invoke(["resume", run_hash[:8], "--store", str(store), "--yes"])
        assert result.exit_code == 0

        m = _manifest.read(store / "runs" / run_hash)
        assert m["state"]["status"] in ("completed", "failed", "paused")
        # At least some executions ran total
        total = m["state"]["counts"]["completed"] + m["state"]["counts"]["failed"]
        assert total > 0

    def test_resume_no_args_picks_paused(self, tmp_path):
        content = (
            "command: 'echo x'\n"
            "parameters:\n"
            "  values:\n"
            "    n: [1, 2, 3]\n"
        )
        store, run_hash = self._start_and_pause(tmp_path, content)
        result = _invoke(["resume", "--store", str(store), "--yes"])
        assert result.exit_code == 0

    def test_resume_non_resumable_status_fails(self, tmp_path):
        store = tmp_path / "store"
        run_hash = "cc" * 32
        run_dir = store / "runs" / run_hash
        _store.create_run_dirs(run_dir)
        m = _manifest.build_initial(
            run_hash=run_hash, recipe_path="r.yaml", recipe_dict={},
            seeds_info=None, total_executions=1, started_at="t",
        )
        m["state"]["status"] = "completed"
        _manifest.write(m, run_dir)

        result = _invoke(["resume", run_hash[:8], "--store", str(store), "--yes"])
        assert result.exit_code == 1
        assert "completed" in result.output

    def test_resume_preserves_completed_exec_ids(self, tmp_path):
        """Resumed executions must get their original exec_ids, not start from 1."""
        content = (
            'command: "echo {{ param.n }}"\n'
            "parameters:\n"
            "  values:\n"
            "    n: [1, 2, 3, 4, 5]\n"
        )
        store, run_hash = self._start_and_pause(tmp_path, content)

        m_before = _manifest.read(store / "runs" / run_hash)
        completed_before = {e["exec_id"] for e in m_before.get("executions", []) if e["status"] == "completed"}

        result = _invoke(["resume", run_hash[:8], "--store", str(store), "--yes"])
        assert result.exit_code == 0

        m_after = _manifest.read(store / "runs" / run_hash)
        execs = m_after.get("executions", [])
        ids = [e["exec_id"] for e in execs]

        # All exec_ids must be unique — no duplicate overwrites
        assert len(ids) == len(set(ids)), f"duplicate exec_ids: {ids}"

        # Previously completed execs must still be completed (not overwritten)
        status_by_id = {e["exec_id"]: e["status"] for e in execs}
        for cid in completed_before:
            assert status_by_id.get(cid) == "completed", (
                f"exec_id {cid} was completed before resume but is now {status_by_id.get(cid)!r}"
            )

    def test_resume_rebuilds_missing_frozen_plan_from_manifest_recipe(self, tmp_path):
        """Older manifests without plan.executions should still be resumable."""
        store = tmp_path / "store"
        run_hash = "ab" * 32
        run_dir = store / "runs" / run_hash
        _store.create_run_dirs(run_dir)

        recipe_file = _write_recipe(tmp_path, (
            'command: "echo {{ param.n }}"\n'
            "parameters:\n"
            "  values:\n"
            "    n: [1, 2, 3]\n"
        ))
        from ygor.parser import load_recipe
        recipe = load_recipe(recipe_file)

        m = _manifest.build_initial(
            run_hash=run_hash,
            recipe_path=str(recipe_file),
            recipe_dict=recipe.model_dump(by_alias=True, mode="json"),
            seeds_info=None,
            total_executions=3,
            started_at="t",
        )
        m["state"]["status"] = "paused"
        m["state"]["counts"] = {"completed": 1, "failed": 0, "running": 0, "pending": 2}
        m["executions"] = [{
            "exec_id": 1,
            "params": {"n": 1},
            "seed": 0,
            "status": "completed",
            "started_at": "t",
            "ended_at": "t",
            "duration_sec": 0,
            "exit_code": 0,
            "failure_reason": None,
            "retry_count": 0,
            "output_dir": "outputs/exec_0001",
        }]
        _manifest.write(m, run_dir)

        result = _invoke(["resume", run_hash[:8], "--store", str(store), "--yes"])
        assert result.exit_code == 0, result.output

        m_after = _manifest.read(run_dir)
        assert m_after["state"]["status"] == "completed"
        assert [e["exec_id"] for e in m_after["executions"]] == [1, 2, 3]

    def test_resume_uses_container_config_for_legacy_docker_recipe(self, tmp_path, monkeypatch):
        """A resumed legacy Docker manifest must keep running inside the declared container."""
        import ygor.cli as cli_module

        store = tmp_path / "store"
        run_hash = "ee" * 32
        run_dir = store / "runs" / run_hash
        _store.create_run_dirs(run_dir)

        recipe_file = _write_recipe(tmp_path, (
            "environment:\n"
            "  base-image: alpine:latest\n"
            "  variables:\n"
            "    FOO: bar\n"
            'command: "echo $FOO"\n'
        ))
        from ygor.parser import load_recipe
        recipe = load_recipe(recipe_file)
        _store.write_recipe_lock(run_dir, recipe_file.read_text())

        m = _manifest.build_initial(
            run_hash=run_hash,
            recipe_path=str(recipe_file),
            recipe_dict=recipe.model_dump(by_alias=True, mode="json"),
            seeds_info=None,
            total_executions=1,
            started_at="t",
        )
        m["state"]["status"] = "paused"
        m["state"]["counts"] = {"completed": 0, "failed": 0, "running": 0, "pending": 1}
        m["plan"] = {"executions": [{"exec_id": 1, "params": {}, "seed": 0}]}
        m["resolved"] = {
            "datasets": {},
            "apps": {},
            "docker": {
                "image": "ygor-test-image",
                "env_vars": {"FOO": "bar"},
                "mounts": [str(store.resolve()), str(tmp_path.resolve())],
                "cpu_limit": None,
                "memory_limit": None,
            },
        }
        _manifest.write(m, run_dir)

        captured = {}

        def fake_execute_run_parallel(
            recipe, executions, run_hash_full, run_dir_arg, manifest_arg, write_manifest, **kwargs
        ):
            captured["container_config"] = kwargs.get("container_config")
            manifest_arg["state"]["counts"] = {
                "completed": 1,
                "failed": 0,
                "running": 0,
                "pending": 0,
            }
            manifest_arg["executions"] = [{
                "exec_id": 1,
                "params": {},
                "seed": 0,
                "status": "completed",
                "started_at": "t",
                "ended_at": "t",
                "duration_sec": 0,
                "exit_code": 0,
                "failure_reason": None,
                "retry_count": 0,
                "output_dir": "outputs/exec_0001",
            }]
            return manifest_arg

        monkeypatch.setattr(cli_module._runner, "execute_run_parallel", fake_execute_run_parallel)

        result = _invoke(["resume", run_hash[:8], "--store", str(store), "--yes"])
        assert result.exit_code == 0, result.output

        cfg = captured.get("container_config")
        assert cfg is not None
        assert cfg.image == "ygor-test-image"
        assert cfg.env_vars["FOO"] == "bar"
        assert store.resolve() in cfg.mounts
        assert tmp_path.resolve() in cfg.mounts

    def test_resume_uses_only_frozen_manifest_data(self, tmp_path, monkeypatch):
        """Resume must use frozen run metadata, even if recipe.yaml changed."""
        import ygor.cli as cli_module

        store = tmp_path / "store"
        run_hash = "ed" * 32
        run_dir = store / "runs" / run_hash
        _store.create_run_dirs(run_dir)

        recipe_file = _write_recipe(tmp_path, (
            "environment:\n"
            "  base-image: alpine:latest\n"
            "  variables:\n"
            "    FOO: locked\n"
            "  apps:\n"
            "    solver:\n"
            "      source: { type: local, path: ./solver }\n"
            "      binary: bin/solver\n"
            "command: \"{{ app['solver'] }} --foo {{ param.n }}\"\n"
            "parameters:\n"
            "  values:\n"
            "    n: [1]\n"
        ))
        from ygor.parser import load_recipe
        recipe = load_recipe(recipe_file)
        _store.write_recipe_lock(run_dir, recipe_file.read_text())

        # Simulate user editing recipe.yaml after the run was created.
        recipe_file.write_text(
            "command: \"echo changed\"\n"
            "parameters:\n"
            "  values:\n"
            "    n: [99]\n",
            encoding="utf-8",
        )

        m = _manifest.build_initial(
            run_hash=run_hash,
            recipe_path=str(recipe_file),
            recipe_dict=recipe.model_dump(by_alias=True, mode="json"),
            seeds_info=None,
            total_executions=1,
            started_at="t",
        )
        m["state"]["status"] = "paused"
        m["state"]["counts"] = {"completed": 0, "failed": 0, "running": 0, "pending": 1}
        m["plan"] = {"executions": [{"exec_id": 1, "params": {"n": 1}, "seed": 0}]}
        m["resolved"] = {
            "datasets": {},
            "apps": {"solver": "/locked/store/solver/bin/solver"},
            "docker": {
                "image": "ygor-locked-image",
                "env_vars": {"FOO": "locked"},
                "mounts": [str(store.resolve())],
                "cpu_limit": 2,
                "memory_limit": "4GB",
            },
        }
        _manifest.write(m, run_dir)

        captured = {}

        def should_not_prepare(*args, **kwargs):
            raise AssertionError("resume should use locked container metadata")

        def should_not_resolve_apps(*args, **kwargs):
            raise AssertionError("resume should use locked app paths")

        def fake_execute_run_parallel(
            recipe, executions, run_hash_full, run_dir_arg, manifest_arg, write_manifest, **kwargs
        ):
            captured["recipe_command"] = recipe.command
            captured["executions"] = executions
            captured["app_paths"] = kwargs.get("app_paths")
            captured["container_config"] = kwargs.get("container_config")
            manifest_arg["state"]["counts"] = {
                "completed": 1,
                "failed": 0,
                "running": 0,
                "pending": 0,
            }
            manifest_arg["executions"] = [{
                "exec_id": 1,
                "params": {"n": 1},
                "seed": 0,
                "status": "completed",
                "started_at": "t",
                "ended_at": "t",
                "duration_sec": 0,
                "exit_code": 0,
                "failure_reason": None,
                "retry_count": 0,
                "output_dir": "outputs/exec_0001",
            }]
            return manifest_arg

        monkeypatch.setattr(cli_module, "_prepare_docker_environment", should_not_prepare)
        monkeypatch.setattr(cli_module, "ensure_app_sources", should_not_resolve_apps)
        monkeypatch.setattr(cli_module._runner, "execute_run_parallel", fake_execute_run_parallel)

        result = _invoke(["resume", run_hash[:8], "--store", str(store), "--yes"])
        assert result.exit_code == 0, result.output

        assert captured["recipe_command"] == ["{{ app['solver'] }} --foo {{ param.n }}"]
        assert captured["executions"] == [(1, {"n": 1}, 0)]
        assert captured["app_paths"] == {"solver": "/locked/store/solver/bin/solver"}
        cfg = captured["container_config"]
        assert cfg.image == "ygor-locked-image"
        assert cfg.env_vars == {"FOO": "locked"}
        assert cfg.cpu_limit == 2
        assert cfg.memory_limit == "4GB"

    def test_resume_fails_if_frozen_app_paths_are_missing(self, tmp_path, monkeypatch):
        """Do not resume by resolving or inferring app paths from current state."""
        import ygor.cli as cli_module

        store = tmp_path / "store"
        run_hash = "ef" * 32
        run_dir = store / "runs" / run_hash
        _store.create_run_dirs(run_dir)

        recipe_file = _write_recipe(tmp_path, (
            "environment:\n"
            "  apps:\n"
            "    solver:\n"
            "      source: { type: git, url: git@example.test:solver.git, ref: abc }\n"
            "      binary: build/bin/solver\n"
            "command: \"{{ app['solver'] }} --x\"\n"
        ))
        from ygor.parser import load_recipe
        recipe = load_recipe(recipe_file)
        _store.write_recipe_lock(run_dir, recipe_file.read_text())

        m = _manifest.build_initial(
            run_hash=run_hash,
            recipe_path=str(recipe_file),
            recipe_dict=recipe.model_dump(by_alias=True, mode="json"),
            seeds_info=None,
            total_executions=1,
            started_at="t",
        )
        m["state"]["status"] = "paused"
        m["state"]["counts"] = {"completed": 0, "failed": 0, "running": 0, "pending": 1}
        m["plan"] = {"executions": [{"exec_id": 1, "params": {}, "seed": 0}]}
        m["resolved"] = {"datasets": {}, "apps": {}, "docker": None}
        _manifest.write(m, run_dir)

        called = {"runner": False}

        def should_not_run(*args, **kwargs):
            called["runner"] = True
            raise AssertionError("runner should not be called")

        monkeypatch.setattr(
            cli_module,
            "ensure_app_sources",
            lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("must not infer app paths")),
        )
        monkeypatch.setattr(cli_module._runner, "execute_run_parallel", should_not_run)

        result = _invoke(["resume", run_hash[:8], "--store", str(store), "--yes"])
        assert result.exit_code == 1
        assert "frozen resolved app paths" in result.output
        assert called["runner"] is False
        assert not (run_dir / ".experiment" / "lock").exists()

    def test_resume_orphan_running_marked_failed(self, tmp_path):
        store = tmp_path / "store"
        run_hash = "dd" * 32
        run_dir = store / "runs" / run_hash
        _store.create_run_dirs(run_dir)

        content = (
            'command: "echo {{ param.n }}"\n'
            "parameters:\n"
            "  values:\n"
            "    n: [1, 2, 3]\n"
        )
        recipe_file = _write_recipe(tmp_path, content)
        from ygor.parser import load_recipe
        recipe = load_recipe(recipe_file)
        _store.write_recipe_lock(run_dir, recipe_file.read_text())

        m = _manifest.build_initial(
            run_hash=run_hash, recipe_path=str(recipe_file),
            recipe_dict=recipe.model_dump(by_alias=True, mode="json"),
            seeds_info=None, total_executions=3, started_at="t",
        )
        m["plan"] = {
            "executions": [
                {"exec_id": 1, "params": {"n": 1}, "seed": 0},
                {"exec_id": 2, "params": {"n": 2}, "seed": 0},
                {"exec_id": 3, "params": {"n": 3}, "seed": 0},
            ]
        }
        m["resolved"] = {"datasets": {}, "apps": {}, "docker": None}
        # Simulate an orphaned execution (status=running, no live PID)
        m["state"]["status"] = "running"
        m["state"]["counts"] = {"completed": 1, "failed": 0, "running": 1, "pending": 1}
        m["executions"] = [
            {"exec_id": 1, "params": {"n": 1}, "seed": 0, "status": "completed",
             "started_at": "t", "ended_at": "t", "duration_sec": 0,
             "exit_code": 0, "failure_reason": None, "retry_count": 0, "output_dir": ""},
            {"exec_id": 2, "params": {"n": 2}, "seed": 0, "status": "running",
             "started_at": "t", "ended_at": None, "duration_sec": None,
             "exit_code": None, "failure_reason": None, "retry_count": 0, "output_dir": ""},
        ]
        # Write a lock file with a dead PID
        import socket
        (run_dir / ".experiment" / "lock").write_text(f"99999999\n{socket.gethostname()}\n")
        _manifest.write(m, run_dir)

        result = _invoke(["resume", run_hash[:8], "--store", str(store), "--yes"])
        assert result.exit_code == 0
        m2 = _manifest.read(run_dir)
        orphan = next((e for e in m2.get("executions", []) if e["exec_id"] == 2), None)
        if orphan:
            assert orphan["status"] in ("failed", "completed")


# ---------------------------------------------------------------------------
# store.read_lock_file
# ---------------------------------------------------------------------------

class TestReadLockFile:
    def test_reads_pid_and_hostname(self, tmp_path):
        run_dir = tmp_path / "run"
        _store.create_run_dirs(run_dir)
        _store.write_lock_file(run_dir)
        result = _store.read_lock_file(run_dir)
        assert result is not None
        pid, hostname = result
        assert pid == os.getpid()
        import socket
        assert hostname == socket.gethostname()

    def test_returns_none_if_absent(self, tmp_path):
        run_dir = tmp_path / "run"
        _store.create_run_dirs(run_dir)
        assert _store.read_lock_file(run_dir) is None

    def test_returns_none_if_malformed(self, tmp_path):
        run_dir = tmp_path / "run"
        _store.create_run_dirs(run_dir)
        (run_dir / ".experiment" / "lock").write_text("notanint\nhostname\n")
        assert _store.read_lock_file(run_dir) is None
