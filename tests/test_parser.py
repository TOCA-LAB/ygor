"""Tests for the YAML recipe parser."""

from pathlib import Path

import pytest

from ygor.parser import RecipeParseError, load_recipe
from ygor.schema import ParameterStrategy, Recipe

FIXTURES = Path(__file__).parent / "fixtures"


class TestLoadRecipe:
    def test_minimal(self):
        r = load_recipe(FIXTURES / "minimal.yaml")
        assert isinstance(r, Recipe)
        assert r.command == ["./solver --input data.txt"]

    def test_full(self):
        r = load_recipe(FIXTURES / "full.yaml")
        assert r.meta.name == "ils-popsize-sweep"
        assert "dimacs-small" in r.datasets
        assert r.environment.base_image == "ubuntu@sha256:8f1d7e"
        assert r.parameters.strategy == ParameterStrategy.cartesian
        assert r.parameters.seeds.count == 30
        assert r.parameters.seeds.meta_seed == 42
        assert r.policy.workers == 16
        assert r.policy.timeout_per_run == "30m"
        assert len(r.post) == 1

    def test_store_source_recipe(self):
        r = load_recipe(FIXTURES / "store_source.yaml")
        assert r.datasets["popsize_baseline"].hash == "a3f2d8c1"

    def test_file_not_found(self):
        with pytest.raises(RecipeParseError, match="file not found"):
            load_recipe(FIXTURES / "nonexistent.yaml")

    def test_invalid_no_command(self):
        with pytest.raises(RecipeParseError, match="validation failed"):
            load_recipe(FIXTURES / "invalid_no_command.yaml")

    def test_invalid_extra_field(self):
        with pytest.raises(RecipeParseError, match="validation failed"):
            load_recipe(FIXTURES / "invalid_extra_field.yaml")

    def test_not_a_mapping(self, tmp_path):
        bad = tmp_path / "bad.yaml"
        bad.write_text("- item1\n- item2\n")
        with pytest.raises(RecipeParseError, match="YAML mapping"):
            load_recipe(bad)

    def test_broken_yaml(self, tmp_path):
        bad = tmp_path / "broken.yaml"
        bad.write_text("command: [\nunclosed bracket\n")
        with pytest.raises(RecipeParseError, match="YAML parse error"):
            load_recipe(bad)
