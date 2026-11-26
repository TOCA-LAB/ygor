"""Persistence boundary for run manifests and derived exports."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ygor import manifest


class ManifestRepository:
    """Read and write a run manifest separately from analysis exports."""

    def read(self, run_dir: Path) -> dict[str, Any]:
        return manifest.read(run_dir)

    def write_manifest(self, data: dict[str, Any], run_dir: Path) -> None:
        manifest.write_manifest(data, run_dir)

    def write_exports(self, data: dict[str, Any], run_dir: Path) -> None:
        manifest.write_execution_exports(data, run_dir)

    def write(self, data: dict[str, Any], run_dir: Path) -> None:
        self.write_manifest(data, run_dir)
        self.write_exports(data, run_dir)
