"""Helpers for resolving local project configuration values."""

from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Any


class LocalConfigValueError(Exception):
    """Raised when a local config value cannot be resolved."""


def mapping_section(
    local_config: Mapping[str, Any] | None,
    section: str,
) -> Mapping[str, Any]:
    """Return a mapping section from local config, or an empty mapping."""
    if not isinstance(local_config, Mapping):
        return {}
    value = local_config.get(section)
    return value if isinstance(value, Mapping) else {}


def resolve_value_or_env(
    *,
    kind: str,
    name: str,
    local: Mapping[str, Any],
    environ: Mapping[str, str] | None = None,
    required: bool = True,
) -> tuple[bool, str | None]:
    """Resolve a local config mapping with exactly one of value or from-env.

    Returns ``(False, None)`` when an optional ``from-env`` reference is absent.
    """
    environ = os.environ if environ is None else environ
    has_value = "value" in local
    has_from_env = "from-env" in local
    if has_value == has_from_env:
        raise LocalConfigValueError(
            f"{kind} '{name}' expects exactly one of value or from-env in local config."
        )

    if has_from_env:
        host_name = str(local["from-env"])
        if host_name not in environ:
            if required:
                raise LocalConfigValueError(
                    f"{kind} '{name}' uses from-env '{host_name}', but that environment variable is not set."
                )
            return False, None
        return True, environ[host_name]

    return True, str(local["value"])
