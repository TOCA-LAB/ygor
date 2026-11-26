"""Pydantic models for recipe.yaml schema."""

from __future__ import annotations

import re
from datetime import date
from enum import Enum
from typing import Annotated, Any, Literal, Union

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)


# ---------------------------------------------------------------------------
# Shared config
# ---------------------------------------------------------------------------

class _Base(BaseModel):
    model_config = ConfigDict(extra="forbid")


# ---------------------------------------------------------------------------
# meta
# ---------------------------------------------------------------------------

class RecipeMeta(_Base):
    name: str
    description: str | None = None
    author: str | None = None
    created: date | None = None


# ---------------------------------------------------------------------------
# datasets
# ---------------------------------------------------------------------------

class HttpSource(_Base):
    type: Literal["http"]
    url: str


class GitSource(_Base):
    type: Literal["git"]
    url: str
    ref: str


class LocalSource(_Base):
    type: Literal["local"]
    path: str


class StoreSource(_Base):
    type: Literal["store"]
    hash: str | None = None


Source = Annotated[
    Union[HttpSource, GitSource, LocalSource, StoreSource],
    Field(discriminator="type"),
]

_EXTRACT_VALUES = {"tar.gz", "zip", "tar.bz2", "tar.xz", "bz2", "rar"}


class Dataset(_Base):
    source: Source
    sha256: str | None = None
    extract: str | None = None
    include: list[str] = Field(default_factory=list)
    # Backward-compatible location for store-type dataset hashes.
    hash: str | None = None

    @field_validator("extract")
    @classmethod
    def _valid_extract(cls, v: str | None) -> str | None:
        if v is not None and v not in _EXTRACT_VALUES:
            raise ValueError(f"extract must be one of {_EXTRACT_VALUES}, got {v!r}")
        return v


# ---------------------------------------------------------------------------
# environment
# ---------------------------------------------------------------------------

class AptConfig(_Base):
    snapshot: date | None = None
    packages: list[str] = Field(default_factory=list)


class PythonConfig(_Base):
    requirements: str


class AppSource(_Base):
    type: Literal["git", "local"]
    url: str | None = None
    ref: str | None = None
    path: str | None = None


class AppConfig(_Base):
    model_config = ConfigDict(extra="forbid")

    source: AppSource | None = None
    build: list[str] = Field(default_factory=list)
    binary: str | None = None
    # shorthand: python: { requirements: ... }
    requirements: str | None = None


class Resources(_Base):
    cpu: int | None = Field(None, ge=1)
    memory: str | None = None  # e.g. "32GB"

    @field_validator("memory", mode="after")
    @classmethod
    def _valid_memory(cls, v: str | None) -> str | None:
        if v is not None and not re.match(r"^\d+(\.\d+)?(B|KB|MB|GB|TB)$", v):
            raise ValueError(f"resources.memory must be like '8GB', got {v!r}")
        return v


class ImageBuild(_Base):
    run: list[str] = Field(default_factory=list)


class FileBinding(_Base):
    type: Literal["file"]
    required: bool = True
    target: str
    readonly: bool = True
    env: dict[str, str] = Field(default_factory=dict)

    @field_validator("env")
    @classmethod
    def _no_binding_value_template(cls, v: dict[str, str]) -> dict[str, str]:
        for value in v.values():
            if "{{ binding.value }}" in value:
                raise ValueError("{{ binding.value }} is only valid for env bindings")
        return v


class DirectoryBinding(_Base):
    type: Literal["directory"]
    required: bool = True
    target: str
    readonly: bool = True
    env: dict[str, str] = Field(default_factory=dict)

    @field_validator("env")
    @classmethod
    def _no_binding_value_template(cls, v: dict[str, str]) -> dict[str, str]:
        for value in v.values():
            if "{{ binding.value }}" in value:
                raise ValueError("{{ binding.value }} is only valid for env bindings")
        return v


class EnvBinding(_Base):
    type: Literal["env"]
    required: bool = True
    env: dict[str, str]

    @field_validator("env")
    @classmethod
    def _env_not_empty(cls, v: dict[str, str]) -> dict[str, str]:
        if not v:
            raise ValueError("env binding requires at least one environment variable")
        return v


Binding = Annotated[
    Union[FileBinding, DirectoryBinding, EnvBinding],
    Field(discriminator="type"),
]


class Environment(_Base):
    base_image: str | None = Field(None, alias="base-image")
    mac_address: str | None = Field(None, alias="mac-address")
    variables: dict[str, str | int | float] = Field(default_factory=dict)
    bindings: dict[str, Binding] = Field(default_factory=dict)
    image_build: ImageBuild | None = Field(None, alias="image-build")
    resources: Resources | None = None
    apps: dict[str, AptConfig | PythonConfig | AppConfig] | None = None

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    @field_validator("mac_address")
    @classmethod
    def _valid_mac_address(cls, v: str | None) -> str | None:
        if v is not None and not re.match(r"^[0-9A-Fa-f]{2}(:[0-9A-Fa-f]{2}){5}$", v):
            raise ValueError("mac-address must be a MAC address like '02:42:ac:11:00:02'")
        return v


# ---------------------------------------------------------------------------
# parameters
# ---------------------------------------------------------------------------

class ParameterStrategy(str, Enum):
    cartesian = "cartesian"
    zip = "zip"
    sample = "sample"


class RangeSpec(_Base):
    """Numeric range: {from, to, step} or {from, to, count, scale}."""
    from_: float = Field(alias="from")
    to: float
    step: float | None = None
    count: int | None = None
    scale: str | None = None  # "linear" | "log"

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    @model_validator(mode="after")
    def _step_or_count(self) -> "RangeSpec":
        if self.step is None and self.count is None:
            raise ValueError("RangeSpec requires either 'step' or 'count'")
        if self.step is not None and self.count is not None:
            raise ValueError("RangeSpec: use either 'step' or 'count', not both")
        if self.step is not None and self.step <= 0:
            raise ValueError("RangeSpec 'step' must be greater than zero")
        if self.to < self.from_:
            raise ValueError("RangeSpec 'to' must be greater than or equal to 'from'")
        if self.count is not None and self.count < 1:
            raise ValueError("RangeSpec 'count' must be greater than or equal to 1")
        if self.scale not in (None, "linear", "log"):
            raise ValueError("RangeSpec 'scale' must be 'linear' or 'log'")
        if self.scale == "log" and (self.from_ <= 0 or self.to <= 0):
            raise ValueError("RangeSpec with log scale requires positive from/to values")
        return self


# A parameter value dimension: list of literals, or a RangeSpec
ParameterDim = list[Any] | RangeSpec


class SeedsConfig(_Base):
    count: int = Field(ge=1)
    meta_seed: int = Field(alias="meta-seed", ge=0)

    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class Parameters(_Base):
    strategy: ParameterStrategy = ParameterStrategy.cartesian
    values: dict[str, ParameterDim] = Field(default_factory=dict)
    instance: list[str] | None = None
    seeds: SeedsConfig | None = None
    sample_size: int | None = Field(None, ge=1)

    @model_validator(mode="after")
    def _sample_needs_size(self) -> "Parameters":
        if self.strategy == ParameterStrategy.sample and self.sample_size is None:
            raise ValueError("strategy 'sample' requires 'sample_size'")
        return self


# ---------------------------------------------------------------------------
# policy
# ---------------------------------------------------------------------------

class OnFailure(str, Enum):
    continue_ = "continue"
    abort = "abort"


_DURATION_RE = re.compile(r"^\d+(\.\d+)?(s|m|h|d)$")


def _validate_duration(v: str | None, field_name: str) -> str | None:
    if v is not None and not _DURATION_RE.match(v):
        raise ValueError(
            f"{field_name} must be a duration like '30m', '2h', '1d', got {v!r}"
        )
    return v


class Policy(_Base):
    timeout_per_run: str | None = Field(None, alias="timeout-per-run")
    workers: int | None = Field(None, ge=1)
    on_failure: OnFailure = Field(OnFailure.continue_, alias="on-failure")
    retries: int = Field(0, ge=0)
    resume: str | None = None  # e.g. "skip-existing"

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    @field_validator("timeout_per_run", mode="after")
    @classmethod
    def _valid_timeout(cls, v: str | None) -> str | None:
        return _validate_duration(v, "timeout-per-run")

# ---------------------------------------------------------------------------
# sync
# ---------------------------------------------------------------------------

class OnConflict(str, Enum):
    skip = "skip"
    overwrite = "overwrite"
    fail = "fail"


class SyncConfig(_Base):
    backend: Literal["rsync", "s3", "none"]
    destination: str | None = None
    exclude: list[str] = Field(default_factory=list)
    on_conflict: OnConflict = Field(OnConflict.skip, alias="on-conflict")

    model_config = ConfigDict(extra="forbid", populate_by_name=True)


# ---------------------------------------------------------------------------
# Top-level Recipe
# ---------------------------------------------------------------------------

class Recipe(_Base):
    meta: RecipeMeta | None = None
    datasets: dict[str, Dataset] = Field(default_factory=dict)
    environment: Environment | None = None
    command: list[str] | str
    parameters: Parameters | None = None
    policy: Policy = Field(default_factory=Policy)
    post: list[str] = Field(default_factory=list)
    sync: SyncConfig | None = None

    @field_validator("command", mode="before")
    @classmethod
    def _normalise_command(cls, v: Any) -> list[str]:
        if isinstance(v, str):
            return [v]
        return v

    @model_validator(mode="after")
    def _command_not_empty(self) -> "Recipe":
        if not self.command:
            raise ValueError("'command' must have at least one entry")
        return self
