"""Resolve local secrets referenced by image-build commands."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Any, Mapping

from ygor.apptainer import ApptainerBuildSecret
from ygor.local_config import LocalConfigValueError, mapping_section, resolve_value_or_env

_SECRET_REF_RE = re.compile(
    r"{{\s*secrets\.([A-Za-z0-9_.-]+)\.value\s*}}"
)


class SecretResolutionError(Exception):
    """Raised when an image-build secret reference cannot be resolved."""


@dataclass
class ResolvedImageBuildSecrets:
    run: list[str]
    build_secrets: list[ApptainerBuildSecret] = field(default_factory=list)


def resolve_image_build_secrets(
    commands: list[str],
    local_config: Mapping[str, Any] | None,
    *,
    environ: Mapping[str, str] | None = None,
) -> ResolvedImageBuildSecrets:
    """Resolve ``{{ secrets.NAME.value }}`` references for image builds.

    Secret values are never interpolated into the returned commands. References
    are rewritten to read from a temporary ``/run/secrets/NAME`` file.
    """
    environ = os.environ if environ is None else environ
    names = _referenced_secret_names(commands)
    if not names:
        return ResolvedImageBuildSecrets(run=list(commands))

    local_secrets = _local_secrets(local_config)
    build_secrets: list[ApptainerBuildSecret] = []
    for name in names:
        local = local_secrets.get(name)
        if not isinstance(local, Mapping):
            raise SecretResolutionError(
                f"Missing required secret '{name}'. Define it in .ygorrc.yaml under secrets.{name}."
            )
        build_secrets.append(ApptainerBuildSecret(id=name, value=_secret_value(name, local, environ)))

    rendered = [_render_secret_refs(cmd) for cmd in commands]
    return ResolvedImageBuildSecrets(run=rendered, build_secrets=build_secrets)


def _referenced_secret_names(commands: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for cmd in commands:
        for match in _SECRET_REF_RE.finditer(cmd):
            name = match.group(1)
            if name not in seen:
                seen.add(name)
                result.append(name)
    return result


def _local_secrets(local_config: Mapping[str, Any] | None) -> Mapping[str, Any]:
    return mapping_section(local_config, "secrets")


def _secret_value(
    name: str,
    local: Mapping[str, Any],
    environ: Mapping[str, str],
) -> str:
    try:
        _, value = resolve_value_or_env(
            kind="Secret",
            name=name,
            local=local,
            environ=environ,
            required=True,
        )
    except LocalConfigValueError as exc:
        raise SecretResolutionError(str(exc)) from exc
    assert value is not None
    return value


def _render_secret_refs(command: str) -> str:
    return _SECRET_REF_RE.sub(
        lambda match: f"\"$(cat /run/secrets/{match.group(1)})\"",
        command,
    )
