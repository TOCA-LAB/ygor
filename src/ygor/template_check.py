"""Jinja2 template validation for recipe commands."""

from __future__ import annotations

from jinja2.sandbox import SandboxedEnvironment
from jinja2 import TemplateSyntaxError

from ygor.schema import Recipe


class TemplateError(Exception):
    def __init__(self, command_index: int, message: str) -> None:
        self.command_index = command_index
        self.message = message
        super().__init__(f"command[{command_index}]: {message}")


def check_templates(recipe: Recipe) -> list[TemplateError]:
    """Parse all command and post Jinja2 templates, returning syntax errors found."""
    env = SandboxedEnvironment()
    errors: list[TemplateError] = []

    all_commands = list(enumerate(recipe.command)) + [
        (len(recipe.command) + i, cmd) for i, cmd in enumerate(recipe.post)
    ]

    for idx, cmd in all_commands:
        try:
            env.parse(cmd)
        except TemplateSyntaxError as exc:
            errors.append(TemplateError(idx, f"template syntax error: {exc.message}"))

    return errors
