"""Tests for image-build secret resolution."""

from __future__ import annotations

import pytest

from ygor.secrets import SecretResolutionError, resolve_image_build_secrets


class TestResolveImageBuildSecrets:
    def test_no_secret_refs_returns_commands_unchanged(self):
        resolved = resolve_image_build_secrets(["echo ok"], {})

        assert resolved.run == ["echo ok"]
        assert resolved.build_secrets == []

    def test_resolves_literal_value_without_interpolating_it(self):
        resolved = resolve_image_build_secrets(
            ["grbgetkey {{ secrets.gurobi-license.value }}"],
            {"secrets": {"gurobi-license": {"value": "secret-key"}}},
        )

        assert resolved.run == ['grbgetkey "$(cat /run/secrets/gurobi-license)"']
        assert resolved.build_secrets[0].id == "gurobi-license"
        assert resolved.build_secrets[0].value == "secret-key"
        assert "secret-key" not in resolved.run[0]

    def test_resolves_from_host_env(self):
        resolved = resolve_image_build_secrets(
            ["grbgetkey {{ secrets.gurobi-license.value }}"],
            {"secrets": {"gurobi-license": {"from-env": "GUROBI_KEY"}}},
            environ={"GUROBI_KEY": "from-env-secret"},
        )

        assert resolved.build_secrets[0].value == "from-env-secret"

    def test_missing_secret_fails(self):
        with pytest.raises(SecretResolutionError, match="Missing required secret"):
            resolve_image_build_secrets(
                ["grbgetkey {{ secrets.gurobi-license.value }}"],
                {},
            )

    def test_missing_host_env_fails(self):
        with pytest.raises(SecretResolutionError, match="environment variable is not set"):
            resolve_image_build_secrets(
                ["grbgetkey {{ secrets.gurobi-license.value }}"],
                {"secrets": {"gurobi-license": {"from-env": "GUROBI_KEY"}}},
                environ={},
            )

    def test_requires_exactly_one_value_source(self):
        with pytest.raises(SecretResolutionError, match="exactly one"):
            resolve_image_build_secrets(
                ["echo {{ secrets.token.value }}"],
                {"secrets": {"token": {"value": "a", "from-env": "TOKEN"}}},
            )
