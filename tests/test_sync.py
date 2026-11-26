"""Tests for `ygor publish` and `ygor sync`."""

from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from ygor.cli import app


runner = CliRunner()


def _write_run(store: Path, run_hash: str, *, name: str = "exp") -> Path:
    run_dir = store / "runs" / run_hash
    (run_dir / "outputs" / "exec_0001").mkdir(parents=True, exist_ok=True)
    (run_dir / "streams" / "exec_0001").mkdir(parents=True, exist_ok=True)
    (run_dir / "logs").mkdir(parents=True, exist_ok=True)
    (run_dir / ".experiment").mkdir(parents=True, exist_ok=True)
    (run_dir / "outputs" / "exec_0001" / "out.txt").write_text("result", encoding="utf-8")
    (run_dir / "streams" / "exec_0001" / "stdout.log").write_text("stdout", encoding="utf-8")
    (run_dir / "logs" / "exec_0001.log").write_text("log", encoding="utf-8")
    (run_dir / ".experiment" / "lock").write_text("internal", encoding="utf-8")
    (run_dir / "recipe.lock.yaml").write_text('command: "echo x"\n', encoding="utf-8")
    manifest = {
        "manifest_version": 1,
        "run_hash": run_hash,
        "recipe_path": f"recipes/{name}/recipe.yaml",
        "recipe": {"meta": {"name": name}, "command": ["echo x"]},
        "state": {
            "status": "completed",
            "started_at": "2026-01-01T00:00:00+00:00",
            "ended_at": "2026-01-01T00:00:01+00:00",
            "last_updated": "2026-01-01T00:00:01+00:00",
            "total_executions": 1,
            "counts": {"completed": 1, "failed": 0, "running": 0, "pending": 0},
        },
        "executions": [],
        "post": [],
    }
    (run_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return run_dir


def _publish_index(store: Path, run_hash: str) -> None:
    store.mkdir(parents=True, exist_ok=True)
    (store / "publications.json").write_text(
        json.dumps({"version": 1, "runs": {run_hash: {"published": True}}}),
        encoding="utf-8",
    )


class TestPublish:
    def test_publish_marks_run(self, tmp_path):
        store = tmp_path / "store"
        run_hash = "a" * 64
        _write_run(store, run_hash, name="my-exp")

        result = runner.invoke(app, ["publish", run_hash[:8], "--store", str(store)])

        assert result.exit_code == 0, result.output
        data = json.loads((store / "publications.json").read_text(encoding="utf-8"))
        assert data["runs"][run_hash]["published"] is True
        assert data["runs"][run_hash]["recipe"] == "my-exp"

    def test_unpublish_unmarks_run(self, tmp_path):
        store = tmp_path / "store"
        run_hash = "a" * 64
        _write_run(store, run_hash)
        _publish_index(store, run_hash)

        result = runner.invoke(app, ["unpublish", run_hash[:8], "--store", str(store)])

        assert result.exit_code == 0, result.output
        data = json.loads((store / "publications.json").read_text(encoding="utf-8"))
        assert run_hash not in data["runs"]


class TestSync:
    def test_sync_pushes_published_run_to_local_remote(self, tmp_path):
        store = tmp_path / "student"
        remote = tmp_path / "remote"
        run_hash = "b" * 64
        _write_run(store, run_hash)
        _publish_index(store, run_hash)

        result = runner.invoke(app, ["sync", "--store", str(store), "--remote", str(remote)])

        assert result.exit_code == 0, result.output
        assert (remote / "runs" / run_hash / "manifest.json").exists()
        assert (remote / "runs" / run_hash / "outputs" / "exec_0001" / "out.txt").exists()
        assert not (remote / "runs" / run_hash / ".experiment" / "lock").exists()
        remote_index = json.loads((remote / "publications.json").read_text(encoding="utf-8"))
        assert run_hash in remote_index["runs"]

    def test_sync_pulls_remote_published_run(self, tmp_path):
        store = tmp_path / "advisor"
        remote = tmp_path / "remote"
        run_hash = "c" * 64
        _write_run(remote, run_hash)
        _publish_index(remote, run_hash)

        result = runner.invoke(app, ["sync", "--store", str(store), "--remote", str(remote)])

        assert result.exit_code == 0, result.output
        assert (store / "runs" / run_hash / "manifest.json").exists()
        assert (store / "runs" / run_hash / "outputs" / "exec_0001" / "out.txt").exists()
        local_index = json.loads((store / "publications.json").read_text(encoding="utf-8"))
        assert run_hash in local_index["runs"]

    def test_sync_dry_run_does_not_copy(self, tmp_path):
        store = tmp_path / "student"
        remote = tmp_path / "remote"
        run_hash = "d" * 64
        _write_run(store, run_hash)
        _publish_index(store, run_hash)

        result = runner.invoke(app, ["sync", "--dry-run", "--store", str(store), "--remote", str(remote)])

        assert result.exit_code == 0, result.output
        assert "Dry run" in result.output
        assert not (remote / "runs" / run_hash).exists()
        assert not (remote / "publications.json").exists()

    def test_sync_uses_configured_local_remote(self, tmp_path, monkeypatch):
        work = tmp_path / "work"
        work.mkdir()
        store = work / "store"
        remote = work / "shared"
        run_hash = "e" * 64
        _write_run(store, run_hash)
        _publish_index(store, run_hash)
        (work / ".ygorrc.yaml").write_text(
            "sync:\n"
            f"  remote: {remote}\n",
            encoding="utf-8",
        )
        monkeypatch.chdir(work)

        result = runner.invoke(app, ["sync", "--store", str(store)])

        assert result.exit_code == 0, result.output
        assert (remote / "runs" / run_hash / "manifest.json").exists()
