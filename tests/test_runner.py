"""Integration tests for the runner and `ygor run` command."""

from __future__ import annotations

import csv
import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from ygor import manifest as _manifest
from ygor import store as _store
from ygor.cli import app
from ygor.expander import build_plan
from ygor.parser import load_recipe
from ygor.runner import execute_post, execute_run

cli = CliRunner()

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _write_recipe(path: Path, content: str) -> Path:
    recipe = path / "recipe.yaml"
    recipe.write_text(content, encoding="utf-8")
    return recipe


def _invoke_run(recipe_path: Path, store_path: Path, extra: list[str] | None = None) -> object:
    args = ["run", str(recipe_path), "--store", str(store_path), "--yes"]
    if extra:
        args += extra
    return cli.invoke(app, args)


def _load_manifest(store_path: Path) -> dict:
    runs = list((store_path / "runs").iterdir())
    assert len(runs) == 1, f"expected 1 run, found {len(runs)}"
    return _manifest.read(runs[0])


# ---------------------------------------------------------------------------
# store helpers
# ---------------------------------------------------------------------------

class TestStoreHelpers:
    def test_run_hash_is_deterministic(self):
        h1 = _store.compute_run_hash("cmd: echo\n", 42, "2026-01-01T00:00:00+00:00")
        h2 = _store.compute_run_hash("cmd: echo\n", 42, "2026-01-01T00:00:00+00:00")
        assert h1 == h2

    def test_different_timestamps_differ(self):
        h1 = _store.compute_run_hash("cmd: echo\n", 42, "2026-01-01T00:00:00+00:00")
        h2 = _store.compute_run_hash("cmd: echo\n", 42, "2026-01-01T00:00:01+00:00")
        assert h1 != h2

    def test_hash_is_hex_64_chars(self):
        h = _store.compute_run_hash("x", None, "t")
        assert len(h) == 64
        assert all(c in "0123456789abcdef" for c in h)

    def test_create_run_dirs(self, tmp_path):
        run_dir = tmp_path / "run"
        _store.create_run_dirs(run_dir)
        assert (run_dir / "outputs").is_dir()
        assert (run_dir / "streams").is_dir()
        assert (run_dir / "logs").is_dir()
        assert (run_dir / ".experiment").is_dir()
        assert not (run_dir / "results").exists()
        assert not (run_dir / "results" / "processed").exists()
        assert not (run_dir / "plots").exists()

    def test_exec_output_dir(self, tmp_path):
        d = _store.exec_output_dir(tmp_path, 7)
        assert d == tmp_path / "outputs" / "exec_0007"

    def test_exec_stream_dir(self, tmp_path):
        d = _store.exec_stream_dir(tmp_path, 7)
        assert d == tmp_path / "streams" / "exec_0007"


# ---------------------------------------------------------------------------
# manifest helpers
# ---------------------------------------------------------------------------

class TestManifestHelpers:
    def test_build_initial_structure(self):
        m = _manifest.build_initial(
            run_hash="abc",
            recipe_path="recipes/r/recipe.yaml",
            recipe_dict={},
            seeds_info=None,
            total_executions=5,
            started_at="2026-01-01T00:00:00+00:00",
        )
        assert m["manifest_version"] == 1
        assert m["state"]["total_executions"] == 5
        assert m["state"]["counts"]["pending"] == 5
        assert m["state"]["counts"]["completed"] == 0

    def test_write_and_read(self, tmp_path):
        run_dir = tmp_path / "run"
        run_dir.mkdir()
        m = _manifest.build_initial(
            run_hash="abc",
            recipe_path="r.yaml",
            recipe_dict={},
            seeds_info=None,
            total_executions=1,
            started_at="2026-01-01T00:00:00+00:00",
        )
        _manifest.write(m, run_dir)
        loaded = _manifest.read(run_dir)
        assert loaded["run_hash"] == "abc"
        assert (run_dir / "manifest.json").exists()
        assert (run_dir / "executions.csv").exists()
        assert (run_dir / "executions.jsonl").exists()

    def test_write_manifest_does_not_export_tables(self, tmp_path):
        run_dir = tmp_path / "run"
        run_dir.mkdir()
        m = _manifest.build_initial(
            run_hash="abc",
            recipe_path="r.yaml",
            recipe_dict={},
            seeds_info=None,
            total_executions=1,
            started_at="2026-01-01T00:00:00+00:00",
        )
        _manifest.write_manifest(m, run_dir)
        assert (run_dir / "manifest.json").exists()
        assert not (run_dir / "executions.csv").exists()
        assert not (run_dir / "executions.jsonl").exists()

    def test_write_is_atomic(self, tmp_path):
        run_dir = tmp_path / "run"
        run_dir.mkdir()
        m = _manifest.build_initial(
            run_hash="x", recipe_path="r", recipe_dict={},
            seeds_info=None, total_executions=0, started_at="t",
        )
        _manifest.write(m, run_dir)
        # .tmp file must not remain
        assert not (run_dir / "manifest.tmp").exists()
        assert not (run_dir / "executions.tmp").exists()


# ---------------------------------------------------------------------------
# runner unit tests
# ---------------------------------------------------------------------------

class TestRunner:
    def _setup(self, tmp_path: Path, recipe_content: str) -> tuple:
        recipe_file = _write_recipe(tmp_path, recipe_content)
        recipe = load_recipe(recipe_file)
        plan = build_plan(recipe)
        run_hash = "deadbeef" * 8
        run_dir = tmp_path / "run"
        _store.create_run_dirs(run_dir)
        raw = [(pt, s) for pt in plan.param_tuples for s in plan.seeds] if plan.seeds \
            else [(pt, 0) for pt in plan.param_tuples]
        executions = [(i + 1, pt, s) for i, (pt, s) in enumerate(raw)]
        m = _manifest.build_initial(
            run_hash=run_hash, recipe_path="r.yaml", recipe_dict={},
            seeds_info=None, total_executions=len(executions), started_at="t",
        )
        return recipe, executions, run_hash, run_dir, m

    def test_simple_success(self, tmp_path):
        recipe, executions, run_hash, run_dir, m = self._setup(
            tmp_path, 'command: "echo hello"\n'
        )
        m = execute_run(recipe, executions, run_hash, run_dir, m, _manifest.write)
        assert m["state"]["counts"]["completed"] == 1
        assert m["state"]["counts"]["failed"] == 0
        assert m["executions"][0]["status"] == "completed"
        assert m["executions"][0]["exit_code"] == 0

    def test_exec_creates_output_dir(self, tmp_path):
        recipe, executions, run_hash, run_dir, m = self._setup(
            tmp_path, 'command: "echo hello"\n'
        )
        execute_run(recipe, executions, run_hash, run_dir, m, _manifest.write)
        assert (run_dir / "outputs" / "exec_0001").is_dir()

    def test_relative_outputs_are_written_to_exec_output_dir(self, tmp_path):
        content = (
            'command: "echo {{ param.x }} > out.txt"\n'
            "parameters:\n"
            "  values:\n"
            "    x: [one, two]\n"
        )
        recipe, executions, run_hash, run_dir, m = self._setup(tmp_path, content)

        execute_run(recipe, executions, run_hash, run_dir, m, _manifest.write)

        out1 = run_dir / "outputs" / "exec_0001" / "out.txt"
        out2 = run_dir / "outputs" / "exec_0002" / "out.txt"
        assert out1.read_text().strip() == "one"
        assert out2.read_text().strip() == "two"
        assert not (run_dir / "out.txt").exists()

    def test_exec_creates_log(self, tmp_path):
        recipe, executions, run_hash, run_dir, m = self._setup(
            tmp_path, 'command: "echo hello"\n'
        )
        execute_run(recipe, executions, run_hash, run_dir, m, _manifest.write)
        log = run_dir / "logs" / "exec_0001.log"
        assert log.exists()
        assert b"echo hello" in log.read_bytes()

    def test_exec_captures_stdout_and_stderr_separately(self, tmp_path):
        recipe, executions, run_hash, run_dir, m = self._setup(
            tmp_path, 'command: "printf out; printf err >&2"\n'
        )

        execute_run(recipe, executions, run_hash, run_dir, m, _manifest.write)

        stdout = run_dir / "streams" / "exec_0001" / "stdout.log"
        stderr = run_dir / "streams" / "exec_0001" / "stderr.log"
        assert stdout.read_text() == "out"
        assert stderr.read_text() == "err"
        assert m["executions"][0]["stdout_path"] == "streams/exec_0001/stdout.log"
        assert m["executions"][0]["stderr_path"] == "streams/exec_0001/stderr.log"

    def test_exec_exposes_recipe_dir(self, tmp_path):
        recipe, executions, run_hash, run_dir, m = self._setup(
            tmp_path, 'command: "printf {{ recipe.dir }} > recipe_dir.txt"\n'
        )

        execute_run(
            recipe, executions, run_hash, run_dir, m, _manifest.write,
            recipe_dir=tmp_path,
        )

        output = run_dir / "outputs" / "exec_0001" / "recipe_dir.txt"
        assert output.read_text(encoding="utf-8") == str(tmp_path)

    def test_exec_exposes_apps_alias(self, tmp_path):
        recipe, executions, run_hash, run_dir, m = self._setup(
            tmp_path, 'command: "printf {{ apps.mean_time }} > app.txt"\n'
        )

        execute_run(
            recipe, executions, run_hash, run_dir, m, _manifest.write,
            app_paths={"mean_time": "/tools/mean_time.py"},
        )

        output = run_dir / "outputs" / "exec_0001" / "app.txt"
        assert output.read_text(encoding="utf-8") == "/tools/mean_time.py"

    def test_exec_exports_execution_tables(self, tmp_path):
        instance_path = tmp_path / "data" / "a.mtx"
        instance_path.parent.mkdir()
        instance_path.write_text("instance", encoding="utf-8")
        relative_instance = "../data/a.mtx"
        content = (
            'command: "echo {{ param.x }}"\n'
            "parameters:\n"
            "  values:\n"
            "    x: [42]\n"
            "  instance:\n"
            f"    - {json.dumps(str(instance_path))}\n"
        )
        recipe, executions, run_hash, run_dir, m = self._setup(tmp_path, content)

        execute_run(recipe, executions, run_hash, run_dir, m, _manifest.write)

        assert m["executions"][0]["params"]["instance"] == str(instance_path)

        with (run_dir / "executions.csv").open(encoding="utf-8", newline="") as fh:
            rows = list(csv.DictReader(fh))
        assert len(rows) == 1
        assert rows[0]["run_hash"] == run_hash
        assert rows[0]["exec_id"] == "1"
        assert rows[0]["status"] == "completed"
        assert rows[0]["output_dir"] == "outputs/exec_0001"
        assert rows[0]["stdout_path"] == "streams/exec_0001/stdout.log"
        assert rows[0]["stderr_path"] == "streams/exec_0001/stderr.log"
        assert rows[0]["log_path"] == "logs/exec_0001.log"
        assert rows[0]["param.x"] == "42"
        assert rows[0]["param.instance"] == relative_instance

        jsonl_rows = [
            json.loads(line)
            for line in (run_dir / "executions.jsonl").read_text(encoding="utf-8").splitlines()
        ]
        assert jsonl_rows == [{
            "duration_sec": jsonl_rows[0]["duration_sec"],
            "ended_at": jsonl_rows[0]["ended_at"],
            "exec_id": 1,
            "exit_code": 0,
            "failure_reason": None,
            "log_path": "logs/exec_0001.log",
            "output_dir": "outputs/exec_0001",
            "params": {"instance": relative_instance, "x": 42},
            "retry_count": 0,
            "run_hash": run_hash,
            "seed": 0,
            "started_at": jsonl_rows[0]["started_at"],
            "status": "completed",
            "stderr_path": "streams/exec_0001/stderr.log",
            "stdout_path": "streams/exec_0001/stdout.log",
        }]

    def test_failing_command_continue(self, tmp_path):
        content = (
            "command:\n"
            "  - 'false'\n"
            "  - 'echo after'\n"
            "parameters:\n"
            "  values:\n"
            "    x: [1, 2]\n"
            "policy:\n"
            "  on-failure: continue\n"
        )
        recipe, executions, run_hash, run_dir, m = self._setup(tmp_path, content)
        m = execute_run(recipe, executions, run_hash, run_dir, m, _manifest.write)
        assert m["state"]["counts"]["failed"] == 2
        assert m["state"]["counts"]["completed"] == 0
        # both were run (on-failure: continue)
        assert len(m["executions"]) == 2

    def test_failing_command_abort(self, tmp_path):
        content = (
            "command: 'false'\n"
            "parameters:\n"
            "  values:\n"
            "    x: [1, 2, 3]\n"
            "policy:\n"
            "  on-failure: abort\n"
        )
        recipe, executions, run_hash, run_dir, m = self._setup(tmp_path, content)
        m = execute_run(recipe, executions, run_hash, run_dir, m, _manifest.write)
        assert m["state"]["status"] == "failed"
        # only 1 execution ran (the first one failed and we aborted)
        assert m["state"]["counts"]["failed"] == 1

    def test_retry_success(self, tmp_path):
        # Command fails once then succeeds (use a counter file)
        counter = tmp_path / "count.txt"
        counter.write_text("0")
        # Shell script: increment counter; fail if count <= 1, succeed otherwise
        cmd = (
            f'c=$(cat {counter}); echo $((c+1)) > {counter}; '
            f'test $((c+1)) -gt 1'
        )
        content = f'command: "{cmd}"\npolicy:\n  retries: 1\n'
        recipe, executions, run_hash, run_dir, m = self._setup(tmp_path, content)
        m = execute_run(recipe, executions, run_hash, run_dir, m, _manifest.write)
        assert m["state"]["counts"]["completed"] == 1
        assert m["executions"][0]["retry_count"] == 1

    def test_timeout(self, tmp_path):
        content = (
            "command: 'sleep 10'\n"
            "policy:\n"
            "  timeout-per-run: 1s\n"
        )
        recipe, executions, run_hash, run_dir, m = self._setup(tmp_path, content)
        m = execute_run(recipe, executions, run_hash, run_dir, m, _manifest.write)
        assert m["state"]["counts"]["failed"] == 1
        assert m["executions"][0]["failure_reason"] == "timeout"

    def test_param_context_in_command(self, tmp_path):
        out_file = tmp_path / "out.txt"
        content = (
            f'command: "echo {{{{ param.x }}}} > {out_file}"\n'
            "parameters:\n"
            "  values:\n"
            "    x: [42]\n"
        )
        recipe, executions, run_hash, run_dir, m = self._setup(tmp_path, content)
        execute_run(recipe, executions, run_hash, run_dir, m, _manifest.write)
        assert out_file.read_text().strip() == "42"

    def test_run_context_in_command(self, tmp_path):
        content = (
            'command: "echo {{ run.exec_id }}"\n'
            "parameters:\n"
            "  values:\n"
            "    x: [1, 2]\n"
        )
        recipe, executions, run_hash, run_dir, m = self._setup(tmp_path, content)
        m = execute_run(recipe, executions, run_hash, run_dir, m, _manifest.write)
        assert m["executions"][0]["exec_id"] == 1
        assert m["executions"][1]["exec_id"] == 2

    def test_post_commands(self, tmp_path):
        out_file = tmp_path / "post.txt"
        content = (
            'command: "echo main"\n'
            f'post:\n  - "echo post > {out_file}"\n'
        )
        recipe, executions, run_hash, run_dir, m = self._setup(tmp_path, content)
        m = execute_run(recipe, executions, run_hash, run_dir, m, _manifest.write)
        m = execute_post(recipe, run_hash, run_dir, m, _manifest.write)
        assert m["post"][0]["status"] == "completed"
        assert out_file.read_text().strip() == "post"

    def test_multi_step_command(self, tmp_path):
        out1 = tmp_path / "step1.txt"
        out2 = tmp_path / "step2.txt"
        content = (
            f'command:\n'
            f'  - "echo step1 > {out1}"\n'
            f'  - "echo step2 > {out2}"\n'
        )
        recipe, executions, run_hash, run_dir, m = self._setup(tmp_path, content)
        execute_run(recipe, executions, run_hash, run_dir, m, _manifest.write)
        assert out1.read_text().strip() == "step1"
        assert out2.read_text().strip() == "step2"

    def test_manifest_counts_track_correctly(self, tmp_path):
        content = (
            "command: 'echo x'\n"
            "parameters:\n"
            "  values:\n"
            "    x: [1, 2, 3]\n"
        )
        recipe, executions, run_hash, run_dir, m = self._setup(tmp_path, content)
        m = execute_run(recipe, executions, run_hash, run_dir, m, _manifest.write)
        counts = m["state"]["counts"]
        assert counts["completed"] == 3
        assert counts["failed"] == 0
        assert counts["running"] == 0
        assert counts["pending"] == 0


# ---------------------------------------------------------------------------
# ygor run CLI integration
# ---------------------------------------------------------------------------

class TestYgorRun:
    def test_minimal_recipe(self, tmp_path):
        recipe_file = _write_recipe(tmp_path, 'command: "echo hello"\n')
        result = _invoke_run(recipe_file, tmp_path / "store")
        assert result.exit_code == 0
        m = _load_manifest(tmp_path / "store")
        assert m["state"]["status"] == "completed"
        assert m["state"]["counts"]["completed"] == 1

    def test_creates_store_structure(self, tmp_path):
        recipe_file = _write_recipe(tmp_path, 'command: "echo x"\n')
        _invoke_run(recipe_file, tmp_path / "store")
        runs = list((tmp_path / "store" / "runs").iterdir())
        run_dir = runs[0]
        assert (run_dir / "manifest.json").exists()
        assert (run_dir / "recipe.lock.yaml").exists()
        assert (run_dir / "outputs").is_dir()
        assert (run_dir / "logs").is_dir()

    def test_lock_file_removed_after_run(self, tmp_path):
        recipe_file = _write_recipe(tmp_path, 'command: "echo x"\n')
        _invoke_run(recipe_file, tmp_path / "store")
        runs = list((tmp_path / "store" / "runs").iterdir())
        assert not (runs[0] / ".experiment" / "lock").exists()

    def test_manifest_has_host_info(self, tmp_path):
        recipe_file = _write_recipe(tmp_path, 'command: "echo x"\n')
        _invoke_run(recipe_file, tmp_path / "store")
        m = _load_manifest(tmp_path / "store")
        assert "hostname" in m["host"]
        assert "cpu_count_logical" in m["host"]

    def test_with_parameters(self, tmp_path):
        content = (
            "command: 'echo {{ param.n }}'\n"
            "parameters:\n"
            "  values:\n"
            "    n: [1, 2, 3]\n"
        )
        recipe_file = _write_recipe(tmp_path, content)
        _invoke_run(recipe_file, tmp_path / "store")
        m = _load_manifest(tmp_path / "store")
        assert m["state"]["counts"]["completed"] == 3
        assert len(m["executions"]) == 3

    def test_manifest_freezes_execution_plan(self, tmp_path):
        content = (
            "command: 'echo {{ param.n }} {{ run.seed }}'\n"
            "parameters:\n"
            "  values:\n"
            "    n: [1, 2]\n"
            "  seeds:\n"
            "    count: 2\n"
            "    meta-seed: 0\n"
        )
        recipe_file = _write_recipe(tmp_path, content)
        _invoke_run(recipe_file, tmp_path / "store")
        m = _load_manifest(tmp_path / "store")

        frozen = m["plan"]["executions"]
        assert len(frozen) == 4
        assert [e["exec_id"] for e in frozen] == [1, 2, 3, 4]
        assert {e["params"]["n"] for e in frozen} == {1, 2}
        assert {e["seed"] for e in frozen} == set(m["seeds"]["values"])

    def test_with_seeds(self, tmp_path):
        content = (
            "command: 'echo {{ run.seed }}'\n"
            "parameters:\n"
            "  values:\n"
            "    x: [1, 2]\n"
            "  seeds:\n"
            "    count: 3\n"
            "    meta-seed: 0\n"
        )
        recipe_file = _write_recipe(tmp_path, content)
        _invoke_run(recipe_file, tmp_path / "store")
        m = _load_manifest(tmp_path / "store")
        # 2 param tuples × 3 seeds = 6 executions
        assert m["state"]["counts"]["completed"] == 6
        assert m["seeds"] is not None
        assert m["seeds"]["count"] == 3
        assert len(m["seeds"]["values"]) == 3

    def test_recipe_path_in_manifest(self, tmp_path):
        recipe_file = _write_recipe(tmp_path, 'command: "echo x"\n')
        _invoke_run(recipe_file, tmp_path / "store")
        m = _load_manifest(tmp_path / "store")
        assert "recipe.yaml" in m["recipe_path"]

    def test_invalid_recipe_exits_1(self, tmp_path):
        bad = tmp_path / "recipe.yaml"
        bad.write_text("not_a_field: x\n")
        result = _invoke_run(bad, tmp_path / "store")
        assert result.exit_code == 1

    def test_failing_command_continues_by_default(self, tmp_path):
        content = (
            "command: 'false'\n"
            "parameters:\n"
            "  values:\n"
            "    x: [1, 2]\n"
        )
        recipe_file = _write_recipe(tmp_path, content)
        _invoke_run(recipe_file, tmp_path / "store")
        m = _load_manifest(tmp_path / "store")
        assert len(m["executions"]) == 2
        assert m["state"]["counts"]["failed"] == 2
