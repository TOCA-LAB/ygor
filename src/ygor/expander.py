"""Expand recipe parameters into a concrete execution plan."""

from __future__ import annotations

import itertools
import math
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from ygor.schema import Parameters, ParameterStrategy, RangeSpec, Recipe, SeedsConfig

SEEDS_ALGORITHM = "uniform_int32_via_numpy"


# ---------------------------------------------------------------------------
# Range expansion
# ---------------------------------------------------------------------------

def _coerce(v: float) -> int | float:
    """Return int if v is a whole number (within floating-point tolerance)."""
    i = round(v)
    return i if abs(v - i) < 1e-9 else v


def expand_range(spec: RangeSpec) -> list[int | float]:
    """Expand a RangeSpec into a list of concrete values.

    Values that are mathematically integers (e.g. from an all-integer step spec)
    are returned as ``int``, not ``float``, so templates render ``10`` not ``10.0``.
    """
    if spec.step is not None:
        values: list[int | float] = []
        v = spec.from_
        epsilon = abs(spec.step) * 1e-9
        while v <= spec.to + epsilon:
            values.append(_coerce(round(v, 12)))
            v += spec.step
        return values

    count = spec.count
    scale = spec.scale or "linear"
    if scale == "log":
        if spec.from_ <= 0:
            raise ValueError("log scale requires 'from' > 0")
        arr = np.logspace(math.log10(spec.from_), math.log10(spec.to), count)
    else:
        arr = np.linspace(spec.from_, spec.to, count)
    return [_coerce(v) for v in arr.tolist()]


def _expand_dim(val: list[Any] | RangeSpec) -> list[Any]:
    if isinstance(val, RangeSpec):
        return expand_range(val)
    return list(val)


# ---------------------------------------------------------------------------
# Seed generation
# ---------------------------------------------------------------------------

def generate_seeds(config: SeedsConfig) -> list[int]:
    """Generate deterministic seeds from meta-seed using the canonical algorithm."""
    rng = np.random.default_rng(config.meta_seed)
    return rng.integers(0, 2**32, size=config.count).tolist()


# ---------------------------------------------------------------------------
# Execution plan
# ---------------------------------------------------------------------------

@dataclass
class ExecutionPlan:
    """Expanded parameter tuples and seeds for a recipe run."""

    param_tuples: list[dict[str, Any]]
    seeds: list[int]
    seeds_algorithm: str = SEEDS_ALGORITHM
    # original dimensions, preserved for display
    dim_sizes: dict[str, int] = field(default_factory=dict)

    @property
    def n_param_tuples(self) -> int:
        return len(self.param_tuples)

    @property
    def n_seeds(self) -> int:
        return max(len(self.seeds), 1)

    @property
    def total_executions(self) -> int:
        return self.n_param_tuples * self.n_seeds


def build_plan(
    recipe: Recipe,
    *,
    instance_values: list[str] | None = None,
) -> ExecutionPlan:
    """Build an :class:`ExecutionPlan` from a validated :class:`Recipe`.

    *instance_values* overrides the raw ``parameters.instance`` patterns with
    already-resolved file paths (e.g., from :func:`datasets.resolve_instance_globs`).
    When omitted, raw patterns are used as-is.
    """
    params = recipe.parameters

    # Determine concrete instance values
    inst_vals: list[str] | None = None
    if params is not None and params.instance is not None:
        inst_vals = instance_values if instance_values is not None else params.instance

    if params is None or (not params.values and inst_vals is None):
        seeds = generate_seeds(params.seeds) if (params and params.seeds) else []
        return ExecutionPlan(param_tuples=[{}], seeds=seeds, dim_sizes={})

    dim_sizes: dict[str, int] = {}

    if params.values:
        expanded: dict[str, list[Any]] = {
            key: _expand_dim(val) for key, val in params.values.items()
        }
        dim_sizes = {k: len(v) for k, v in expanded.items()}
        keys = list(expanded.keys())
        lists = [expanded[k] for k in keys]

        if params.strategy == ParameterStrategy.cartesian:
            tuples = [dict(zip(keys, combo)) for combo in itertools.product(*lists)]

        elif params.strategy == ParameterStrategy.zip:
            lengths = {k: len(expanded[k]) for k in keys}
            if len(set(lengths.values())) > 1:
                raise ValueError(
                    f"'zip' strategy requires all dimensions to have the same length; "
                    f"got: {lengths}"
                )
            tuples = [dict(zip(keys, combo)) for combo in zip(*lists)]

        elif params.strategy == ParameterStrategy.sample:
            all_combos = list(itertools.product(*lists))
            meta_seed = params.seeds.meta_seed if params.seeds else 0
            rng = np.random.default_rng(meta_seed)
            size = min(params.sample_size, len(all_combos))
            indices = rng.choice(len(all_combos), size=size, replace=False)
            tuples = [dict(zip(keys, all_combos[i])) for i in sorted(indices)]

        else:
            raise ValueError(f"Unknown strategy: {params.strategy!r}")
    else:
        tuples = [{}]

    # Cross-product with instance dimension (always cartesian regardless of strategy)
    if inst_vals is not None:
        tuples = [
            {**pt, "instance": inst}
            for pt in tuples
            for inst in inst_vals
        ]
        dim_sizes["instance"] = len(inst_vals)

    seeds = generate_seeds(params.seeds) if params.seeds else []
    return ExecutionPlan(param_tuples=tuples, seeds=seeds, dim_sizes=dim_sizes)
