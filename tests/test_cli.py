"""Tests for `ygor validate` and `ygor run --dry-run` CLI commands."""

import json
import re
from pathlib import Path

from typer.testing import CliRunner

from ygor.cli import app

FIXTURES = Path(__file__).parent / "fixtures"
runner = CliRunner()
ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")


def _plain(text: str) -> str:
    return ANSI_RE.sub("", text)


# ---------------------------------------------------------------------------
# ygor doctor / command surface
# ---------------------------------------------------------------------------

class TestCommandSurface:
    def test_help_shows_run_not_start(self):
        result = runner.invoke(app, ["--help"])
        assert result.exit_code == 0
        assert "run" in result.output
        assert "start" not in result.output

    def test_help_uses_english_command_descriptions(self):
        result = runner.invoke(app, ["--help"])
        assert result.exit_code == 0
        assert "List runs in the store, newest first" in result.output
        assert "Lista runs" not in result.output

    def test_help_groups_commands_by_workflow(self):
        result = runner.invoke(app, ["--help"])
        assert result.exit_code == 0
        sections = ["Recipe", "Run", "Control", "Sharing", "System"]
        positions = [result.output.index(section) for section in sections]
        assert positions == sorted(positions)

    def test_latest_argument_uses_generic_recipe_name(self):
        result = runner.invoke(app, ["latest", "--help"])
        assert result.exit_code == 0
        assert "RECIPE" in result.output
        assert "RECIPE_NAME" not in result.output
        assert "Recipe name, path, file name, or stem." in result.output

    def test_run_selectors_are_documented_consistently(self):
        for command in ("show", "status", "open"):
            result = runner.invoke(app, [command, "--help"])
            assert result.exit_code == 0
            output = _plain(result.output)
            assert "[RUN]" in output
            assert "--recipe" in output
            assert "--status" in output
            assert "--latest" in output

    def test_list_recipe_filter_documents_recipe_aliases(self):
        result = runner.invoke(app, ["list", "--help"])
        assert result.exit_code == 0
        assert "Filter by recipe name, path, file name, or stem." in result.output

    def test_doctor_json(self, tmp_path):
        result = runner.invoke(app, ["doctor", "--store", str(tmp_path / "store"), "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["ok"] is True
        assert any(c["name"] == "ygor module" for c in data["checks"])
        assert any(c["name"] == "store writable" for c in data["checks"])

    def test_start_alias_still_runs(self, tmp_path):
        recipe = tmp_path / "recipe.yaml"
        recipe.write_text('command: "echo alias"\n', encoding="utf-8")
        result = runner.invoke(app, ["start", str(recipe), "--store", str(tmp_path / "store"), "--yes"])
        assert result.exit_code == 0


# ---------------------------------------------------------------------------
# ygor validate
# ---------------------------------------------------------------------------

class TestValidateCommand:
    def test_valid_minimal(self):
        result = runner.invoke(app, ["validate", str(FIXTURES / "minimal.yaml")])
        assert result.exit_code == 0
        assert "✓" in result.output

    def test_valid_full(self):
        result = runner.invoke(app, ["validate", str(FIXTURES / "full.yaml")])
        assert result.exit_code == 0
        assert "ils-popsize-sweep" in result.output

    def test_invalid_no_command(self):
        result = runner.invoke(app, ["validate", str(FIXTURES / "invalid_no_command.yaml")])
        assert result.exit_code == 1

    def test_invalid_extra_field(self):
        result = runner.invoke(app, ["validate", str(FIXTURES / "invalid_extra_field.yaml")])
        assert result.exit_code == 1

    def test_file_not_found(self):
        result = runner.invoke(app, ["validate", "nonexistent.yaml"])
        assert result.exit_code == 1

    def test_json_output_valid(self):
        result = runner.invoke(app, ["validate", "--json", str(FIXTURES / "minimal.yaml")])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["ok"] is True

    def test_json_output_invalid(self):
        result = runner.invoke(app, ["validate", "--json", str(FIXTURES / "invalid_no_command.yaml")])
        assert result.exit_code == 1

    def test_invalid_template_syntax(self):
        result = runner.invoke(app, ["validate", str(FIXTURES / "invalid_template.yaml")])
        assert result.exit_code == 1
        assert "template syntax error" in result.output or "✗" in result.output

    def test_invalid_template_json(self):
        result = runner.invoke(app, ["validate", "--json", str(FIXTURES / "invalid_template.yaml")])
        assert result.exit_code == 1
        data = json.loads(result.stderr if hasattr(result, "stderr") else result.output)
        assert data["ok"] is False


# ---------------------------------------------------------------------------
# ygor run --dry-run
# ---------------------------------------------------------------------------

class TestDryRunCommand:
    def test_top_level_dry_run_command_removed(self):
        result = runner.invoke(app, ["dry-run", str(FIXTURES / "minimal.yaml")])
        assert result.exit_code != 0

    def test_minimal_exits_ok(self):
        result = runner.invoke(app, ["run", "--dry-run", str(FIXTURES / "minimal.yaml")])
        assert result.exit_code == 0

    def test_minimal_shows_one_execution(self):
        result = runner.invoke(app, ["run", "--dry-run", str(FIXTURES / "minimal.yaml")])
        assert "1" in result.output  # total_executions == 1

    def test_full_recipe_json(self):
        result = runner.invoke(app, ["run", "--dry-run", "--json", str(FIXTURES / "full.yaml")])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["ok"] is True
        assert data["n_param_tuples"] == 90
        assert data["n_seeds"] == 30
        assert data["total_executions"] == 2700

    def test_no_params_json(self):
        result = runner.invoke(app, ["run", "--dry-run", "--json", str(FIXTURES / "no_params.yaml")])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["total_executions"] == 1
        assert data["n_seeds"] == 0

    def test_zip_json(self):
        result = runner.invoke(app, ["run", "--dry-run", "--json", str(FIXTURES / "zip_params.yaml")])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["n_param_tuples"] == 3
        assert data["total_executions"] == 3

    def test_sample_json(self):
        result = runner.invoke(app, ["run", "--dry-run", "--json", str(FIXTURES / "sample_params.yaml")])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["n_param_tuples"] == 4
        assert data["total_executions"] == 12  # 4 * 3 seeds

    def test_file_not_found(self):
        result = runner.invoke(app, ["run", "--dry-run", "nonexistent.yaml"])
        assert result.exit_code == 1

    def test_invalid_template_fails(self):
        result = runner.invoke(app, ["run", "--dry-run", str(FIXTURES / "invalid_template.yaml")])
        assert result.exit_code == 1

    def test_human_output_shows_commands(self):
        result = runner.invoke(app, ["run", "--dry-run", str(FIXTURES / "full.yaml")])
        assert result.exit_code == 0
        assert "Command" in result.output
        assert "ils-odd-coloring" in result.output

    def test_human_output_shows_seeds(self):
        result = runner.invoke(app, ["run", "--dry-run", str(FIXTURES / "full.yaml")])
        assert "Seeds" in result.output or "seed" in result.output.lower()

    def test_json_seeds_preview(self):
        result = runner.invoke(app, ["run", "--dry-run", "--json", str(FIXTURES / "full.yaml")])
        data = json.loads(result.output)
        assert len(data["seeds_preview"]) == 5
        assert all(0 <= s < 2**32 for s in data["seeds_preview"])

    def test_prepare_requires_dry_run(self):
        result = runner.invoke(app, ["run", "--prepare", str(FIXTURES / "minimal.yaml")])
        assert result.exit_code == 1
        assert "--prepare can only be used with --dry-run" in result.output

    def test_prepare_with_dry_run_exits_ok_without_creating_run(self, tmp_path):
        store = tmp_path / "store"
        result = runner.invoke(
            app,
            ["run", "--dry-run", "--prepare", str(FIXTURES / "minimal.yaml"), "--store", str(store)],
        )
        assert result.exit_code == 0
        assert not (store / "runs").exists()


class TestDryRunCommands:
    """Tests for `ygor run --dry-run --commands`."""

    def _recipe_file(self, tmp_path, content: str):
        f = tmp_path / "recipe.yaml"
        f.write_text(content)
        return f

    def test_commands_substitutes_params(self, tmp_path):
        recipe = self._recipe_file(tmp_path, (
            'command: "./solver --x {{ param.x }}"\n'
            "parameters:\n"
            "  values:\n"
            "    x: [10, 20]\n"
        ))
        result = runner.invoke(app, ["run", "--dry-run", "--commands", str(recipe)])
        assert result.exit_code == 0
        assert "--x 10" in result.output
        assert "--x 20" in result.output

    def test_commands_substitutes_seed(self, tmp_path):
        recipe = self._recipe_file(tmp_path, (
            'command: "./solver --seed {{ run.seed }}"\n'
            "parameters:\n"
            "  values:\n"
            "    x: [1]\n"
            "  seeds:\n"
            "    count: 2\n"
            "    meta-seed: 0\n"
        ))
        result = runner.invoke(app, ["run", "--dry-run", "--commands", str(recipe)])
        assert result.exit_code == 0
        lines = [l for l in result.output.splitlines() if "./solver --seed " in l and "{{" not in l]
        assert len(lines) == 2

    def test_commands_shows_run_output_dir(self, tmp_path):
        recipe = self._recipe_file(tmp_path,
            'command: "./parse.py --dir {{ run.output_dir }}"\n'
        )
        result = runner.invoke(app, ["run", "--dry-run", "--commands", str(recipe)])
        assert result.exit_code == 0
        assert "./parse.py --dir" in result.output
        assert "outputs/exec_" in result.output

    def test_commands_substitutes_recipe_dir(self, tmp_path):
        recipe = self._recipe_file(tmp_path,
            'command: "python {{ recipe.dir }}/scripts/analyze.py"\n'
        )
        result = runner.invoke(app, ["run", "--dry-run", "--commands", str(recipe)])
        assert result.exit_code == 0
        assert f"python {tmp_path.resolve()}/scripts/analyze.py" in result.output

    def test_commands_app_placeholder(self, tmp_path):
        recipe = self._recipe_file(tmp_path,
            "command: \"{{ app['my-solver'] }} --x {{ param.x }}\"\n"
            "parameters:\n"
            "  values:\n"
            "    x: [5]\n"
        )
        result = runner.invoke(app, ["run", "--dry-run", "--commands", str(recipe)])
        assert result.exit_code == 0
        assert "my-solver" in result.output   # key shown in placeholder
        assert "--x 5" in result.output        # param still substituted

    def test_commands_apps_alias_placeholder(self, tmp_path):
        recipe = self._recipe_file(tmp_path,
            'command: "{{ apps.mean_time }} --input {{ param.instance }}"\n'
            "parameters:\n"
            "  instance:\n"
            "    - dataset:brkga-runs/executions.csv\n"
            "datasets:\n"
            "  brkga-runs:\n"
            "    source: { type: store, hash: 13c32451 }\n"
        )
        run_dir = tmp_path / "store" / "runs" / ("13c32451" + "0" * 56)
        run_dir.mkdir(parents=True)
        (run_dir / "executions.csv").write_text("status,duration_sec,param.instance\n", encoding="utf-8")

        result = runner.invoke(
            app,
            ["run", "--dry-run", "--commands", str(recipe), "--store", str(tmp_path / "store")],
        )

        assert result.exit_code == 0
        assert "<apps['mean_time']>" in result.output
        assert str(run_dir / "executions.csv") in result.output

    def test_commands_all_executions_shown(self, tmp_path):
        recipe = self._recipe_file(tmp_path, (
            'command: "echo {{ param.n }}"\n'
            "parameters:\n"
            "  values:\n"
            "    n: [1, 2, 3]\n"
        ))
        result = runner.invoke(app, ["run", "--dry-run", "--commands", str(recipe)])
        assert result.exit_code == 0
        assert "echo 1" in result.output
        assert "echo 2" in result.output
        assert "echo 3" in result.output

    def test_commands_single_execution_no_params(self, tmp_path):
        recipe = self._recipe_file(tmp_path, 'command: "echo hello"\n')
        result = runner.invoke(app, ["run", "--dry-run", "--commands", str(recipe)])
        assert result.exit_code == 0
        assert "echo hello" in result.output

    def test_commands_not_shown_without_flag(self, tmp_path):
        recipe = self._recipe_file(tmp_path, (
            'command: "echo {{ param.x }}"\n'
            "parameters:\n"
            "  values:\n"
            "    x: [42]\n"
        ))
        result = runner.invoke(app, ["run", "--dry-run", str(recipe)])
        assert result.exit_code == 0
        assert "echo 42" not in result.output

    def test_commands_ignored_with_json_flag(self, tmp_path):
        recipe = self._recipe_file(tmp_path,
            'command: "echo {{ param.x }}"\n'
            "parameters:\n  values:\n    x: [1]\n"
        )
        result = runner.invoke(app, ["run", "--dry-run", "--json", "--commands", str(recipe)])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["ok"] is True   # valid JSON, no expanded section mixed in
