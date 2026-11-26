"""Tests for local runtime binding resolution."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ygor.bindings import BindingResolutionError, merge_binding_env, resolve_bindings
from ygor.cli import _apptainer_config_from_manifest, _apptainer_config_to_manifest
from ygor.apptainer import ApptainerConfig, ApptainerMount
from ygor.schema import Recipe


def _bindings(recipe_yaml: str):
    recipe = Recipe.model_validate(
        {
            "environment": {
                "bindings": recipe_yaml,
            },
            "command": "echo ok",
        }
    )
    return recipe.environment.bindings  # type: ignore[union-attr]


class TestResolveBindings:
    def test_required_file_binding_resolves(self, tmp_path):
        lic = tmp_path / "gurobi.lic"
        lic.write_text("LICENSE", encoding="utf-8")
        bindings = _bindings(
            {
                "gurobi-license": {
                    "type": "file",
                    "target": "/run/ygor/bindings/gurobi.lic",
                    "env": {"GRB_LICENSE_FILE": "/run/ygor/bindings/gurobi.lic"},
                }
            }
        )

        resolved = resolve_bindings(
            bindings,
            {"bindings": {"gurobi-license": {"source": str(lic)}}},
            config_dir=tmp_path,
        )

        assert resolved.env_vars == {"GRB_LICENSE_FILE": "/run/ygor/bindings/gurobi.lic"}
        assert resolved.mounts == [
            ApptainerMount(
                source=lic,
                target=Path("/run/ygor/bindings/gurobi.lic"),
                readonly=True,
            )
        ]
        assert resolved.manifest["gurobi-license"] == {
            "type": "file",
            "required": True,
            "resolved": True,
            "target": "/run/ygor/bindings/gurobi.lic",
            "readonly": True,
        }

    def test_missing_required_binding_fails(self, tmp_path):
        bindings = _bindings(
            {"license": {"type": "file", "target": "/run/ygor/bindings/license"}}
        )

        with pytest.raises(BindingResolutionError, match="Missing required binding 'license'"):
            resolve_bindings(bindings, {}, config_dir=tmp_path)

    def test_optional_missing_binding_is_unresolved(self, tmp_path):
        bindings = _bindings(
            {
                "cache": {
                    "type": "directory",
                    "required": False,
                    "target": "/run/ygor/bindings/cache",
                    "env": {"CACHE_DIR": "/run/ygor/bindings/cache"},
                }
            }
        )

        resolved = resolve_bindings(bindings, {}, config_dir=tmp_path)

        assert resolved.env_vars == {}
        assert resolved.mounts == []
        assert resolved.manifest["cache"]["resolved"] is False

    def test_file_binding_rejects_directory(self, tmp_path):
        bindings = _bindings({"license": {"type": "file", "target": "/license"}})

        with pytest.raises(BindingResolutionError, match="not a regular file"):
            resolve_bindings(
                bindings,
                {"bindings": {"license": {"source": str(tmp_path)}}},
                config_dir=tmp_path,
            )

    def test_directory_binding_rejects_file(self, tmp_path):
        file_path = tmp_path / "cache"
        file_path.write_text("not dir", encoding="utf-8")
        bindings = _bindings({"cache": {"type": "directory", "target": "/cache"}})

        with pytest.raises(BindingResolutionError, match="not a directory"):
            resolve_bindings(
                bindings,
                {"bindings": {"cache": {"source": str(file_path)}}},
                config_dir=tmp_path,
            )

    def test_env_binding_resolves_literal_value(self, tmp_path):
        bindings = _bindings(
            {
                "token": {
                    "type": "env",
                    "env": {"DATASET_TOKEN": "{{ binding.value }}"},
                }
            }
        )

        resolved = resolve_bindings(
            bindings,
            {"bindings": {"token": {"value": "secret-token"}}},
            config_dir=tmp_path,
        )

        assert resolved.env_vars == {"DATASET_TOKEN": "secret-token"}
        assert resolved.manifest["token"] == {
            "type": "env",
            "required": True,
            "resolved": True,
        }

    def test_env_binding_resolves_from_host_env(self, tmp_path):
        bindings = _bindings(
            {
                "token": {
                    "type": "env",
                    "env": {"DATASET_TOKEN": "{{ binding.value }}"},
                }
            }
        )

        resolved = resolve_bindings(
            bindings,
            {"bindings": {"token": {"from-env": "UFABC_DATASET_TOKEN"}}},
            config_dir=tmp_path,
            environ={"UFABC_DATASET_TOKEN": "from-host"},
        )

        assert resolved.env_vars == {"DATASET_TOKEN": "from-host"}

    def test_env_binding_fails_when_host_env_missing(self, tmp_path):
        bindings = _bindings(
            {"token": {"type": "env", "env": {"DATASET_TOKEN": "{{ binding.value }}"}}}
        )

        with pytest.raises(BindingResolutionError, match="environment variable is not set"):
            resolve_bindings(
                bindings,
                {"bindings": {"token": {"from-env": "UFABC_DATASET_TOKEN"}}},
                config_dir=tmp_path,
                environ={},
            )

    def test_duplicate_environment_variable_detection(self, tmp_path):
        with pytest.raises(BindingResolutionError, match="Duplicate environment variable"):
            merge_binding_env({"DATASET_TOKEN": "base"}, {"DATASET_TOKEN": "binding"})

    def test_manifest_redacts_sources_and_values(self, tmp_path):
        secret_file = tmp_path / "secret.lic"
        secret_file.write_text("license", encoding="utf-8")
        bindings = _bindings(
            {
                "license": {"type": "file", "target": "/license"},
                "token": {"type": "env", "env": {"TOKEN": "{{ binding.value }}"}},
            }
        )

        resolved = resolve_bindings(
            bindings,
            {
                "bindings": {
                    "license": {"source": str(secret_file)},
                    "token": {"value": "secret-token"},
                }
            },
            config_dir=tmp_path,
        )
        payload = json.dumps(resolved.manifest)

        assert str(secret_file) not in payload
        assert "secret-token" not in payload
        assert resolved.manifest["license"]["target"] == "/license"


class TestApptainerManifestRedaction:
    def test_apptainer_manifest_excludes_binding_env_and_mounts(self, tmp_path):
        binding_source = tmp_path / "secret.lic"
        binding_source.write_text("license", encoding="utf-8")
        project_mount = tmp_path / "project"
        project_mount.mkdir()
        container_config = ApptainerConfig(
            image="ubuntu:24.04",
            env_vars={"SAFE": "ok", "TOKEN": "secret-token"},
            mounts=[
                project_mount,
                ApptainerMount(binding_source, Path("/license"), readonly=True),
            ],
        )
        bindings = resolve_bindings(
            _bindings(
                {
                    "token": {"type": "env", "env": {"TOKEN": "{{ binding.value }}"}},
                    "license": {"type": "file", "target": "/license"},
                }
            ),
            {
                "bindings": {
                    "token": {"value": "secret-token"},
                    "license": {"source": str(binding_source)},
                }
            },
            config_dir=tmp_path,
        )

        manifest = _apptainer_config_to_manifest(container_config, bindings)
        payload = json.dumps(manifest)

        assert manifest["env_vars"] == {"SAFE": "ok"}
        assert manifest["mounts"] == [str(project_mount)]
        assert "secret-token" not in payload
        assert str(binding_source) not in payload

    def test_apptainer_manifest_round_trip(self):
        container_config = ApptainerConfig(
            image="ubuntu:24.04",
        )

        manifest = _apptainer_config_to_manifest(container_config)
        restored = _apptainer_config_from_manifest(manifest)

        assert manifest["image"] == "ubuntu:24.04"
        assert restored is not None
        assert restored.image == "ubuntu:24.04"
