"""Pure helpers for preparing and finalising experiment runs."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ygor import manifest as manifest_mod
from ygor import store
from ygor.expander import ExecutionPlan
from ygor.schema import Recipe


@dataclass(frozen=True)
class RunIdentity:
    run_hash: str
    run_dir: Path
    started_at: str


def executions_from_plan(plan: ExecutionPlan) -> list[tuple[int, dict[str, Any], int]]:
    """Return stable ``(exec_id, params, seed)`` triples from a built plan."""
    if plan.seeds:
        raw_pairs = [(params, seed) for params in plan.param_tuples for seed in plan.seeds]
    else:
        raw_pairs = [(params, 0) for params in plan.param_tuples]
    return [(index + 1, params, seed) for index, (params, seed) in enumerate(raw_pairs)]


def build_run_identity(
    *,
    recipe_text: str,
    recipe: Recipe,
    store_root: Path,
    started_at: str | None = None,
) -> RunIdentity:
    """Compute run hash and directory using the current hashing contract."""
    started_at = started_at or manifest_mod._now_iso()
    meta_seed = (
        recipe.parameters.seeds.meta_seed
        if recipe.parameters and recipe.parameters.seeds
        else None
    )
    run_hash = store.compute_run_hash(recipe_text, meta_seed, started_at)
    return RunIdentity(
        run_hash=run_hash,
        run_dir=store.run_dir_path(store_root, run_hash),
        started_at=started_at,
    )


def seeds_info_from_plan(recipe: Recipe, plan: ExecutionPlan) -> dict[str, Any] | None:
    """Return manifest seed metadata for a plan, if seeds are enabled."""
    if not plan.seeds:
        return None
    seeds_cfg = recipe.parameters.seeds  # type: ignore[union-attr]
    return {
        "meta_seed": seeds_cfg.meta_seed,
        "count": seeds_cfg.count,
        "algorithm": manifest_mod.SEEDS_ALGORITHM,
        "values": plan.seeds,
    }


def finalize_manifest_status(manifest: dict[str, Any]) -> str:
    """Apply the final status rule used by run/resume and return the status."""
    final_status = manifest["state"]["status"]
    failed_count = manifest["state"]["counts"]["failed"]
    if final_status not in ("failed", "paused"):
        manifest["state"]["status"] = "failed" if failed_count > 0 else "completed"
        final_status = manifest["state"]["status"]
    if final_status != "paused":
        manifest["state"]["ended_at"] = manifest_mod._now_iso()
    return final_status
