"""Tests for run preparation helpers."""

from __future__ import annotations

from pathlib import Path

from ygor.expander import ExecutionPlan
from ygor.run_service import (
    build_run_identity,
    executions_from_plan,
    finalize_manifest_status,
    seeds_info_from_plan,
)
from ygor.schema import Recipe


def test_executions_from_plan_without_seeds():
    plan = ExecutionPlan(param_tuples=[{"x": 1}, {"x": 2}], seeds=[])
    assert executions_from_plan(plan) == [
        (1, {"x": 1}, 0),
        (2, {"x": 2}, 0),
    ]


def test_executions_from_plan_with_seeds():
    plan = ExecutionPlan(param_tuples=[{"x": 1}, {"x": 2}], seeds=[10, 20])
    assert executions_from_plan(plan) == [
        (1, {"x": 1}, 10),
        (2, {"x": 1}, 20),
        (3, {"x": 2}, 10),
        (4, {"x": 2}, 20),
    ]


def test_build_run_identity_uses_meta_seed(tmp_path: Path):
    recipe = Recipe.model_validate({
        "command": "echo",
        "parameters": {"seeds": {"count": 1, "meta-seed": 42}},
    })
    identity = build_run_identity(
        recipe_text="command: echo\n",
        recipe=recipe,
        store_root=tmp_path,
        started_at="2026-01-01T00:00:00+00:00",
    )
    assert identity.run_dir == tmp_path / "runs" / identity.run_hash
    assert len(identity.run_hash) == 64


def test_seeds_info_from_plan():
    recipe = Recipe.model_validate({
        "command": "echo",
        "parameters": {"seeds": {"count": 2, "meta-seed": 42}},
    })
    plan = ExecutionPlan(param_tuples=[{}], seeds=[123, 456])
    info = seeds_info_from_plan(recipe, plan)
    assert info is not None
    assert info["meta_seed"] == 42
    assert info["values"] == [123, 456]


def test_finalize_manifest_status_completed():
    manifest = {
        "state": {
            "status": "running",
            "counts": {"failed": 0},
            "ended_at": None,
        },
    }
    assert finalize_manifest_status(manifest) == "completed"
    assert manifest["state"]["status"] == "completed"
    assert manifest["state"]["ended_at"] is not None


def test_finalize_manifest_status_paused_keeps_open():
    manifest = {
        "state": {
            "status": "paused",
            "counts": {"failed": 0},
            "ended_at": None,
        },
    }
    assert finalize_manifest_status(manifest) == "paused"
    assert manifest["state"]["ended_at"] is None
