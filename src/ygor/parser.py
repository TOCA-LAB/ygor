"""Load and parse a recipe.yaml file into a validated Recipe model."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from pydantic import ValidationError
from ruamel.yaml import YAML

from ygor.schema import Recipe


class RecipeParseError(Exception):
    """Raised when a recipe file cannot be loaded or fails validation."""

    def __init__(self, path: Path, message: str) -> None:
        self.path = path
        self.message = message
        super().__init__(f"{path}: {message}")


def _load_yaml(path: Path) -> Any:
    yaml = YAML()
    yaml.preserve_quotes = True
    try:
        with path.open("r", encoding="utf-8") as fh:
            return yaml.load(fh)
    except Exception as exc:
        raise RecipeParseError(path, f"YAML parse error: {exc}") from exc


def load_recipe(path: Path | str) -> Recipe:
    """Parse *path* as a recipe.yaml and return a validated :class:`Recipe`.

    Raises :class:`RecipeParseError` on any YAML or schema error.
    """
    path = Path(path)
    if not path.exists():
        raise RecipeParseError(path, "file not found")

    raw = _load_yaml(path)

    if not isinstance(raw, dict):
        raise RecipeParseError(path, "recipe must be a YAML mapping at the top level")

    try:
        return Recipe.model_validate(raw)
    except ValidationError as exc:
        lines = [f"recipe validation failed ({exc.error_count()} error(s)):"]
        for err in exc.errors():
            loc = " → ".join(str(p) for p in err["loc"]) or "<root>"
            lines.append(f"  • {loc}: {err['msg']}")
        raise RecipeParseError(path, "\n".join(lines)) from exc
