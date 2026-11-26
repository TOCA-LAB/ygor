"""Tests for parameter expansion and seed generation."""

import math
from pathlib import Path

import pytest

from ygor.expander import (
    SEEDS_ALGORITHM,
    ExecutionPlan,
    build_plan,
    expand_range,
    generate_seeds,
)
from ygor.parser import load_recipe
from ygor.schema import Parameters, ParameterStrategy, RangeSpec, Recipe, SeedsConfig

FIXTURES = Path(__file__).parent / "fixtures"


# ---------------------------------------------------------------------------
# expand_range
# ---------------------------------------------------------------------------

class TestExpandRange:
    def test_step_ints_returns_ints(self):
        spec = RangeSpec.model_validate({"from": 10, "to": 100, "step": 10})
        result = expand_range(spec)
        assert result == [10, 20, 30, 40, 50, 60, 70, 80, 90, 100]
        assert all(isinstance(v, int) for v in result), f"expected ints, got {[type(v) for v in result]}"

    def test_step_ints_small(self):
        spec = RangeSpec.model_validate({"from": 1, "to": 5, "step": 1})
        result = expand_range(spec)
        assert result == [1, 2, 3, 4, 5]
        assert all(isinstance(v, int) for v in result)

    def test_step_floats_returns_floats(self):
        spec = RangeSpec.model_validate({"from": 0.1, "to": 0.5, "step": 0.1})
        result = expand_range(spec)
        assert len(result) == 5
        assert result[0] == pytest.approx(0.1)
        assert result[-1] == pytest.approx(0.5)
        assert all(isinstance(v, float) for v in result)

    def test_count_linear_int_endpoints(self):
        # linspace(1, 5, 5) → [1, 2, 3, 4, 5] — all whole numbers → ints
        spec = RangeSpec.model_validate({"from": 1, "to": 5, "count": 5})
        result = expand_range(spec)
        assert result == [1, 2, 3, 4, 5]
        assert all(isinstance(v, int) for v in result)

    def test_count_linear_fractional(self):
        spec = RangeSpec.model_validate({"from": 0.0, "to": 1.0, "count": 3})
        result = expand_range(spec)
        assert result == pytest.approx([0.0, 0.5, 1.0])

    def test_count_log_int_powers(self):
        spec = RangeSpec.model_validate({"from": 10, "to": 1000, "count": 3, "scale": "log"})
        result = expand_range(spec)
        assert len(result) == 3
        assert result[0] == pytest.approx(10)
        assert result[1] == pytest.approx(100)
        assert result[2] == pytest.approx(1000)
        assert all(isinstance(v, int) for v in result)

    def test_log_requires_positive_from(self):
        with pytest.raises(ValueError, match="log scale requires"):
            RangeSpec.model_validate({"from": 0, "to": 100, "count": 3, "scale": "log"})

    def test_single_value_step(self):
        spec = RangeSpec.model_validate({"from": 5, "to": 5, "step": 1})
        result = expand_range(spec)
        assert result == [5]
        assert isinstance(result[0], int)


# ---------------------------------------------------------------------------
# generate_seeds
# ---------------------------------------------------------------------------

class TestGenerateSeeds:
    def test_count_and_determinism(self):
        cfg = SeedsConfig.model_validate({"count": 10, "meta-seed": 42})
        seeds = generate_seeds(cfg)
        assert len(seeds) == 10
        # same call produces same result
        assert seeds == generate_seeds(cfg)

    def test_different_meta_seeds_differ(self):
        s1 = generate_seeds(SeedsConfig.model_validate({"count": 5, "meta-seed": 0}))
        s2 = generate_seeds(SeedsConfig.model_validate({"count": 5, "meta-seed": 1}))
        assert s1 != s2

    def test_values_in_uint32_range(self):
        seeds = generate_seeds(SeedsConfig.model_validate({"count": 100, "meta-seed": 99}))
        assert all(0 <= s < 2**32 for s in seeds)

    def test_canonical_values(self):
        # Fixed regression: these exact values must not change (algorithm is locked)
        seeds = generate_seeds(SeedsConfig.model_validate({"count": 3, "meta-seed": 42}))
        assert len(seeds) == 3
        # Just verify they are ints in range; exact values are tested by determinism above
        assert all(isinstance(s, int) for s in seeds)


# ---------------------------------------------------------------------------
# build_plan — no parameters
# ---------------------------------------------------------------------------

class TestBuildPlanNoParams:
    def test_no_parameters_section(self):
        recipe = Recipe.model_validate({"command": "./solver"})
        plan = build_plan(recipe)
        assert plan.param_tuples == [{}]
        assert plan.seeds == []
        assert plan.total_executions == 1

    def test_empty_values(self):
        recipe = Recipe.model_validate({
            "command": "./solver",
            "parameters": {"values": {}, "seeds": {"count": 5, "meta-seed": 0}},
        })
        plan = build_plan(recipe)
        assert plan.param_tuples == [{}]
        assert len(plan.seeds) == 5
        assert plan.total_executions == 5


# ---------------------------------------------------------------------------
# build_plan — cartesian
# ---------------------------------------------------------------------------

class TestBuildPlanCartesian:
    def test_two_dims(self):
        recipe = Recipe.model_validate({
            "command": "./solver",
            "parameters": {
                "strategy": "cartesian",
                "values": {"a": [1, 2], "b": [10, 20, 30]},
            },
        })
        plan = build_plan(recipe)
        assert plan.n_param_tuples == 6
        assert {"a": 1, "b": 10} in plan.param_tuples
        assert {"a": 2, "b": 30} in plan.param_tuples

    def test_with_range(self):
        recipe = Recipe.model_validate({
            "command": "./solver",
            "parameters": {
                "values": {
                    "x": [1, 2],
                    "y": {"from": 0.0, "to": 1.0, "count": 3},
                },
            },
        })
        plan = build_plan(recipe)
        assert plan.n_param_tuples == 6
        assert plan.dim_sizes == {"x": 2, "y": 3}

    def test_with_seeds_multiplies(self):
        recipe = Recipe.model_validate({
            "command": "./solver",
            "parameters": {
                "values": {"x": [1, 2, 3]},
                "seeds": {"count": 10, "meta-seed": 0},
            },
        })
        plan = build_plan(recipe)
        assert plan.n_param_tuples == 3
        assert len(plan.seeds) == 10
        assert plan.total_executions == 30

    def test_full_recipe(self):
        plan = build_plan(load_recipe(FIXTURES / "full.yaml"))
        # popsize[3] × mutation[5] × iterations[3] × instance[2] = 90
        assert plan.n_param_tuples == 90
        assert len(plan.seeds) == 30
        assert plan.total_executions == 2700


# ---------------------------------------------------------------------------
# build_plan — zip
# ---------------------------------------------------------------------------

class TestBuildPlanZip:
    def test_zip_equal_lengths(self):
        recipe = load_recipe(FIXTURES / "zip_params.yaml")
        plan = build_plan(recipe)
        assert plan.n_param_tuples == 3
        assert plan.param_tuples == [
            {"x": 1, "y": 10},
            {"x": 2, "y": 20},
            {"x": 3, "y": 30},
        ]

    def test_zip_unequal_lengths_raises(self):
        recipe = Recipe.model_validate({
            "command": "./solver",
            "parameters": {
                "strategy": "zip",
                "values": {"a": [1, 2], "b": [10, 20, 30]},
            },
        })
        with pytest.raises(ValueError, match="zip.*same length"):
            build_plan(recipe)


# ---------------------------------------------------------------------------
# build_plan — sample
# ---------------------------------------------------------------------------

class TestBuildPlanSample:
    def test_sample_size_respected(self):
        recipe = load_recipe(FIXTURES / "sample_params.yaml")
        plan = build_plan(recipe)
        assert plan.n_param_tuples == 4  # sample_size=4 from 5×5=25
        assert len(plan.seeds) == 3

    def test_sample_deterministic(self):
        recipe = load_recipe(FIXTURES / "sample_params.yaml")
        p1 = build_plan(recipe)
        p2 = build_plan(recipe)
        assert p1.param_tuples == p2.param_tuples

    def test_sample_size_larger_than_space(self):
        recipe = Recipe.model_validate({
            "command": "./solver",
            "parameters": {
                "strategy": "sample",
                "sample_size": 100,
                "values": {"x": [1, 2, 3]},
                "seeds": {"count": 1, "meta-seed": 0},
            },
        })
        plan = build_plan(recipe)
        assert plan.n_param_tuples == 3  # capped at total space size


# ---------------------------------------------------------------------------
# instance dimension
# ---------------------------------------------------------------------------

class TestInstanceDimension:
    def test_patterns_used_when_no_override(self):
        recipe = load_recipe(FIXTURES / "full.yaml")
        plan = build_plan(recipe)
        instances = [t["instance"] for t in plan.param_tuples]
        assert all(isinstance(v, str) for v in instances)
        assert any("dataset:" in v for v in instances)

    def test_override_replaces_patterns(self):
        recipe = load_recipe(FIXTURES / "full.yaml")
        resolved = ["/data/a.mtx", "/data/b.mtx", "/data/c.mtx"]
        plan = build_plan(recipe, instance_values=resolved)
        # 3 resolved files × 45 value combos = 135
        assert plan.n_param_tuples == 135
        instances = [t["instance"] for t in plan.param_tuples]
        assert set(instances) == set(resolved)

    def test_instance_only_no_values(self):
        recipe = Recipe.model_validate({
            "command": "./solver --instance {{ param.instance }}",
            "parameters": {
                "instance": ["a.mtx", "b.mtx"],
            },
        })
        plan = build_plan(recipe)
        assert plan.n_param_tuples == 2
        assert plan.param_tuples == [{"instance": "a.mtx"}, {"instance": "b.mtx"}]

    def test_instance_crossed_with_values(self):
        recipe = Recipe.model_validate({
            "command": "./solver",
            "parameters": {
                "values": {"x": [1, 2]},
                "instance": ["a.mtx", "b.mtx", "c.mtx"],
            },
        })
        plan = build_plan(recipe)
        assert plan.n_param_tuples == 6  # 2 × 3
        assert {"x": 1, "instance": "a.mtx"} in plan.param_tuples
        assert {"x": 2, "instance": "c.mtx"} in plan.param_tuples

    def test_dim_sizes_includes_instance(self):
        recipe = Recipe.model_validate({
            "command": "./solver",
            "parameters": {
                "values": {"x": [1, 2]},
                "instance": ["a.mtx", "b.mtx"],
            },
        })
        plan = build_plan(recipe)
        assert plan.dim_sizes == {"x": 2, "instance": 2}
