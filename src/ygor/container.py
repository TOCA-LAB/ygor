"""Container execution boundary used by runners and source builds."""

from __future__ import annotations

from typing import Protocol

from ygor.apptainer import ApptainerConfig


ContainerConfig = ApptainerConfig


class ContainerBackend(Protocol):
    """Build command-line arguments for executing inside a prepared container."""

    def exec_args(self, cmd: str, config: ContainerConfig) -> list[str]:
        ...


class ApptainerBackend:
    """Container backend backed by ``apptainer exec``."""

    def exec_args(self, cmd: str, config: ContainerConfig) -> list[str]:
        from ygor.apptainer import make_apptainer_exec_args

        return make_apptainer_exec_args(cmd, config)


DEFAULT_CONTAINER_BACKEND = ApptainerBackend()


def make_container_exec_args(cmd: str, config: ContainerConfig) -> list[str]:
    """Build execution arguments for the configured container backend."""
    return DEFAULT_CONTAINER_BACKEND.exec_args(cmd, config)
