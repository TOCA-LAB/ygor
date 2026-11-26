"""Resolve recipe bindings against local project configuration."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

from ygor.apptainer import ApptainerMount
from ygor.local_config import LocalConfigValueError, mapping_section, resolve_value_or_env
from ygor.schema import Binding, DirectoryBinding, EnvBinding, FileBinding

_BINDING_VALUE_TEMPLATE = "{{ binding.value }}"


class BindingResolutionError(Exception):
    """Raised when a recipe binding cannot be resolved from local config."""


@dataclass
class ResolvedBindings:
    env_vars: dict[str, str] = field(default_factory=dict)
    mounts: list[ApptainerMount] = field(default_factory=list)
    manifest: dict[str, dict[str, Any]] = field(default_factory=dict)


def resolve_bindings(
    recipe_bindings: Mapping[str, Binding] | None,
    local_config: Mapping[str, Any] | None,
    *,
    config_dir: Path,
    environ: Mapping[str, str] | None = None,
) -> ResolvedBindings:
    """Resolve recipe-declared bindings using local config.

    The returned manifest data is intentionally redacted: it records only
    logical binding metadata, never local source paths or resolved values.
    """
    resolved = ResolvedBindings()
    if not recipe_bindings:
        return resolved

    environ = os.environ if environ is None else environ
    local_bindings = _local_bindings(local_config)

    for name, binding in recipe_bindings.items():
        local = local_bindings.get(name)
        if not isinstance(local, Mapping):
            if binding.required:
                raise BindingResolutionError(
                    f"Missing required binding '{name}'. Define it in .ygorrc.yaml under bindings.{name}."
                )
            resolved.manifest[name] = _manifest_entry(binding, resolved=False)
            continue

        if isinstance(binding, FileBinding):
            _resolve_path_binding(name, binding, local, config_dir, resolved, expect_file=True)
        elif isinstance(binding, DirectoryBinding):
            _resolve_path_binding(name, binding, local, config_dir, resolved, expect_file=False)
        elif isinstance(binding, EnvBinding):
            _resolve_env_binding(name, binding, local, environ, resolved)
        else:  # pragma: no cover - protected by schema discriminator
            raise BindingResolutionError(f"Unsupported binding type for '{name}'.")

    return resolved


def merge_binding_env(
    base_env: Mapping[str, Any],
    binding_env: Mapping[str, str],
) -> dict[str, Any]:
    """Merge binding-provided env vars, rejecting duplicate names."""
    merged = dict(base_env)
    duplicates = sorted(set(merged).intersection(binding_env))
    if duplicates:
        names = ", ".join(duplicates)
        raise BindingResolutionError(f"Duplicate environment variable from bindings: {names}")
    merged.update(binding_env)
    return merged


def _local_bindings(local_config: Mapping[str, Any] | None) -> Mapping[str, Any]:
    return mapping_section(local_config, "bindings")


def _resolve_path_binding(
    name: str,
    binding: FileBinding | DirectoryBinding,
    local: Mapping[str, Any],
    config_dir: Path,
    resolved: ResolvedBindings,
    *,
    expect_file: bool,
) -> None:
    source_raw = local.get("source")
    if source_raw is None:
        raise BindingResolutionError(f"Binding '{name}' expects a source path in local config.")

    source = Path(str(source_raw)).expanduser()
    if not source.is_absolute():
        source = config_dir / source
    source = source.resolve()

    expected = "file" if expect_file else "directory"
    if not source.exists():
        raise BindingResolutionError(
            f"Binding '{name}' expects a {expected}, but the configured source does not exist."
        )
    if expect_file and not source.is_file():
        raise BindingResolutionError(
            f"Binding '{name}' expects a file, but the configured source is not a regular file."
        )
    if not expect_file and not source.is_dir():
        raise BindingResolutionError(
            f"Binding '{name}' expects a directory, but the configured source is not a directory."
        )

    target = Path(binding.target)
    resolved.mounts.append(
        ApptainerMount(source=source, target=target, readonly=binding.readonly)
    )
    _add_binding_env(name, resolved, binding.env)
    resolved.manifest[name] = _manifest_entry(binding, resolved=True)


def _resolve_env_binding(
    name: str,
    binding: EnvBinding,
    local: Mapping[str, Any],
    environ: Mapping[str, str],
    resolved: ResolvedBindings,
) -> None:
    try:
        available, value = resolve_value_or_env(
            kind="Binding",
            name=name,
            local=local,
            environ=environ,
            required=binding.required,
        )
    except LocalConfigValueError as exc:
        raise BindingResolutionError(str(exc)) from exc
    if not available or value is None:
        resolved.manifest[name] = _manifest_entry(binding, resolved=False)
        return

    rendered: dict[str, str] = {}
    for env_name, env_value in binding.env.items():
        rendered[env_name] = env_value.replace(_BINDING_VALUE_TEMPLATE, value)
    _add_binding_env(name, resolved, rendered)
    resolved.manifest[name] = _manifest_entry(binding, resolved=True)


def _manifest_entry(binding: Binding, *, resolved: bool) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "type": binding.type,
        "required": binding.required,
        "resolved": resolved,
    }
    if isinstance(binding, (FileBinding, DirectoryBinding)):
        entry["target"] = binding.target
        entry["readonly"] = binding.readonly
    return entry


def _add_binding_env(
    binding_name: str,
    resolved: ResolvedBindings,
    env_vars: Mapping[str, str],
) -> None:
    duplicates = sorted(set(resolved.env_vars).intersection(env_vars))
    if duplicates:
        names = ", ".join(duplicates)
        raise BindingResolutionError(
            f"Duplicate environment variable from binding '{binding_name}': {names}"
        )
    resolved.env_vars.update(env_vars)
