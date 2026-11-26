"""CLI entry-point for the `ygor` tool."""

from __future__ import annotations

import json as _json
import importlib
import os
import subprocess
import sys
from io import StringIO
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, Any

import typer
from rich.console import Console
from rich.table import Table
from rich.text import Text

from ygor.expander import build_plan
from ygor.parser import RecipeParseError, load_recipe
from ygor.schema import AppConfig, Recipe
from ygor.template_check import check_templates
from ygor.bindings import BindingResolutionError, ResolvedBindings, merge_binding_env, resolve_bindings
from ygor.datasets import DatasetResolutionError, ensure_datasets, find_cached_datasets, resolve_instance_globs
from ygor.secrets import SecretResolutionError, resolve_image_build_secrets
from ygor.sources import AppSourceError, ensure_app_sources
from ygor.sync_service import SyncError, published_hashes as _published_hashes, sync_stores
from ygor import manifest as _manifest
from ygor import store as _store
from ygor import runner as _runner
from ygor.manifest_repository import ManifestRepository
from ygor.compat import LEGACY_CONFIG_FILE
from ygor.run_service import (
    build_run_identity,
    executions_from_plan,
    finalize_manifest_status,
    seeds_info_from_plan,
)
from ygor.apptainer import (
    ApptainerBuildError,
    ApptainerConfig,
    ApptainerMount,
    ApptainerNotAvailable,
    check_apptainer,
    ensure_ygor_image,
)

app = typer.Typer(
    name="ygor",
    help="YAML-Guided Orchestrator for Research.",
    no_args_is_help=True,
    add_completion=False,
)

_out = Console()
_err = Console(stderr=True)
_DEFAULT_STORE = Path("store")
_CONFIG_FILE = ".ygorrc.yaml"
_MANIFEST_REPOSITORY = ManifestRepository()
_HELP_PANEL_RECIPE = "Recipe"
_HELP_PANEL_RUN = "Run"
_HELP_PANEL_CONTROL = "Control"
_HELP_PANEL_SHARING = "Sharing"
_HELP_PANEL_SYSTEM = "System"
_HELP_PANEL_ORDER = {
    _HELP_PANEL_RECIPE: 0,
    _HELP_PANEL_RUN: 1,
    _HELP_PANEL_CONTROL: 2,
    _HELP_PANEL_SHARING: 3,
    _HELP_PANEL_SYSTEM: 4,
}
_COMMAND_HELP_ORDER = {
    "validate": 0,
    "run": 10,
    "list": 11,
    "latest": 12,
    "show": 13,
    "status": 14,
    "open": 15,
    "web": 16,
    "pause": 20,
    "resume": 21,
    "publish": 30,
    "unpublish": 31,
    "sync": 32,
    "doctor": 40,
}
_RUNTIME_DEPENDENCIES = {
    "pydantic": "pydantic",
    "ruamel.yaml": "ruamel.yaml",
    "typer": "typer",
    "rich": "rich",
    "jinja2": "jinja2",
    "numpy": "numpy",
    "rarfile": "rarfile",
}


@app.callback()
def _main() -> None:
    """YAML-Guided Orchestrator for Research."""


# ---------------------------------------------------------------------------
# ygor doctor
# ---------------------------------------------------------------------------

def _doctor_check(name: str, ok: bool, detail: str, *, level: str | None = None) -> dict[str, Any]:
    if level is None:
        level = "ok" if ok else "fail"
    return {"name": name, "ok": ok, "level": level, "detail": detail}


def _store_writable_detail(store: Path) -> tuple[bool, str]:
    if store.exists():
        if not store.is_dir():
            return False, f"{store} exists but is not a directory"
        return os.access(store, os.W_OK), str(store)

    parent = store.parent if store.parent != Path("") else Path(".")
    parent = parent.resolve()
    if not parent.exists():
        return False, f"parent does not exist: {parent}"
    return os.access(parent, os.W_OK), f"{store} can be created"


@app.command(rich_help_panel=_HELP_PANEL_SYSTEM)
def doctor(
    store: Annotated[Path, typer.Option("--store", help="Store root directory to check.")] = _DEFAULT_STORE,
    json: Annotated[bool, typer.Option("--json", help="Output checks as JSON.")] = False,
) -> None:
    """Check the local ygor environment."""
    store = store.resolve()
    checks: list[dict[str, Any]] = []

    checks.append(_doctor_check("python", True, sys.executable))
    checks.append(_doctor_check("ygor module", True, str(Path(__file__).resolve())))

    executable = Path(sys.argv[0]).resolve() if sys.argv and sys.argv[0] else None
    checks.append(_doctor_check("ygor executable", executable is not None, str(executable or "unknown")))

    venv = os.environ.get("VIRTUAL_ENV")
    venv_level = "ok" if venv else "warn"
    venv_detail = venv or "not active"
    if venv and executable is not None and executable.parent.name == "bin":
        executable_venv = executable.parent.parent
        try:
            if Path(venv).resolve() != executable_venv:
                venv_level = "warn"
                venv_detail = f"{venv} (ygor executable is in {executable_venv})"
        except OSError:
            venv_level = "warn"
    checks.append(
        _doctor_check(
            "virtualenv",
            True,
            venv_detail,
            level=venv_level,
        )
    )

    for label, module_name in _RUNTIME_DEPENDENCIES.items():
        try:
            module = importlib.import_module(module_name)
            version = getattr(module, "__version__", "installed")
            checks.append(_doctor_check(f"dependency:{label}", True, str(version)))
        except Exception as exc:
            checks.append(_doctor_check(f"dependency:{label}", False, str(exc)))

    store_ok, store_detail = _store_writable_detail(store)
    checks.append(_doctor_check("store writable", store_ok, store_detail))

    runs_dir = store / "runs"
    if runs_dir.exists():
        run_count = sum(1 for p in runs_dir.iterdir() if p.is_dir())
        checks.append(_doctor_check("store runs", True, f"{run_count} run(s)"))
    else:
        checks.append(_doctor_check("store runs", True, "no runs directory yet", level="warn"))

    try:
        check_apptainer()
        checks.append(_doctor_check("apptainer", True, "available"))
    except Exception as exc:
        checks.append(_doctor_check("apptainer", True, str(exc), level="warn"))

    has_failures = any(c["level"] == "fail" for c in checks)

    if json:
        _out.print_json(_json.dumps({"ok": not has_failures, "checks": checks}))
    else:
        tbl = Table(show_header=True, header_style="bold", box=None, padding=(0, 2))
        tbl.add_column("CHECK", no_wrap=True)
        tbl.add_column("STATUS", no_wrap=True)
        tbl.add_column("DETAIL")
        for check in checks:
            level = check["level"]
            if level == "ok":
                status = "[green]ok[/green]"
            elif level == "warn":
                status = "[yellow]warn[/yellow]"
            else:
                status = "[red]fail[/red]"
            tbl.add_row(check["name"], status, check["detail"])
        _out.print(tbl)

    if has_failures:
        raise typer.Exit(1)


# ---------------------------------------------------------------------------
# ygor validate
# ---------------------------------------------------------------------------

@app.command(rich_help_panel=_HELP_PANEL_RECIPE)
def validate(
    recipe_path: Annotated[Path, typer.Argument(help="Path to recipe.yaml to validate.")],
    json: Annotated[bool, typer.Option("--json", help="Output result as JSON.")] = False,
) -> None:
    """Validate a recipe.yaml file without running anything."""
    try:
        recipe = load_recipe(recipe_path)
    except RecipeParseError as exc:
        if json:
            _err.print_json(_json.dumps({"ok": False, "error": exc.message}))
        else:
            _err.print(Text("✗ ", style="bold red") + Text(str(exc)))
        raise typer.Exit(1)

    template_errors = check_templates(recipe)
    if template_errors:
        msgs = [f"command[{e.command_index}]: {e.message}" for e in template_errors]
        combined = "\n".join(msgs)
        if json:
            _err.print_json(_json.dumps({"ok": False, "error": combined}))
        else:
            for e in template_errors:
                _err.print(Text("✗ ", style="bold red") + Text(str(e)))
        raise typer.Exit(1)

    if json:
        _out.print_json(
            _json.dumps(
                {
                    "ok": True,
                    "name": recipe.meta.name if recipe.meta else None,
                    "path": str(recipe_path),
                }
            )
        )
    else:
        name = recipe.meta.name if recipe.meta else recipe_path.name
        _out.print(Text("✓ ", style="bold green") + Text(f"{name} — recipe is valid"))


# ---------------------------------------------------------------------------
# dry-run helpers
# ---------------------------------------------------------------------------

class _FallbackDict(dict):
    """Dict that returns a placeholder string for missing keys.

    Keeps {{ app['solver'] }} readable instead of raising KeyError when
    app/dataset paths are not known at dry-run time.
    """
    def __init__(self, varname: str) -> None:
        super().__init__()
        self._varname = varname

    def __missing__(self, key: object) -> str:
        return f"<{self._varname}[{key!r}]>"


def _fmt_param_val(v: Any) -> str:
    """Format a parameter value for the group header (compact, no trailing zeros)."""
    if isinstance(v, float):
        return f"{v:.6g}"
    if isinstance(v, str) and ("/" in v or "\\" in v):
        return Path(v).name
    return str(v)


def _print_dry_run_commands(recipe, plan, recipe_dir: Path) -> None:
    from jinja2.sandbox import SandboxedEnvironment as _SandboxedEnv
    env = _SandboxedEnv()

    seeds = plan.seeds if plan.seeds else [0]
    has_seeds = bool(plan.seeds)
    n_seeds = len(seeds)
    n_groups = plan.n_param_tuples
    total = n_groups * n_seeds

    if has_seeds:
        summary = (
            f"[dim]{n_groups} group{'s' if n_groups != 1 else ''}"
            f" × {n_seeds} seed{'s' if n_seeds != 1 else ''}"
            f" = {total:,}[/dim]"
        )
    else:
        summary = f"[dim]{total:,}[/dim]"
    _out.rule(f"[bold cyan]Commands[/bold cyan]  {summary}")

    exec_id = 0
    for params in plan.param_tuples:
        _out.print("")

        # Group header: dim key= + cyan value for each param
        if params:
            parts = [
                f"[dim]{k}=[/dim][cyan]{_fmt_param_val(v)}[/cyan]"
                for k, v in params.items()
            ]
            seed_badge = f"  [dim]×{n_seeds}[/dim]" if has_seeds else ""
            _out.print("  ".join(parts) + seed_badge)

        for seed in seeds:
            exec_id += 1
            context: dict[str, Any] = {
                "param": params,
                "run": {
                    "seed": seed,
                    "exec_id": exec_id,
                    "hash": "<hash>",
                    "run_dir": "<run_dir>",
                    "output_dir": f"<run_dir>/outputs/exec_{exec_id:04d}",
                },
                "dataset": _FallbackDict("dataset"),
                "app": _FallbackDict("app"),
                "apps": _FallbackDict("apps"),
                "recipe": {
                    "dir": str(recipe_dir),
                },
            }
            for cmd in recipe.command:
                try:
                    rendered = env.from_string(cmd).render(**context).strip()
                except Exception:
                    rendered = cmd.strip()
                _out.print(rendered, markup=False, highlight=False, soft_wrap=True)


def _run_dry_run(
    recipe_path: Path,
    store: Path,
    *,
    json: bool = False,
    commands: bool = False,
    prepare: bool = False,
) -> None:
    """Show the full execution plan without running anything."""
    try:
        recipe = load_recipe(recipe_path)
    except RecipeParseError as exc:
        if json:
            _err.print_json(_json.dumps({"ok": False, "error": exc.message}))
        else:
            _err.print(Text("✗ ", style="bold red") + Text(str(exc)))
        raise typer.Exit(1)

    template_errors = check_templates(recipe)
    if template_errors:
        if json:
            msgs = [str(e) for e in template_errors]
            _err.print_json(_json.dumps({"ok": False, "errors": msgs}))
        else:
            for e in template_errors:
                _err.print(Text("✗ ", style="bold red") + Text(str(e)))
        raise typer.Exit(1)

    instance_values: list[str] | None = None
    if recipe.parameters and recipe.parameters.instance:
        if prepare:
            try:
                resolved_datasets = ensure_datasets(recipe.datasets, store.resolve())
            except DatasetResolutionError as exc:
                if json:
                    _err.print_json(_json.dumps({"ok": False, "error": str(exc)}))
                else:
                    _err.print(Text("✗ ", style="bold red") + Text(str(exc)))
                raise typer.Exit(1)
        else:
            resolved_datasets = find_cached_datasets(recipe.datasets, store.resolve())
        instance_values = resolve_instance_globs(recipe.parameters.instance, recipe.datasets, resolved_datasets)

    if prepare and recipe.environment and recipe.environment.apps:
        try:
            ensure_app_sources(recipe.environment.apps, store.resolve())
        except AppSourceError as exc:
            if json:
                _err.print_json(_json.dumps({"ok": False, "error": str(exc)}))
            else:
                _err.print(Text("✗ ", style="bold red") + Text(str(exc)))
            raise typer.Exit(1)

    try:
        plan = build_plan(recipe, instance_values=instance_values)
    except ValueError as exc:
        if json:
            _err.print_json(_json.dumps({"ok": False, "error": str(exc)}))
        else:
            _err.print(Text("✗ ", style="bold red") + Text(str(exc)))
        raise typer.Exit(1)

    if json:
        _print_dry_run_json(recipe, plan)
    else:
        _print_dry_run_human(recipe, plan)
        if commands:
            _print_dry_run_commands(recipe, plan, Path(recipe_path).resolve().parent)


def _print_dry_run_json(recipe, plan) -> None:
    from ygor.expander import SEEDS_ALGORITHM

    data = {
        "ok": True,
        "name": recipe.meta.name if recipe.meta else None,
        "n_param_tuples": plan.n_param_tuples,
        "n_seeds": len(plan.seeds) if plan.seeds else 0,
        "total_executions": plan.total_executions,
        "seeds_algorithm": SEEDS_ALGORITHM if plan.seeds else None,
        "seeds_preview": plan.seeds[:5] if plan.seeds else [],
        "param_tuples_preview": plan.param_tuples[:3],
        "commands": recipe.command,
        "post": recipe.post,
    }
    _out.print_json(_json.dumps(data))


def _print_dry_run_human(recipe, plan) -> None:
    name = recipe.meta.name if recipe.meta else "unnamed"
    _out.rule(f"[bold]Dry run — {name}[/bold]")

    if recipe.meta and recipe.meta.description:
        _out.print(f"[dim]{recipe.meta.description.strip()}[/dim]\n")

    # --- Parameters table ---
    if plan.dim_sizes:
        tbl = Table(title="Parameter dimensions", show_header=True, header_style="bold cyan")
        tbl.add_column("Dimension", style="green")
        tbl.add_column("Values", justify="right")
        tbl.add_column("Sample", style="dim")

        for dim, size in plan.dim_sizes.items():
            # find back the raw expanded list from the first tuple-sample
            sample_vals = sorted(
                {t[dim] for t in plan.param_tuples[:20]},
                key=lambda x: (isinstance(x, str), x),
            )[:4]
            sample_str = ", ".join(str(v) for v in sample_vals)
            if size > 4:
                sample_str += ", …"
            tbl.add_row(dim, str(size), sample_str)
        _out.print(tbl)

    # --- Execution count ---
    _out.print(
        f"\n  Param tuples:      [bold]{plan.n_param_tuples:,}[/bold]\n"
        f"  Seeds:             [bold]{len(plan.seeds) if plan.seeds else '—'}[/bold]"
        + (
            f"  (meta-seed={recipe.parameters.seeds.meta_seed})"
            if recipe.parameters and recipe.parameters.seeds
            else ""
        )
        + f"\n  [bold]Total executions:  {plan.total_executions:,}[/bold]"
    )

    # --- Seeds preview ---
    if plan.seeds:
        preview = ", ".join(str(s) for s in plan.seeds[:5])
        if len(plan.seeds) > 5:
            preview += ", …"
        _out.print(f"\n  Seeds: [{preview}]")

    # --- Command ---
    _out.print("\n[bold cyan]Command(s):[/bold cyan]")
    for i, cmd in enumerate(recipe.command):
        _out.print(f"  [{i}] {cmd.strip()}")

    if recipe.post:
        _out.print("\n[bold cyan]Post:[/bold cyan]")
        for i, cmd in enumerate(recipe.post):
            _out.print(f"  [{i}] {cmd.strip()}")

    _out.print()


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

_STATUS_STYLE: dict[str, str] = {
    "completed": "green",
    "failed": "red",
    "running": "yellow",
    "paused": "dim",
}


def _dedup_paths(paths: list[Path]) -> list[Path]:
    seen: set[Path] = set()
    result: list[Path] = []
    for path in paths:
        resolved = path.resolve()
        if resolved not in seen:
            seen.add(resolved)
            result.append(resolved)
    return result


def _resolve_recipe_bindings(recipe: Recipe) -> ResolvedBindings:
    """Resolve recipe bindings from .ygorrc.yaml, if the recipe declares any."""
    if not (recipe.environment and recipe.environment.bindings):
        return ResolvedBindings()
    try:
        return resolve_bindings(
            recipe.environment.bindings,
            _read_project_config(),
            config_dir=Path.cwd(),
        )
    except BindingResolutionError as exc:
        _err.print(Text("✗ ", style="bold red") + Text(str(exc)))
        raise typer.Exit(1)


def _merged_environment_variables(recipe: Recipe, bindings: ResolvedBindings) -> dict[str, Any]:
    base = recipe.environment.variables if recipe.environment else {}
    try:
        return merge_binding_env(base, bindings.env_vars)
    except BindingResolutionError as exc:
        _err.print(Text("✗ ", style="bold red") + Text(str(exc)))
        raise typer.Exit(1)


def _apply_bindings_to_apptainer_config(
    recipe: Recipe,
    container_config: ApptainerConfig | None,
    bindings: ResolvedBindings,
) -> ApptainerConfig | None:
    if container_config is None:
        return None
    container_config.env_vars = _merged_environment_variables(recipe, bindings)
    container_config.mounts = [*container_config.mounts, *bindings.mounts]
    return container_config


def _prepare_apptainer_environment(
    recipe: Recipe,
    recipe_path: Path,
    store: Path,
    bindings: ResolvedBindings,
) -> ApptainerConfig | None:
    """Build the ApptainerConfig for a recipe, if it declares a base image."""
    base_image = recipe.environment.base_image if recipe.environment else None
    if not base_image:
        return None

    try:
        check_apptainer()
    except ApptainerNotAvailable as exc:
        _err.print(Text("✗ ", style="bold red") + Text(str(exc)))
        raise typer.Exit(1)

    apt_packages: list[str] = []
    python_reqs_content = ""
    apt_snapshot_str: str | None = None
    image_build_run = (
        list(recipe.environment.image_build.run)
        if recipe.environment and recipe.environment.image_build
        else []
    )
    try:
        image_build_secrets = resolve_image_build_secrets(
            image_build_run,
            _read_project_config(),
        )
    except SecretResolutionError as exc:
        _err.print(Text("✗ ", style="bold red") + Text(str(exc)))
        raise typer.Exit(1)
    recipe_dir = Path(recipe_path).resolve().parent

    if recipe.environment and recipe.environment.apps:
        from ygor.schema import AptConfig, PythonConfig
        for app_cfg in recipe.environment.apps.values():
            if isinstance(app_cfg, AptConfig):
                apt_packages.extend(app_cfg.packages)
                if app_cfg.snapshot is not None:
                    apt_snapshot_str = app_cfg.snapshot.strftime("%Y%m%d")
            elif isinstance(app_cfg, PythonConfig) and app_cfg.requirements:
                req_path = Path(app_cfg.requirements)
                candidates = [req_path]
                if not req_path.is_absolute():
                    candidates.append(recipe_dir / req_path)
                for candidate in candidates:
                    if candidate.exists():
                        python_reqs_content = candidate.read_text(encoding="utf-8")
                        break
                else:
                    _err.print(
                        Text("✗ ", style="bold red")
                        + Text(f"Python requirements file not found: {app_cfg.requirements}")
                    )
                    raise typer.Exit(1)

    try:
        image_tag = ensure_ygor_image(
            base_image, apt_packages, python_reqs_content, store,
            apt_snapshot=apt_snapshot_str,
            image_build_run=image_build_secrets.run,
            build_secrets=image_build_secrets.build_secrets,
            on_status=lambda msg: _out.print(f"  [dim]{msg}[/dim]"),
        )
    except ApptainerBuildError as exc:
        _err.print(Text("✗ ", style="bold red") + Text(exc.message))
        raise typer.Exit(1)

    env_vars = _merged_environment_variables(recipe, bindings)
    resources = recipe.environment.resources if recipe.environment else None
    mounts: list[Path | ApptainerMount] = [*_dedup_paths([store, recipe_dir]), *bindings.mounts]
    container_config = ApptainerConfig(
        image=image_tag,
        env_vars=env_vars,
        mounts=mounts,
        cpu_limit=resources.cpu if resources else None,
        memory_limit=resources.memory if resources else None,
    )
    _out.print(f"  [dim]image:[/dim] {image_tag}")
    return container_config


def _apptainer_config_to_manifest(
    config: ApptainerConfig | None,
    bindings: ResolvedBindings | None = None,
) -> dict[str, Any] | None:
    if config is None:
        return None
    binding_env_names = set((bindings.env_vars if bindings else {}).keys())
    return {
        "image": config.image,
        "env_vars": {
            k: v for k, v in config.env_vars.items()
            if k not in binding_env_names
        },
        "mounts": [str(p) for p in config.mounts if isinstance(p, Path)],
        "cpu_limit": config.cpu_limit,
        "memory_limit": config.memory_limit,
    }


def _apptainer_config_from_manifest(data: dict[str, Any] | None) -> ApptainerConfig | None:
    if not data:
        return None
    image = data.get("image")
    if not image:
        return None
    return ApptainerConfig(
        image=image,
        env_vars=dict(data.get("env_vars") or {}),
        mounts=[Path(p) for p in (data.get("mounts") or [])],
        cpu_limit=data.get("cpu_limit"),
        memory_limit=data.get("memory_limit"),
    )


_docker_config_to_manifest = _apptainer_config_to_manifest
_docker_config_from_manifest = _apptainer_config_from_manifest
_prepare_docker_environment = _prepare_apptainer_environment
_apply_bindings_to_docker_config = _apply_bindings_to_apptainer_config


def _execution_plan_to_manifest(
    executions: list[tuple[int, dict[str, Any], int]],
) -> dict[str, Any]:
    return {
        "executions": [
            {"exec_id": exec_id, "params": params, "seed": seed}
            for exec_id, params, seed in executions
        ],
    }


def _execution_plan_from_manifest(
    manifest: dict[str, Any],
    recipe: Recipe,
) -> list[tuple[int, dict[str, Any], int]]:
    plan = manifest.get("plan")
    if not isinstance(plan, dict):
        return _execution_plan_from_frozen_recipe(manifest, recipe)

    raw_executions = plan.get("executions")
    if not isinstance(raw_executions, list):
        return _execution_plan_from_frozen_recipe(manifest, recipe)

    executions: list[tuple[int, dict[str, Any], int]] = []
    for item in raw_executions:
        if not isinstance(item, dict):
            _err.print("Run manifest contains a malformed frozen execution plan.")
            raise typer.Exit(1)
        try:
            exec_id = int(item["exec_id"])
            params = dict(item.get("params") or {})
            seed = int(item["seed"])
        except (KeyError, TypeError, ValueError):
            _err.print("Run manifest contains a malformed frozen execution plan.")
            raise typer.Exit(1)
        executions.append((exec_id, params, seed))
    return executions


def _execution_plan_from_frozen_recipe(
    manifest: dict[str, Any],
    recipe: Recipe,
) -> list[tuple[int, dict[str, Any], int]]:
    _err.print(
        "Run manifest has no frozen execution plan; "
        "rebuilding from the frozen recipe snapshot."
    )
    try:
        rebuilt = build_plan(recipe)
    except ValueError as exc:
        _err.print(f"Cannot rebuild execution plan from frozen recipe: {exc}")
        raise typer.Exit(1)

    seeds_info = manifest.get("seeds") or {}
    frozen_seeds = seeds_info.get("values") if isinstance(seeds_info, dict) else None
    seeds = list(frozen_seeds) if isinstance(frozen_seeds, list) else list(rebuilt.seeds)

    if seeds:
        raw_pairs = [(pt, int(s)) for pt in rebuilt.param_tuples for s in seeds]
    else:
        raw_pairs = [(pt, 0) for pt in rebuilt.param_tuples]
    return [(i + 1, pt, seed) for i, (pt, seed) in enumerate(raw_pairs)]


def _recipe_from_manifest(manifest: dict[str, Any]) -> Recipe:
    recipe_dict = manifest.get("recipe")
    if not isinstance(recipe_dict, dict) or not recipe_dict:
        _err.print("Run manifest does not contain a frozen recipe; cannot resume reproducibly.")
        raise typer.Exit(1)
    try:
        return Recipe.model_validate(recipe_dict)
    except Exception as exc:
        _err.print(f"Cannot parse frozen recipe from manifest: {exc}")
        raise typer.Exit(1)


def _resolved_from_manifest(manifest: dict[str, Any], recipe: Recipe) -> dict[str, Any]:
    resolved = manifest.get("resolved")
    if not isinstance(resolved, dict):
        resolved = {}

    datasets = dict(resolved.get("datasets") or {})
    apps = dict(resolved.get("apps") or {})
    container = resolved.get("apptainer") or resolved.get("docker")

    if recipe.datasets and set(datasets) != set(recipe.datasets):
        _err.print(
            "Run manifest does not contain frozen resolved dataset paths; "
            "cannot resume reproducibly."
        )
        raise typer.Exit(1)

    app_sources = {
        name
        for name, cfg in (recipe.environment.apps.items() if recipe.environment and recipe.environment.apps else [])
        if isinstance(cfg, AppConfig) and cfg.source is not None
    }
    if app_sources and set(apps) != app_sources:
        _err.print(
            "Run manifest does not contain frozen resolved app paths; "
            "cannot resume reproducibly."
        )
        raise typer.Exit(1)

    if recipe.environment and recipe.environment.base_image and not container:
        _err.print(
            "Run manifest does not contain frozen Apptainer metadata; "
            "cannot resume reproducibly."
        )
        raise typer.Exit(1)

    return {
        "datasets": datasets,
        "apps": apps,
        "container": container,
        "recipe_dir": resolved.get("recipe_dir"),
    }


def _fmt_duration(sec: float | None) -> str:
    if sec is None:
        return "—"
    s = int(sec)
    if s < 60:
        return f"{s}s"
    m = s // 60
    if m < 60:
        return f"{m}m {s % 60}s"
    h = m // 60
    return f"{h}h {m % 60}m"


def _find_run_matches(store_root: Path, hash_prefix: str) -> list[tuple[Path, dict[str, Any]]]:
    runs_dir = store_root / "runs"
    if not runs_dir.exists():
        return []

    matches: list[tuple[Path, dict[str, Any]]] = []
    for run_dir in runs_dir.iterdir():
        if not run_dir.is_dir() or not run_dir.name.startswith(hash_prefix):
            continue
        manifest_path = run_dir / "manifest.json"
        if not manifest_path.exists():
            continue
        try:
            m: dict[str, Any] = _json.loads(manifest_path.read_text(encoding="utf-8"))
            matches.append((run_dir, m))
        except Exception:
            continue
    return matches


def _find_run(store_root: Path, hash_prefix: str) -> tuple[Path, dict[str, Any]]:
    """Locate a run by hash prefix. Raises typer.Exit(1) on no match or ambiguity."""
    runs_dir = store_root / "runs"
    if not runs_dir.exists():
        _err.print(f"No runs in store '{store_root}'.")
        raise typer.Exit(1)

    matches = _find_run_matches(store_root, hash_prefix)
    if not matches:
        _err.print(f"No run found matching '{hash_prefix}'.")
        raise typer.Exit(1)
    if len(matches) > 1:
        ambiguous = "  ".join(r.name[:16] for r, _ in matches)
        _err.print(f"Ambiguous prefix '{hash_prefix}' matches multiple runs:\n  {ambiguous}")
        raise typer.Exit(1)

    return matches[0]


def _recipe_name_from_manifest(manifest: dict[str, Any]) -> str:
    recipe_dict: dict[str, Any] = manifest.get("recipe") or {}
    meta: dict[str, Any] = recipe_dict.get("meta") or {}
    recipe_path = Path(manifest.get("recipe_path") or "")
    return meta.get("name") or recipe_path.parent.name or recipe_path.stem or "(unnamed)"


def _recipe_matches(manifest: dict[str, Any], query: str) -> bool:
    recipe_dict: dict[str, Any] = manifest.get("recipe") or {}
    meta: dict[str, Any] = recipe_dict.get("meta") or {}
    recipe_path_raw = str(manifest.get("recipe_path") or "")
    recipe_path = Path(recipe_path_raw)
    candidates = {
        _recipe_name_from_manifest(manifest),
        str(meta.get("name") or ""),
        recipe_path_raw,
        recipe_path.name,
        recipe_path.stem,
    }
    normalized_query = query.lower()
    return any(candidate.lower() == normalized_query for candidate in candidates if candidate)


def _read_project_config() -> dict[str, Any]:
    from ruamel.yaml import YAML

    merged: dict[str, Any] = {}
    yaml = YAML()
    for path in (Path.cwd() / LEGACY_CONFIG_FILE, Path.cwd() / _CONFIG_FILE):
        if not path.exists():
            continue
        raw = yaml.load(path.read_text(encoding="utf-8")) or {}
        if not isinstance(raw, dict):
            continue
        for key, value in raw.items():
            if isinstance(value, dict) and isinstance(merged.get(key), dict):
                merged[key] = {**merged[key], **value}
            else:
                merged[key] = value
    return merged


def _sync_remote(remote: str | None) -> str:
    if remote:
        return remote
    cfg = _read_project_config()
    sync_cfg = cfg.get("sync") if isinstance(cfg.get("sync"), dict) else {}
    configured = sync_cfg.get("remote") or sync_cfg.get("destination")
    if configured:
        return str(configured)
    _err.print("No sync remote configured. Use --remote or set sync.remote in .ygorrc.yaml.")
    raise typer.Exit(1)


def _collect_runs(
    store_root: Path,
    *,
    status_filter: str | None = None,
    recipe_filter: str | None = None,
    since_filter: str | None = None,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    runs_dir = store_root / "runs"
    if not runs_dir.exists():
        return []

    published = _published_hashes(_store.read_publications(store_root))
    summaries: list[dict[str, Any]] = []
    for run_dir in runs_dir.iterdir():
        if not run_dir.is_dir():
            continue
        manifest_path = run_dir / "manifest.json"
        if not manifest_path.exists():
            continue
        try:
            m: dict[str, Any] = _json.loads(manifest_path.read_text(encoding="utf-8"))
        except Exception:
            continue

        state: dict[str, Any] = m.get("state") or {}
        status = state.get("status", "unknown")
        if status_filter and status != status_filter:
            continue

        started_at: str | None = state.get("started_at")
        if since_filter and (started_at or "") < since_filter:
            continue

        recipe_name = _recipe_name_from_manifest(m)
        if recipe_filter and not _recipe_matches(m, recipe_filter):
            continue

        ended_at: str | None = state.get("ended_at")
        duration_sec: float | None = None
        if started_at:
            try:
                t0 = datetime.fromisoformat(started_at)
                t1 = datetime.fromisoformat(ended_at) if ended_at else datetime.now(timezone.utc)
                duration_sec = (t1 - t0).total_seconds()
            except Exception:
                pass

        counts: dict[str, int] = state.get("counts") or {}
        run_hash: str = m.get("run_hash") or run_dir.name

        summaries.append({
            "run_hash": run_hash,
            "hash_short": run_hash[:8],
            "recipe_name": recipe_name,
            "recipe_path": m.get("recipe_path") or "",
            "status": status,
            "started_at": started_at,
            "ended_at": ended_at,
            "duration_sec": duration_sec,
            "total_executions": state.get("total_executions") or 0,
            "completed": counts.get("completed", 0),
            "failed": counts.get("failed", 0),
            "published": run_hash in published,
        })

    summaries.sort(key=lambda r: r["started_at"] or "", reverse=True)
    if limit is not None:
        return summaries[:limit]
    return summaries


def _latest_run_for_recipe(store: Path, recipe: str, *, any_status: bool = False) -> dict[str, Any]:
    runs = _collect_runs(
        store,
        status_filter=None if any_status else "completed",
        recipe_filter=recipe,
        limit=1,
    )
    if not runs:
        qualifier = "" if any_status else "completed "
        _err.print(f"No {qualifier}run found for recipe '{recipe}'.")
        raise typer.Exit(1)
    return runs[0]


def _select_run(
    store: Path,
    selector: str | None,
    *,
    recipe: str | None = None,
    status_filter: str | None = None,
    latest: bool = False,
    default_latest: bool = False,
    selector_may_be_recipe: bool = False,
) -> tuple[Path, dict[str, Any]]:
    if selector and recipe:
        _err.print("Specify either a run selector or --recipe, not both.")
        raise typer.Exit(1)

    if selector:
        matches = _find_run_matches(store, selector)
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            ambiguous = "  ".join(r.name[:16] for r, _ in matches)
            _err.print(f"Ambiguous prefix '{selector}' matches multiple runs:\n  {ambiguous}")
            raise typer.Exit(1)
        if selector_may_be_recipe:
            recipe = selector
        else:
            _err.print(f"No run found matching '{selector}'.")
            raise typer.Exit(1)

    if latest or recipe or status_filter or default_latest:
        runs = _collect_runs(
            store,
            status_filter=status_filter,
            recipe_filter=recipe,
            limit=1,
        )
        if not runs:
            parts: list[str] = []
            if recipe:
                parts.append(f"recipe '{recipe}'")
            if status_filter:
                parts.append(f"status '{status_filter}'")
            qualifier = " matching " + " and ".join(parts) if parts else ""
            _err.print(f"No run found{qualifier}.")
            raise typer.Exit(1)
        return _find_run(store, runs[0]["run_hash"])

    _err.print("Specify a run hash/prefix or use --latest, --recipe, or --status.")
    raise typer.Exit(1)


def _open_directory(path: Path) -> None:
    if sys.platform == "darwin":
        cmd = ["open", str(path)]
    elif os.name == "nt":
        cmd = ["explorer", str(path)]
    else:
        cmd = ["xdg-open", str(path)]

    try:
        subprocess.Popen(cmd)
    except OSError as exc:
        _err.print(f"Could not open '{path}': {exc}")
        raise typer.Exit(1)


def _fmt_dt(iso: str | None) -> str:
    if not iso:
        return "—"
    try:
        return datetime.fromisoformat(iso).strftime("%Y-%m-%d %H:%M")
    except Exception:
        return iso[:16]


def _print_list_table(runs: list[dict[str, Any]]) -> None:
    tbl = Table(show_header=True, header_style="bold", box=None, padding=(0, 2))
    tbl.add_column("HASH", style="dim", no_wrap=True)
    tbl.add_column("RECIPE")
    tbl.add_column("STARTED", no_wrap=True)
    tbl.add_column("STATUS", no_wrap=True)
    tbl.add_column("PUB", justify="center", no_wrap=True)
    tbl.add_column("PROGRESS", justify="right")

    for r in runs:
        status = r["status"]
        style = _STATUS_STYLE.get(status, "")
        status_cell = f"[{style}]{status}[/{style}]" if style else status

        total: int = r["total_executions"]
        done: int = r["completed"]
        failed: int = r["failed"]

        if status == "completed" or total == 0:
            progress = "-"
        elif failed:
            progress = f"{done}/{total} [red]({failed} failed)[/red]"
        else:
            progress = f"{done}/{total}"

        tbl.add_row(
            r["hash_short"],
            r["recipe_name"],
            _fmt_dt(r["started_at"]),
            status_cell,
            "yes" if r.get("published") else "-",
            progress,
        )

    _out.print(tbl)
    _out.print(f"\n[dim]{len(runs)} run(s)[/dim]")


# ---------------------------------------------------------------------------
# ygor list
# ---------------------------------------------------------------------------


@app.command(name="list", rich_help_panel=_HELP_PANEL_RUN)
def list_runs(
    store: Annotated[Path, typer.Option("--store", help="Store root directory.")] = _DEFAULT_STORE,
    json: Annotated[bool, typer.Option("--json", help="Output as JSON.")] = False,
    status: Annotated[str | None, typer.Option("--status", help="Filter by status (running, completed, failed, aborted).")] = None,
    recipe: Annotated[
        str | None,
        typer.Option("--recipe", help="Filter by recipe name, path, file name, or stem."),
    ] = None,
    since: Annotated[str | None, typer.Option("--since", help="Only show runs started on or after this date (YYYY-MM-DD).")] = None,
    limit: Annotated[int | None, typer.Option("--limit", help="Maximum number of runs to show.")] = None,
) -> None:
    """List runs in the store, newest first."""
    store = store.resolve()
    runs = _collect_runs(
        store,
        status_filter=status,
        recipe_filter=recipe,
        since_filter=since,
        limit=limit,
    )

    if json:
        _out.print_json(_json.dumps(runs))
        return

    if not runs:
        _out.print("[dim]No runs found.[/dim]")
        return

    _print_list_table(runs)


# ---------------------------------------------------------------------------
# ygor latest
# ---------------------------------------------------------------------------


@app.command(rich_help_panel=_HELP_PANEL_RUN)
def latest(
    recipe: Annotated[str, typer.Argument(help="Recipe name, path, file name, or stem.", metavar="RECIPE")],
    store: Annotated[Path, typer.Option("--store", help="Store root directory.")] = _DEFAULT_STORE,
    any_status: Annotated[bool, typer.Option("--any-status", help="Include runs of any status, not just completed.")] = False,
) -> None:
    """Print the hash of the most recent run for a recipe.

    By default only completed runs are considered.
    Use --any-status to include running, failed, and other statuses.
    """
    store = store.resolve()
    run_info = _latest_run_for_recipe(store, recipe, any_status=any_status)
    _out.print(run_info["hash_short"])


# ---------------------------------------------------------------------------
# ygor open
# ---------------------------------------------------------------------------


@app.command(name="open", rich_help_panel=_HELP_PANEL_RUN)
def open_results(
    run: Annotated[
        str | None,
        typer.Argument(help="Run hash/prefix, or recipe name/path/file/stem for compatibility.", metavar="RUN"),
    ] = None,
    store: Annotated[Path, typer.Option("--store", help="Store root directory.")] = _DEFAULT_STORE,
    recipe: Annotated[
        str | None,
        typer.Option("--recipe", help="Select the most recent run for this recipe name, path, file name, or stem."),
    ] = None,
    status_filter: Annotated[str | None, typer.Option("--status", help="Select the most recent run with this status.")] = None,
    latest: Annotated[bool, typer.Option("--latest", help="Open the most recent run.")] = False,
) -> None:
    """Open a run directory."""
    store = store.resolve()
    run_dir, manifest = _select_run(
        store,
        run,
        recipe=recipe,
        status_filter=status_filter,
        latest=latest,
        default_latest=run is None and recipe is None and status_filter is None,
        selector_may_be_recipe=True,
    )
    if not run_dir.is_dir():
        run_hash = (manifest.get("run_hash") or run_dir.name)[:8]
        _err.print(f"Run directory not found for run {run_hash}: {run_dir}")
        raise typer.Exit(1)
    _open_directory(run_dir)
    _out.print(str(run_dir), soft_wrap=True)


# ---------------------------------------------------------------------------
# ygor publish / unpublish / sync
# ---------------------------------------------------------------------------


@app.command(rich_help_panel=_HELP_PANEL_SHARING)
def publish(
    hash_prefix: Annotated[str, typer.Argument(help="Run hash or unique prefix to mark as publishable.")],
    store: Annotated[Path, typer.Option("--store", help="Store root directory.")] = _DEFAULT_STORE,
) -> None:
    """Mark a local run as publishable for the next sync."""
    store = store.resolve()
    _, manifest = _find_run(store, hash_prefix)
    run_hash = manifest.get("run_hash") or hash_prefix
    index = _store.read_publications(store)
    runs = index.setdefault("runs", {})
    runs[run_hash] = {
        "published": True,
        "published_at": _manifest._now_iso(),
        "recipe": _recipe_name_from_manifest(manifest),
        "status": (manifest.get("state") or {}).get("status"),
    }
    _store.write_publications(store, index)
    _out.print(f"Marked [bold]{run_hash[:8]}[/bold] as publishable.")


@app.command(rich_help_panel=_HELP_PANEL_SHARING)
def unpublish(
    hash_prefix: Annotated[str, typer.Argument(help="Run hash or unique prefix to unmark locally.")],
    store: Annotated[Path, typer.Option("--store", help="Store root directory.")] = _DEFAULT_STORE,
) -> None:
    """Remove the local publishable mark for a run. Does not delete remote data."""
    store = store.resolve()
    _, manifest = _find_run(store, hash_prefix)
    run_hash = manifest.get("run_hash") or hash_prefix
    index = _store.read_publications(store)
    runs = index.setdefault("runs", {})
    runs.pop(run_hash, None)
    _store.write_publications(store, index)
    _out.print(f"Unmarked [bold]{run_hash[:8]}[/bold].")


@app.command(rich_help_panel=_HELP_PANEL_SHARING)
def sync(
    store: Annotated[Path, typer.Option("--store", help="Local store root directory.")] = _DEFAULT_STORE,
    remote: Annotated[str | None, typer.Option("--remote", help="Remote store path or rsync destination.")] = None,
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Show what would be copied without changing either store.")] = False,
) -> None:
    """Synchronize publishable runs with a remote shared store."""
    store = store.resolve()
    remote_spec = _sync_remote(remote)
    try:
        result = sync_stores(
            store=store,
            remote_spec=remote_spec,
            dry_run=dry_run,
            on_action=lambda action, run_hash: _out.print(f"{action} {run_hash[:8]}"),
        )
    except SyncError as exc:
        _err.print(str(exc))
        raise typer.Exit(1)

    if not result.push_hashes and not result.pull_hashes:
        _out.print("[dim]Nothing to sync.[/dim]")
    elif dry_run:
        _out.print(
            f"[yellow]Dry run[/yellow] {len(result.push_hashes)} push(es), "
            f"{len(result.pull_hashes)} pull(s)."
        )
    else:
        _out.print(
            f"[green]Synced[/green] {len(result.push_hashes)} pushed, "
            f"{len(result.pull_hashes)} pulled."
        )


# ---------------------------------------------------------------------------
# ygor show — helpers + command
# ---------------------------------------------------------------------------


def _show_kv(label: str, value: str, *, width: int = 14) -> None:
    _out.print(f"  [dim]{label:<{width}}[/dim]  {value}")


def _ordered_unique(values: list[Any]) -> list[Any]:
    seen: set[str] = set()
    result: list[Any] = []
    for value in values:
        key = repr(value)
        if key in seen:
            continue
        seen.add(key)
        result.append(value)
    return result


def _show_list(title: str, values: list[Any]) -> None:
    if not values:
        return
    _out.rule(f"[bold cyan]{title}[/bold cyan]")
    for value in values:
        _out.print(f"  {value}")
    _out.print()


def _instance_patterns_from_recipe(recipe_dict: dict[str, Any]) -> list[str]:
    params_dict: dict[str, Any] = recipe_dict.get("parameters") or {}
    raw = params_dict.get("instance") or []
    return [str(item) for item in raw] if isinstance(raw, list) else []


def _plan_executions(m: dict[str, Any]) -> list[dict[str, Any]]:
    plan = m.get("plan") or {}
    executions = plan.get("executions") if isinstance(plan, dict) else None
    if isinstance(executions, list):
        return [item for item in executions if isinstance(item, dict)]
    raw_executions = m.get("executions") or []
    return [item for item in raw_executions if isinstance(item, dict)]


def _seeds_used(m: dict[str, Any]) -> list[Any]:
    executions = _plan_executions(m)
    if executions:
        return _ordered_unique([item.get("seed") for item in executions if "seed" in item])

    seeds = m.get("seeds") or {}
    values = seeds.get("values") if isinstance(seeds, dict) else None
    return list(values) if isinstance(values, list) else []


def _concrete_instances_used(m: dict[str, Any]) -> list[str]:
    instances: list[Any] = []
    for item in _plan_executions(m):
        params = item.get("params") or {}
        if isinstance(params, dict) and "instance" in params:
            instances.append(params["instance"])
    return [str(item) for item in _ordered_unique(instances)]


def _print_frozen_recipe(m: dict[str, Any], run_dir: Path) -> None:
    lock_path = run_dir / "recipe.lock.yaml"
    if lock_path.exists():
        _out.print(lock_path.read_text(encoding="utf-8").rstrip(), markup=False, highlight=False)
        return

    recipe_dict = m.get("recipe") or {}
    if not isinstance(recipe_dict, dict) or not recipe_dict:
        _err.print("Run manifest does not contain a frozen recipe.")
        raise typer.Exit(1)

    from ruamel.yaml import YAML

    stream = StringIO()
    yaml = YAML()
    yaml.default_flow_style = False
    yaml.dump(recipe_dict, stream)
    _out.print(stream.getvalue().rstrip(), markup=False, highlight=False)


def _print_show_human(m: dict[str, Any], run_dir: Path, *, long: bool = False) -> None:
    state: dict[str, Any] = m.get("state") or {}
    recipe_dict: dict[str, Any] = m.get("recipe") or {}
    meta: dict[str, Any] = recipe_dict.get("meta") or {}

    run_hash: str = m.get("run_hash") or run_dir.name
    recipe_name = meta.get("name") or Path(m.get("recipe_path") or "").parent.name or "(unnamed)"
    status = state.get("status", "unknown")
    style = _STATUS_STYLE.get(status, "")
    status_badge = f"[{style}]{status}[/{style}]" if style else status

    _out.rule(f"[bold]{run_hash[:8]}[/bold]  {recipe_name}  {status_badge}")
    _out.print()

    # --- timeline ---
    started_at = state.get("started_at")
    ended_at = state.get("ended_at")
    started_str = _fmt_dt(started_at)
    if ended_at:
        try:
            dur = _fmt_duration(
                (datetime.fromisoformat(ended_at) - datetime.fromisoformat(started_at)).total_seconds()
            )
        except Exception:
            dur = "—"
        _show_kv("Started", f"{started_str}  →  {_fmt_dt(ended_at)}  ({dur})")
    else:
        _show_kv("Started", started_str)

    _show_kv("Recipe", m.get("recipe_path") or "—")
    if meta.get("description"):
        _show_kv("Description", meta["description"].strip())

    # --- execution stats ---
    counts: dict[str, int] = state.get("counts") or {}
    total = state.get("total_executions") or 0
    completed = counts.get("completed", 0)
    failed = counts.get("failed", 0)
    pending = counts.get("pending", 0)

    parts: list[str] = [f"[bold]{total:,}[/bold] total"]
    if completed:
        parts.append(f"[green]✓ {completed:,}[/green]")
    if failed:
        parts.append(f"[red]✗ {failed:,}[/red]")
    if pending:
        parts.append(f"[dim]{pending:,} pending[/dim]")
    _show_kv("Executions", "  ".join(parts))

    post_records: list[dict[str, Any]] = m.get("post") or []
    if post_records:
        n_fail = sum(1 for p in post_records if p.get("status") == "failed")
        if n_fail:
            post_summary = f"{len(post_records)} command(s)  [red]✗ {n_fail} failed[/red]"
        else:
            post_summary = f"{len(post_records)} command(s)  [green]all completed[/green]"
        _show_kv("Post", post_summary)

    _out.print()

    # --- paths ---
    _show_kv("Run dir", str(run_dir))
    _show_kv("Outputs", str(run_dir / "outputs"))
    _show_kv("Logs", str(run_dir / "logs"))
    _out.print()

    # --- commands ---
    commands: list[str] = recipe_dict.get("command") or []
    if commands:
        _out.rule("[bold cyan]Command(s)[/bold cyan]")
        for i, cmd in enumerate(commands):
            _out.print(f"  [{i}]  {cmd.strip()}")
        _out.print()

    # --- inputs ---
    instance_patterns = _instance_patterns_from_recipe(recipe_dict)
    if instance_patterns:
        _show_list("Input Patterns", instance_patterns)
    if long:
        _show_list("Seeds Used", _seeds_used(m))
        _show_list("Concrete Instances", _concrete_instances_used(m))

    # --- post detail ---
    if post_records:
        _out.rule("[bold cyan]Post[/bold cyan]")
        for i, p in enumerate(post_records):
            s = p.get("status", "unknown")
            sty = _STATUS_STYLE.get(s, "")
            badge = f"[{sty}]{s}[/{sty}]" if sty else s
            _out.print(f"  [{i}]  {p.get('command', '—')}  {badge}")
        _out.print()

    # --- parameters ---
    params_dict: dict[str, Any] = recipe_dict.get("parameters") or {}
    values: dict[str, Any] = params_dict.get("values") or {}
    if values:
        _out.rule("[bold cyan]Parameters[/bold cyan]")
        for dim, val in values.items():
            if isinstance(val, list):
                preview = str(val) if len(val) <= 5 else f"[{', '.join(str(v) for v in val[:4])}, …]"
                _out.print(f"  {dim:<24}  {preview}  ({len(val)} values)")
            elif isinstance(val, dict):
                from_ = val.get("from")
                to_ = val.get("to")
                step = val.get("step")
                count = val.get("count")
                scale = val.get("scale", "")
                if step is not None:
                    _out.print(f"  {dim:<24}  {from_} → {to_}, step {step}")
                elif count is not None:
                    extra = f", {scale} scale" if scale else ""
                    _out.print(f"  {dim:<24}  {from_} → {to_}, {count} values{extra}")
                else:
                    _out.print(f"  {dim:<24}  {val}")
            else:
                _out.print(f"  {dim:<24}  {val}")
        _out.print()

    # --- seeds ---
    seeds: dict[str, Any] = m.get("seeds") or {}
    if seeds:
        _out.rule("[bold cyan]Seeds[/bold cyan]")
        _out.print(
            f"  meta-seed [bold]{seeds.get('meta_seed')}[/bold]"
            f"  ·  {seeds.get('count')} seeds"
            f"  ·  {seeds.get('algorithm')}"
        )
        _out.print()

    # --- host ---
    host: dict[str, Any] = m.get("host") or {}
    if host:
        _out.rule("[bold cyan]Host[/bold cyan]")
        host_parts: list[str] = []
        if host.get("hostname"):
            host_parts.append(host["hostname"])
        if host.get("os"):
            host_parts.append(host["os"])
        if host.get("cpu_count_logical"):
            host_parts.append(f"{host['cpu_count_logical']} CPUs")
        if host.get("memory_total_gb"):
            host_parts.append(f"{host['memory_total_gb']} GB RAM")
        _out.print(f"  {'  ·  '.join(host_parts)}")
        _out.print()


@app.command(rich_help_panel=_HELP_PANEL_RUN)
def show(
    run: Annotated[str | None, typer.Argument(help="Run hash or unique prefix (default: most recent).", metavar="RUN")] = None,
    store: Annotated[Path, typer.Option("--store", help="Store root directory.")] = _DEFAULT_STORE,
    json: Annotated[bool, typer.Option("--json", help="Output the raw manifest as JSON.")] = False,
    long: Annotated[bool, typer.Option("--long", help="Show full seed list and concrete instance files.")] = False,
    recipe: Annotated[
        str | None,
        typer.Option("--recipe", help="Select the most recent run for this recipe name, path, file name, or stem."),
    ] = None,
    status_filter: Annotated[str | None, typer.Option("--status", help="Select the most recent run with this status.")] = None,
    latest: Annotated[bool, typer.Option("--latest", help="Show the most recent run.")] = False,
    recipe_lock: Annotated[
        bool,
        typer.Option("--recipe-lock", "--lock", help="Print the frozen recipe.yaml snapshot and exit."),
    ] = False,
) -> None:
    """Show detailed information about a run."""
    store = store.resolve()
    run_dir, manifest = _select_run(
        store,
        run,
        recipe=recipe,
        status_filter=status_filter,
        latest=latest,
        default_latest=run is None and recipe is None and status_filter is None,
    )

    if recipe_lock:
        _print_frozen_recipe(manifest, run_dir)
        return

    if json:
        _out.print_json(_json.dumps(manifest))
        return

    _print_show_human(manifest, run_dir, long=long)


# ---------------------------------------------------------------------------
# ygor run
# ---------------------------------------------------------------------------


@app.command(name="run", rich_help_panel=_HELP_PANEL_RUN)
def run(
    recipe_path: Annotated[Path, typer.Argument(help="Path to recipe.yaml.")],
    store: Annotated[Path, typer.Option("--store", help="Store root directory.")] = _DEFAULT_STORE,
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Skip confirmations.")] = False,
    no_docker: Annotated[
        bool,
        typer.Option(
            "--no-container",
            "--no-docker",
            help="Run on host even if environment.base-image is set.",
        ),
    ] = False,
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Show the execution plan without creating a run.")] = False,
    prepare: Annotated[bool, typer.Option("--prepare", help="With --dry-run, resolve/download inputs and apps before planning.")] = False,
    commands: Annotated[bool, typer.Option("--commands", help="With --dry-run, show each expanded command.")] = False,
    json: Annotated[bool, typer.Option("--json", help="With --dry-run, output the plan as JSON.")] = False,
) -> None:
    """Execute a recipe and record the run in the store."""
    store = store.resolve()
    if prepare and not dry_run:
        _err.print("--prepare can only be used with --dry-run.")
        raise typer.Exit(1)
    if commands and not dry_run:
        _err.print("--commands can only be used with --dry-run.")
        raise typer.Exit(1)
    if json and not dry_run:
        _err.print("--json can only be used with --dry-run.")
        raise typer.Exit(1)
    if dry_run:
        _run_dry_run(recipe_path, store, json=json, commands=commands, prepare=prepare)
        return

    # --- load + validate ---
    try:
        recipe = load_recipe(recipe_path)
    except RecipeParseError as exc:
        _err.print(Text("✗ ", style="bold red") + Text(str(exc)))
        raise typer.Exit(1)
    recipe_dir = Path(recipe_path).resolve().parent

    template_errors = check_templates(recipe)
    if template_errors:
        for e in template_errors:
            _err.print(Text("✗ ", style="bold red") + Text(str(e)))
        raise typer.Exit(1)

    # --- resolve local runtime bindings ---
    bindings = _resolve_recipe_bindings(recipe)
    _merged_environment_variables(recipe, bindings)

    def _status(name: str, msg: str) -> None:
        _out.print(f"  [dim]{name}[/dim]: {msg}")

    # --- resolve datasets (needed before plan to expand instance globs) ---
    dataset_paths: dict[str, str] = {}
    resolved_datasets: dict[str, Path] = {}
    if recipe.datasets:
        _out.print("[cyan]Checking datasets…[/cyan]")
        try:
            resolved_datasets = ensure_datasets(recipe.datasets, store, on_status=_status)
            dataset_paths = {k: str(v) for k, v in resolved_datasets.items()}
        except DatasetResolutionError as exc:
            _err.print(Text("✗ ", style="bold red") + Text(str(exc)))
            raise typer.Exit(1)

    # --- resolve instance globs ---
    instance_values: list[str] | None = None
    if recipe.parameters and recipe.parameters.instance:
        instance_values = resolve_instance_globs(
            recipe.parameters.instance, recipe.datasets, resolved_datasets
        )

    # --- build plan ---
    try:
        plan = build_plan(recipe, instance_values=instance_values)
    except ValueError as exc:
        _err.print(Text("✗ ", style="bold red") + Text(str(exc)))
        raise typer.Exit(1)

    executions = executions_from_plan(plan)

    # --- compute run hash ---
    recipe_text = Path(recipe_path).read_text(encoding="utf-8")
    identity = build_run_identity(recipe_text=recipe_text, recipe=recipe, store_root=store)
    started_at = identity.started_at
    run_hash = identity.run_hash
    run_dir = identity.run_dir

    # --- confirm ---
    total = len(executions)
    name = recipe.meta.name if recipe.meta else recipe_path.name
    _out.print(
        f"\nRecipe:   [bold]{name}[/bold]\n"
        f"Run:      [dim]{run_hash[:8]}[/dim]\n"
        f"Store:    {run_dir}\n"
        f"Total:    [bold]{total:,}[/bold] execution(s)\n"
    )
    if not yes and not typer.confirm("Proceed?", default=True):
        _out.print("Aborted.")
        raise typer.Exit(0)

    # --- Apptainer setup ---
    container_config: ApptainerConfig | None = None
    base_image = recipe.environment.base_image if recipe.environment else None
    if base_image and not no_docker:
        _out.print("[cyan]Preparing Apptainer environment…[/cyan]")
        container_config = _prepare_apptainer_environment(recipe, recipe_path, store, bindings)

    # --- resolve app sources ---
    app_paths: dict[str, str] = {}
    if recipe.environment and recipe.environment.apps:
        _out.print("[cyan]Checking app sources…[/cyan]")
        try:
            resolved_apps = ensure_app_sources(
                recipe.environment.apps, store, on_status=_status,
                container_config=container_config,
            )
            app_paths = {k: str(v) for k, v in resolved_apps.items()}
        except AppSourceError as exc:
            _err.print(Text("✗ ", style="bold red") + Text(str(exc)))
            raise typer.Exit(1)

    # --- set up store ---
    _store.create_run_dirs(run_dir)
    _store.write_recipe_lock(run_dir, recipe_text)
    _store.write_lock_file(run_dir)

    # --- recipe path relative to CWD for manifest ---
    try:
        rp_str = str(Path(recipe_path).resolve().relative_to(Path.cwd()))
    except ValueError:
        rp_str = str(recipe_path)

    seeds_info = seeds_info_from_plan(recipe, plan)

    # --- initial manifest ---
    mfest = _manifest.build_initial(
        run_hash=run_hash,
        recipe_path=rp_str,
        recipe_dict=recipe.model_dump(by_alias=True, mode="json"),
        seeds_info=seeds_info,
        total_executions=total,
        started_at=started_at,
    )
    mfest["plan"] = _execution_plan_to_manifest(executions)
    mfest["resolved"] = {
        "datasets": dict(dataset_paths),
        "apps": dict(app_paths),
        "recipe_dir": str(recipe_dir),
        "apptainer": _apptainer_config_to_manifest(container_config, bindings),
        "bindings": dict(bindings.manifest),
    }
    _MANIFEST_REPOSITORY.write_manifest(mfest, run_dir)

    # --- execute ---
    from rich.progress import Progress, BarColumn, MofNCompleteColumn, TimeElapsedColumn

    failed_count = 0

    with Progress(
        "[progress.description]{task.description}",
        BarColumn(),
        MofNCompleteColumn(),
        TimeElapsedColumn(),
        console=_out,
        transient=False,
    ) as progress:
        task = progress.add_task(f"[cyan]{run_hash[:8]}[/cyan]", total=total)

        def _on_progress(record: _runner.ExecRecord) -> None:
            nonlocal failed_count
            status = "✓" if record.status == "completed" else "✗"
            if record.status == "failed":
                failed_count += 1
            progress.advance(task)

        workers = recipe.policy.workers or 1
        mfest = _runner.execute_run_parallel(
            recipe, executions, run_hash, run_dir, mfest,
            _MANIFEST_REPOSITORY.write_manifest, workers=workers,
            on_progress=_on_progress,
            dataset_paths=dataset_paths,
            app_paths=app_paths,
            recipe_dir=recipe_dir,
            container_config=container_config,
        )

    # --- post ---
    if recipe.post:
        _out.print("[cyan]Running post commands…[/cyan]")
        mfest = _runner.execute_post(
            recipe, run_hash, run_dir, mfest, _MANIFEST_REPOSITORY.write_manifest,
            dataset_paths=dataset_paths,
            app_paths=app_paths,
            recipe_dir=recipe_dir,
            on_status=lambda msg: _out.print(f"  [dim]{msg}[/dim]"),
            container_config=container_config,
        )

    # --- finalise ---
    all_failed = mfest["state"]["counts"]["failed"]
    final_status = finalize_manifest_status(mfest)
    _MANIFEST_REPOSITORY.write(mfest, run_dir)

    _store.remove_lock_file(run_dir)

    completed = mfest["state"]["counts"]["completed"]
    if final_status == "paused":
        pending = mfest["state"]["counts"]["pending"]
        _out.print(
            f"\n[yellow]⏸[/yellow] Run [dim]{run_hash[:8]}[/dim] paused  "
            f"[green]{completed}[/green] completed  "
            + (f"[red]{all_failed}[/red] failed  " if all_failed else "")
            + f"[dim]{pending}[/dim] pending"
            + f"\n  Resume with: ygor resume {run_hash[:8]}"
        )
    else:
        _out.print(
            f"\n[bold green]✓[/bold green] Run [dim]{run_hash[:8]}[/dim] finished  "
            f"[green]{completed}[/green] completed  "
            + (f"[red]{all_failed}[/red] failed" if all_failed else "")
            + f"\n  Store: {run_dir}"
        )


@app.command(name="start", hidden=True)
def start(
    recipe_path: Annotated[Path, typer.Argument(help="Path to recipe.yaml.")],
    store: Annotated[Path, typer.Option("--store", help="Store root directory.")] = _DEFAULT_STORE,
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Skip confirmations.")] = False,
    no_docker: Annotated[
        bool,
        typer.Option(
            "--no-container",
            "--no-docker",
            help="Run on host even if environment.base-image is set.",
        ),
    ] = False,
) -> None:
    """Deprecated alias for `ygor run`."""
    run(recipe_path=recipe_path, store=store, yes=yes, no_docker=no_docker)


# ---------------------------------------------------------------------------
# ygor status
# ---------------------------------------------------------------------------


@app.command(rich_help_panel=_HELP_PANEL_RUN)
def status(
    run: Annotated[str | None, typer.Argument(help="Run hash or unique prefix (default: most recent).", metavar="RUN")] = None,
    store: Annotated[Path, typer.Option("--store", help="Store root directory.")] = _DEFAULT_STORE,
    json: Annotated[bool, typer.Option("--json", help="Output as JSON.")] = False,
    recipe: Annotated[
        str | None,
        typer.Option("--recipe", help="Select the most recent run for this recipe name, path, file name, or stem."),
    ] = None,
    status_filter: Annotated[str | None, typer.Option("--status", help="Select the most recent run with this status.")] = None,
    latest: Annotated[bool, typer.Option("--latest", help="Show the most recent run.")] = False,
) -> None:
    """Show execution progress for a run."""
    store = store.resolve()
    run_dir, manifest = _select_run(
        store,
        run,
        recipe=recipe,
        status_filter=status_filter,
        latest=latest,
        default_latest=run is None and recipe is None and status_filter is None,
    )

    if json:
        _out.print_json(_json.dumps(manifest.get("state", {})))
        return

    state: dict[str, Any] = manifest.get("state") or {}
    recipe_dict: dict[str, Any] = manifest.get("recipe") or {}
    meta: dict[str, Any] = recipe_dict.get("meta") or {}
    run_hash: str = manifest.get("run_hash") or run_dir.name
    recipe_name = meta.get("name") or Path(manifest.get("recipe_path") or "").parent.name or "(unnamed)"

    status_val = state.get("status", "unknown")
    style = _STATUS_STYLE.get(status_val, "")
    status_badge = f"[{style}]{status_val}[/{style}]" if style else status_val

    _out.print(f"\n[bold]{run_hash[:8]}[/bold]  {recipe_name}  {status_badge}")

    counts: dict[str, int] = state.get("counts") or {}
    total = state.get("total_executions") or 0
    completed = counts.get("completed", 0)
    failed = counts.get("failed", 0)
    running = counts.get("running", 0)
    pending = counts.get("pending", 0)

    if total > 0:
        pct = 100 * (completed + failed) / total
        bar_width = 30
        filled = int(bar_width * (completed + failed) / total)
        bar = "[green]" + "█" * filled + "[/green]" + "░" * (bar_width - filled)
        _out.print(f"  {bar}  {pct:.0f}%")

    parts: list[str] = [f"[bold]{total:,}[/bold] total"]
    if completed:
        parts.append(f"[green]✓ {completed:,}[/green]")
    if failed:
        parts.append(f"[red]✗ {failed:,}[/red]")
    if running:
        parts.append(f"[yellow]⟳ {running}[/yellow]")
    if pending:
        parts.append(f"[dim]{pending:,} pending[/dim]")
    _out.print("  " + "  ".join(parts))

    if state.get("last_updated"):
        _out.print(f"\n  [dim]Last updated: {_fmt_dt(state['last_updated'])}[/dim]")
    _out.print()


# ---------------------------------------------------------------------------
# ygor pause
# ---------------------------------------------------------------------------


@app.command(rich_help_panel=_HELP_PANEL_CONTROL)
def pause(
    hash_prefix: Annotated[str | None, typer.Argument(help="Run hash or unique prefix (default: most recent running).")] = None,
    store: Annotated[Path, typer.Option("--store", help="Store root directory.")] = _DEFAULT_STORE,
    force: Annotated[bool, typer.Option("--force", help="Stop running executions immediately and leave them pending.")] = False,
) -> None:
    """Request a pause of a running experiment."""
    store = store.resolve()

    if hash_prefix is None:
        runs = _collect_runs(store, status_filter="running", limit=1)
        if not runs:
            _err.print("No running run found. Specify a hash prefix.")
            raise typer.Exit(1)
        hash_prefix = runs[0]["run_hash"]

    run_dir, manifest = _find_run(store, hash_prefix)
    state = manifest.get("state") or {}
    current_status = state.get("status", "unknown")

    if current_status not in ("running",):
        _err.print(f"Run {hash_prefix[:8]} is '{current_status}', not running — cannot pause.")
        raise typer.Exit(1)

    if force:
        _store.create_force_pause_request(run_dir)
    else:
        _store.create_pause_request(run_dir)
    run_hash: str = manifest.get("run_hash") or run_dir.name
    if force:
        _out.print(
            f"[yellow]⏸[/yellow] Force pause requested for run [bold]{run_hash[:8]}[/bold]. "
            "Running executions will be stopped and left pending."
        )
    else:
        _out.print(
            f"[yellow]⏸[/yellow] Pause requested for run [bold]{run_hash[:8]}[/bold]. "
            "Workers will stop after current executions finish."
        )


# ---------------------------------------------------------------------------
# ygor resume
# ---------------------------------------------------------------------------


def _check_for_crash(run_dir: Path) -> bool:
    """Return True if the lock file indicates a crashed process."""
    import os
    import socket

    lock_info = _store.read_lock_file(run_dir)
    if lock_info is None:
        return False
    pid, hostname = lock_info
    if hostname != socket.gethostname():
        return False
    try:
        os.kill(pid, 0)
        return False  # process is alive
    except ProcessLookupError:
        return True
    except PermissionError:
        return False  # process exists, we just can't signal it


@app.command(rich_help_panel=_HELP_PANEL_CONTROL)
def resume(
    hash_prefix: Annotated[str | None, typer.Argument(help="Run hash or unique prefix.")] = None,
    store: Annotated[Path, typer.Option("--store", help="Store root directory.")] = _DEFAULT_STORE,
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Skip confirmations.")] = False,
    retry_failed: Annotated[bool, typer.Option("--retry-failed", help="Re-run failed executions that exhausted retries.")] = False,
) -> None:
    """Resume a paused, failed, or crashed run."""
    store = store.resolve()

    if hash_prefix is None:
        runs = _collect_runs(store, limit=20)
        eligible = [r for r in runs if r["status"] in ("paused", "running", "failed")]
        if not eligible:
            _err.print("No resumable run found (status must be paused, running, or failed).")
            raise typer.Exit(1)
        hash_prefix = eligible[0]["run_hash"]
        _out.print(f"[dim]Resuming most recent eligible run: {hash_prefix[:8]}[/dim]")

    run_dir, manifest = _find_run(store, hash_prefix)
    state: dict[str, Any] = manifest.get("state") or {}
    current_status = state.get("status", "unknown")

    if current_status not in ("paused", "running", "failed"):
        _err.print(f"Run {hash_prefix[:8]} is '{current_status}' — only paused/running/failed runs can be resumed.")
        raise typer.Exit(1)

    recipe = _recipe_from_manifest(manifest)
    all_executions = _execution_plan_from_manifest(manifest, recipe)
    resolved = _resolved_from_manifest(manifest, recipe)
    dataset_paths: dict[str, str] = dict(resolved["datasets"])
    app_paths: dict[str, str] = dict(resolved["apps"])
    recipe_dir = Path(resolved["recipe_dir"]) if resolved.get("recipe_dir") else Path.cwd()
    container_config: ApptainerConfig | None = _apptainer_config_from_manifest(resolved.get("container"))
    bindings = _resolve_recipe_bindings(recipe)
    container_config = _apply_bindings_to_apptainer_config(recipe, container_config, bindings)

    # --- crash detection: mark orphaned running execs as failed ---
    crashed = _check_for_crash(run_dir)
    executions_data: list[dict[str, Any]] = manifest.get("executions") or []
    orphaned = 0
    for ex in executions_data:
        if ex.get("status") == "running":
            ex["status"] = "failed"
            ex["failure_reason"] = "orphaned"
            orphaned += 1

    if orphaned:
        counts = state.setdefault("counts", {})
        counts["running"] = 0
        counts["failed"] = counts.get("failed", 0) + orphaned

    # --- compute pending executions ---
    max_retries = recipe.policy.retries
    completed_ids: set[int] = set()
    retryable_ids: set[int] = set()
    exhausted_ids: set[int] = set()

    for ex in executions_data:
        eid = ex.get("exec_id")
        ex_status = ex.get("status")
        retry_count = ex.get("retry_count", 0)
        if ex_status == "completed":
            completed_ids.add(eid)
        elif ex_status == "failed":
            if retry_count < max_retries or retry_failed:
                retryable_ids.add(eid)
            else:
                exhausted_ids.add(eid)

    # Executions to run: pending ones + retryable failed ones (preserve original exec_ids)
    pending_executions: list[tuple[int, dict, int]] = []
    retryable_pairs: list[tuple[int, dict, int]] = []

    for exec_id, pt, s in all_executions:
        if exec_id not in completed_ids and exec_id not in exhausted_ids:
            if exec_id in retryable_ids:
                retryable_pairs.append((exec_id, pt, s))
            else:
                pending_executions.append((exec_id, pt, s))

    to_execute = retryable_pairs + pending_executions

    # --- resume plan ---
    n_skip = len(completed_ids)
    n_retry = len(retryable_pairs)
    n_exhaust = len(exhausted_ids)
    n_pending = len(pending_executions)
    n_total_to_run = len(to_execute)

    _out.print(f"\n[bold]Resume plan for run {hash_prefix[:8]}:[/bold]")
    _out.print(f"  [green]✓ Completed (skip):[/green]          {n_skip:>6,}")
    _out.print(f"  [cyan]↻ Failed, will retry:[/cyan]         {n_retry:>6,}")
    _out.print(f"  [dim]⚠ Failed, exhausted retries:[/dim]   {n_exhaust:>6,}")
    if orphaned:
        _out.print(f"  [red]💀 Orphaned (now failed):[/red]      {orphaned:>6,}")
    _out.print(f"  [dim]⏸ Pending:[/dim]                     {n_pending:>6,}")
    _out.print(f"\n  [bold]Total to execute: {n_total_to_run:,}[/bold]")
    _out.print()

    if n_total_to_run == 0:
        _out.print("[dim]Nothing to execute — run is already complete.[/dim]")
        raise typer.Exit(0)

    if not yes and not typer.confirm("Proceed?", default=True):
        _out.print("Aborted.")
        raise typer.Exit(0)

    if container_config is not None:
        _out.print(f"[cyan]Using locked Apptainer image…[/cyan]\n  [dim]image:[/dim] {container_config.image}")

    # --- update manifest for resume ---
    manifest["executions"] = executions_data
    counts = state.setdefault("counts", {})
    counts["completed"] = len(completed_ids)
    counts["failed"] = len(exhausted_ids)
    counts["running"] = 0
    counts["pending"] = n_total_to_run
    state["status"] = "running"
    state["ended_at"] = None
    _MANIFEST_REPOSITORY.write_manifest(manifest, run_dir)

    _store.write_lock_file(run_dir)
    _store.clear_pause_request(run_dir)

    run_hash_full: str = manifest.get("run_hash") or run_dir.name
    total_resume = len(to_execute)
    failed_count = 0

    from rich.progress import Progress, BarColumn, MofNCompleteColumn, TimeElapsedColumn

    with Progress(
        "[progress.description]{task.description}",
        BarColumn(),
        MofNCompleteColumn(),
        TimeElapsedColumn(),
        console=_out,
        transient=False,
    ) as progress:
        task = progress.add_task(f"[cyan]{run_hash_full[:8]}[/cyan] (resume)", total=total_resume)

        def _on_progress(record: _runner.ExecRecord) -> None:
            nonlocal failed_count
            if record.status == "failed":
                failed_count += 1
            progress.advance(task)

        workers = recipe.policy.workers or 1
        manifest = _runner.execute_run_parallel(
            recipe, to_execute, run_hash_full, run_dir, manifest,
            _MANIFEST_REPOSITORY.write_manifest, workers=workers,
            on_progress=_on_progress,
            dataset_paths=dataset_paths,
            app_paths=app_paths,
            recipe_dir=recipe_dir,
            container_config=container_config,
        )

    # --- post (only if fully complete) ---
    final_status = manifest["state"]["status"]
    if final_status not in ("paused", "failed") and recipe.post:
        _out.print("[cyan]Running post commands…[/cyan]")
        manifest = _runner.execute_post(
            recipe, run_hash_full, run_dir, manifest, _MANIFEST_REPOSITORY.write_manifest,
            dataset_paths=dataset_paths,
            app_paths=app_paths,
            recipe_dir=recipe_dir,
            on_status=lambda msg: _out.print(f"  [dim]{msg}[/dim]"),
            container_config=container_config,
        )

    # --- finalise ---
    all_failed_count = manifest["state"]["counts"]["failed"]
    final_status = finalize_manifest_status(manifest)
    _MANIFEST_REPOSITORY.write(manifest, run_dir)
    _store.remove_lock_file(run_dir)

    completed_count = manifest["state"]["counts"]["completed"]
    if final_status == "paused":
        pending_left = manifest["state"]["counts"]["pending"]
        _out.print(
            f"\n[yellow]⏸[/yellow] Run [dim]{run_hash_full[:8]}[/dim] paused  "
            f"[green]{completed_count}[/green] completed  "
            + (f"[red]{all_failed_count}[/red] failed  " if all_failed_count else "")
            + f"[dim]{pending_left}[/dim] pending"
        )
    else:
        _out.print(
            f"\n[bold green]✓[/bold green] Run [dim]{run_hash_full[:8]}[/dim] resumed and finished  "
            f"[green]{completed_count}[/green] completed  "
            + (f"[red]{all_failed_count}[/red] failed" if all_failed_count else "")
        )


@app.command(rich_help_panel=_HELP_PANEL_RUN)
def web(
    store: Annotated[Path, typer.Option("--store", help="Store root directory to explore.")] = _DEFAULT_STORE,
    host: Annotated[str, typer.Option("--host", help="Local IPv4 address to listen on.")] = "127.0.0.1",
    port: Annotated[int, typer.Option("--port", min=0, max=65535, help="HTTP port (0 chooses a free port).")] = 8765,
    open_browser: Annotated[bool, typer.Option("--open/--no-open", help="Open the explorer in your browser.")] = False,
) -> None:
    """Browse experiments in the local, read-only web explorer."""
    from ygor.web import create_server
    try:
        server = create_server(store, host, port)
    except (OSError, ValueError) as exc:
        _err.print(f"[red]Cannot start explorer:[/red] {exc}")
        raise typer.Exit(1)
    url = f"http://{host}:{server.server_address[1]}"
    _out.print(f"[bold]Ygor Explorer[/bold]  {url}")
    _out.print(f"Store: {store.resolve()}\nRead-only · local access only · Ctrl+C to stop")
    if open_browser:
        import webbrowser
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


def _registered_command_name(command_info: Any) -> str:
    if command_info.name:
        return str(command_info.name)
    callback = command_info.callback
    return callback.__name__.replace("_", "-") if callback else ""


def _order_registered_commands_for_help() -> None:
    def sort_key(command_info: Any) -> tuple[int, int, str]:
        panel = command_info.rich_help_panel
        panel_order = _HELP_PANEL_ORDER.get(panel, 99) if isinstance(panel, str) else 99
        name = _registered_command_name(command_info)
        command_order = _COMMAND_HELP_ORDER.get(name, 999)
        return (panel_order, command_order, name)

    app.registered_commands.sort(key=sort_key)


_order_registered_commands_for_help()
