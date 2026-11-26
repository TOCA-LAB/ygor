"""Tests for recipe schema models."""

import pytest
from pydantic import ValidationError

from ygor.schema import (
    Dataset,
    Environment,
    GitSource,
    HttpSource,
    LocalSource,
    OnFailure,
    ParameterStrategy,
    Parameters,
    Policy,
    RangeSpec,
    Recipe,
    RecipeMeta,
    SeedsConfig,
    StoreSource,
    SyncConfig,
)


# ---------------------------------------------------------------------------
# Recipe — minimum valid
# ---------------------------------------------------------------------------

class TestRecipeMinimal:
    def test_command_string(self):
        r = Recipe.model_validate({"command": "./solver"})
        assert r.command == ["./solver"]

    def test_command_list(self):
        r = Recipe.model_validate({"command": ["./step1", "./step2"]})
        assert len(r.command) == 2

    def test_missing_command_raises(self):
        with pytest.raises(ValidationError):
            Recipe.model_validate({"meta": {"name": "x"}})

    def test_empty_command_raises(self):
        with pytest.raises(ValidationError):
            Recipe.model_validate({"command": []})

    def test_extra_field_raises(self):
        with pytest.raises(ValidationError):
            Recipe.model_validate({"command": "./solver", "bogus": True})

    def test_defaults(self):
        r = Recipe.model_validate({"command": "./solver"})
        assert r.meta is None
        assert r.datasets == {}
        assert r.environment is None
        assert r.parameters is None
        assert r.post == []
        assert r.sync is None
        assert r.policy.on_failure == OnFailure.continue_
        assert r.policy.retries == 0


# ---------------------------------------------------------------------------
# RecipeMeta
# ---------------------------------------------------------------------------

class TestRecipeMeta:
    def test_all_fields(self):
        m = RecipeMeta.model_validate(
            {"name": "exp", "description": "desc", "author": "a@b.com", "created": "2026-05-14"}
        )
        assert m.name == "exp"
        assert str(m.created) == "2026-05-14"

    def test_only_name(self):
        m = RecipeMeta.model_validate({"name": "x"})
        assert m.description is None


# ---------------------------------------------------------------------------
# Datasets / Sources
# ---------------------------------------------------------------------------

class TestSources:
    def test_http_source(self):
        s = HttpSource.model_validate({"type": "http", "url": "https://example.com/f.tar.gz"})
        assert s.type == "http"

    def test_git_source(self):
        s = GitSource.model_validate({"type": "git", "url": "https://github.com/x/y.git", "ref": "abc123"})
        assert s.ref == "abc123"

    def test_local_source(self):
        s = LocalSource.model_validate({"type": "local", "path": "../../data/"})
        assert s.path == "../../data/"

    def test_store_source(self):
        s = StoreSource.model_validate({"type": "store"})
        assert s.type == "store"

    def test_store_source_with_hash(self):
        s = StoreSource.model_validate({"type": "store", "hash": "13c32451"})
        assert s.hash == "13c32451"


class TestDataset:
    def test_http_dataset(self):
        d = Dataset.model_validate(
            {"source": {"type": "http", "url": "https://x.org/f.tar.gz"}, "sha256": "abc", "extract": "tar.gz"}
        )
        assert d.extract == "tar.gz"

    def test_bz2_extract(self):
        d = Dataset.model_validate(
            {"source": {"type": "http", "url": "https://x.org/f.csv.bz2"}, "extract": "bz2"}
        )
        assert d.extract == "bz2"

    def test_rar_extract(self):
        d = Dataset.model_validate(
            {"source": {"type": "http", "url": "https://x.org/f.rar"}, "extract": "rar"}
        )
        assert d.extract == "rar"

    def test_invalid_extract(self):
        with pytest.raises(ValidationError, match="extract must be one of"):
            Dataset.model_validate(
                {"source": {"type": "http", "url": "https://x.org/f.7z"}, "extract": "7z"}
            )

    def test_git_with_include(self):
        d = Dataset.model_validate(
            {
                "source": {"type": "git", "url": "https://github.com/x/y.git", "ref": "main"},
                "include": ["**/*.mtx"],
            }
        )
        assert d.include == ["**/*.mtx"]


# ---------------------------------------------------------------------------
# Environment
# ---------------------------------------------------------------------------

class TestEnvironment:
    def test_base_image(self):
        e = Environment.model_validate({"base-image": "ubuntu@sha256:abc"})
        assert e.base_image == "ubuntu@sha256:abc"

    def test_mac_address(self):
        e = Environment.model_validate({"mac-address": "02:42:ac:11:00:02"})
        assert e.mac_address == "02:42:ac:11:00:02"

    def test_invalid_mac_address_raises(self):
        with pytest.raises(ValidationError):
            Environment.model_validate({"mac-address": "not-a-mac"})

    def test_variables(self):
        e = Environment.model_validate({"variables": {"CC": "gcc", "OMP_NUM_THREADS": 1}})
        assert e.variables["CC"] == "gcc"

    def test_resources(self):
        e = Environment.model_validate({"resources": {"cpu": 4, "memory": "8GB"}})
        assert e.resources is not None
        assert e.resources.cpu == 4
        assert e.resources.memory == "8GB"

    def test_invalid_resources_memory_raises(self):
        with pytest.raises(ValidationError):
            Environment.model_validate({"resources": {"memory": "8 gigabytes"}})

    def test_invalid_resources_cpu_raises(self):
        with pytest.raises(ValidationError):
            Environment.model_validate({"resources": {"cpu": 0}})

    def test_bindings(self):
        e = Environment.model_validate(
            {
                "bindings": {
                    "gurobi-license": {
                        "type": "file",
                        "target": "/run/ygor/bindings/gurobi.lic",
                        "env": {
                            "GRB_LICENSE_FILE": "/run/ygor/bindings/gurobi.lic",
                        },
                    },
                    "dataset-token": {
                        "type": "env",
                        "env": {"DATASET_TOKEN": "{{ binding.value }}"},
                    },
                }
            }
        )
        assert e.bindings["gurobi-license"].required is True
        assert e.bindings["gurobi-license"].readonly is True
        assert e.bindings["dataset-token"].type == "env"

    def test_image_build_run(self):
        e = Environment.model_validate(
            {
                "image-build": {
                    "run": [
                        "mkdir -p /opt/tools",
                        "ln -s /opt/tools/bin/tool /usr/local/bin/tool",
                    ],
                }
            }
        )
        assert e.image_build is not None
        assert e.image_build.run == [
            "mkdir -p /opt/tools",
            "ln -s /opt/tools/bin/tool /usr/local/bin/tool",
        ]

    def test_extra_field_raises(self):
        with pytest.raises(ValidationError):
            Environment.model_validate({"unknown": "x"})


# ---------------------------------------------------------------------------
# Parameters
# ---------------------------------------------------------------------------

class TestRangeSpec:
    def test_step(self):
        r = RangeSpec.model_validate({"from": 0.1, "to": 0.5, "step": 0.1})
        assert r.from_ == pytest.approx(0.1)
        assert r.step == pytest.approx(0.1)

    def test_count_with_scale(self):
        r = RangeSpec.model_validate({"from": 10, "to": 1000, "count": 3, "scale": "log"})
        assert r.count == 3
        assert r.scale == "log"

    def test_neither_step_nor_count_raises(self):
        with pytest.raises(ValidationError, match="step.*count|count.*step"):
            RangeSpec.model_validate({"from": 1, "to": 10})

    def test_both_step_and_count_raises(self):
        with pytest.raises(ValidationError):
            RangeSpec.model_validate({"from": 1, "to": 10, "step": 1, "count": 5})

    def test_step_must_be_positive(self):
        with pytest.raises(ValidationError, match="step"):
            RangeSpec.model_validate({"from": 1, "to": 10, "step": 0})

    def test_to_must_not_be_less_than_from(self):
        with pytest.raises(ValidationError, match="to"):
            RangeSpec.model_validate({"from": 10, "to": 1, "step": 1})

    def test_log_scale_requires_positive_bounds(self):
        with pytest.raises(ValidationError, match="log"):
            RangeSpec.model_validate({"from": 0, "to": 10, "count": 3, "scale": "log"})


class TestParameters:
    def test_cartesian_strategy(self):
        p = Parameters.model_validate(
            {"strategy": "cartesian", "values": {"popsize": [100, 200, 300]}}
        )
        assert p.strategy == ParameterStrategy.cartesian
        assert p.values["popsize"] == [100, 200, 300]

    def test_range_value(self):
        p = Parameters.model_validate(
            {"values": {"mutation": {"from": 0.1, "to": 0.5, "step": 0.1}}}
        )
        assert isinstance(p.values["mutation"], RangeSpec)

    def test_seeds(self):
        p = Parameters.model_validate(
            {"values": {}, "seeds": {"count": 30, "meta-seed": 42}}
        )
        assert p.seeds.count == 30
        assert p.seeds.meta_seed == 42

    def test_invalid_strategy(self):
        with pytest.raises(ValidationError):
            Parameters.model_validate({"values": {}, "strategy": "random"})


class TestSeedsConfig:
    def test_valid(self):
        s = SeedsConfig.model_validate({"count": 5, "meta-seed": 0})
        assert s.count == 5

    def test_count_zero_raises(self):
        with pytest.raises(ValidationError):
            SeedsConfig.model_validate({"count": 0, "meta-seed": 42})


# ---------------------------------------------------------------------------
# Policy
# ---------------------------------------------------------------------------

class TestPolicy:
    def test_defaults(self):
        p = Policy.model_validate({})
        assert p.on_failure == OnFailure.continue_
        assert p.retries == 0
        assert p.workers is None

    def test_valid_timeout(self):
        p = Policy.model_validate({"timeout-per-run": "30m"})
        assert p.timeout_per_run == "30m"

    def test_invalid_timeout(self):
        with pytest.raises(ValidationError, match="duration"):
            Policy.model_validate({"timeout-per-run": "30minutes"})

    def test_memory_limit_per_run_removed(self):
        with pytest.raises(ValidationError):
            Policy.model_validate({"memory-limit-per-run": "8GB"})

    def test_abort_on_failure(self):
        p = Policy.model_validate({"on-failure": "abort"})
        assert p.on_failure == OnFailure.abort

    def test_negative_workers_raises(self):
        with pytest.raises(ValidationError):
            Policy.model_validate({"workers": 0})


# ---------------------------------------------------------------------------
# SyncConfig
# ---------------------------------------------------------------------------

class TestSyncConfig:
    def test_rsync(self):
        s = SyncConfig.model_validate(
            {"backend": "rsync", "destination": "ygor@host:/srv/", "exclude": ["outputs/**"]}
        )
        assert s.backend == "rsync"
        assert s.exclude == ["outputs/**"]

    def test_invalid_backend_raises(self):
        with pytest.raises(ValidationError):
            SyncConfig.model_validate({"backend": "ftp"})
