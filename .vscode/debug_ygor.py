"""Debug entrypoint for the local ygor Typer application."""

from __future__ import annotations

import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
INSTALLED = Path("/usr/lib/ygor")

sys.path.insert(0, str(SRC))
if INSTALLED.exists():
    sys.path.append(str(INSTALLED))

from ygor.cli import app  # noqa: E402


if __name__ == "__main__":
    app()
