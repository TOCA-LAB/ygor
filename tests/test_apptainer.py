"""Tests for Apptainer environment support (src/ygor/apptainer.py)."""

from __future__ import annotations

import pytest
import typer
from typer.testing import CliRunner

import ygor.apptainer as apptainer_mod
from ygor.apptainer import (
    ApptainerConfig,
    ApptainerBuildError,
    ApptainerMount,
    _apt_install_script,
    _apt_snapshot_script,
    _apt_update_script,
    _build_definition,
    _env_fingerprint,
    apptainer_available,
    check_apptainer,
    ensure_ygor_image,
    image_exists_locally,
    make_apptainer_exec_args,
    memory_to_bytes,
)

APPTAINER_AVAILABLE = apptainer_available()
requires_apptainer = pytest.mark.skipif(
    not APPTAINER_AVAILABLE, reason="Apptainer not available on this machine"
)

cli = CliRunner()


class TestEnvFingerprint:
    def test_deterministic(self):
        k1 = _env_fingerprint("ubuntu:24.04", ["gcc"], "numpy==1.26\n")
        k2 = _env_fingerprint("ubuntu:24.04", ["gcc"], "numpy==1.26\n")
        assert k1 == k2

    def test_package_order_invariant(self):
        k1 = _env_fingerprint("ubuntu:24.04", ["cmake", "gcc"], "")
        k2 = _env_fingerprint("ubuntu:24.04", ["gcc", "cmake"], "")
        assert k1 == k2

    def test_fingerprint_differs_with_image_build_run(self):
        k1 = _env_fingerprint("ubuntu:24.04", [], "", image_build_run=[])
        k2 = _env_fingerprint("ubuntu:24.04", [], "", image_build_run=["echo custom"])
        assert k1 != k2


class TestBuildDefinition:
    def test_from_line(self):
        definition = _build_definition("ubuntu:24.04", [], "")
        assert "Bootstrap: docker" in definition
        assert "From: ubuntu:24.04" in definition

    def test_strips_docker_uri_prefix(self):
        definition = _build_definition("docker://alpine:latest", [], "")
        assert "From: alpine:latest" in definition

    def test_no_post_when_empty(self):
        definition = _build_definition("ubuntu:24.04", [], "")
        assert "%post" not in definition

    def test_apt_install_when_packages(self):
        definition = _build_definition("ubuntu:24.04", ["build-essential", "cmake"], "")
        assert "%post" in definition
        assert "install -y --no-install-recommends build-essential cmake" in definition

    def test_pip_install_when_reqs(self):
        definition = _build_definition("ubuntu:24.04", [], "numpy==1.26\n")
        assert "%files" in definition
        assert "requirements.lock /tmp/ygor_requirements.lock" in definition
        assert "pip install --quiet -r /tmp/ygor_requirements.lock" in definition

    def test_image_build_run_commands_in_post(self):
        definition = _build_definition(
            "ubuntu:24.04",
            [],
            "",
            image_build_run=["mkdir -p /opt/my-tools"],
        )
        assert "mkdir -p /opt/my-tools" in definition

    def test_snapshot_block_inserted_before_apt_get(self):
        definition = _build_definition("ubuntu:24.04", ["gcc"], "", apt_snapshot="20240101")
        snap_pos = definition.find("snapshot.debian.org/archive/debian/20240101T000000Z/")
        apt_pos = definition.find("apt-get -o Acquire::Retries=5")
        assert snap_pos != -1
        assert snap_pos < apt_pos

    def test_apt_update_retries_before_install(self):
        definition = _build_definition("ubuntu:24.04", ["gcc"], "")
        update_pos = definition.find("APT::Update::Error-Mode=any update")
        install_pos = definition.find("install -y --no-install-recommends")
        assert update_pos != -1
        assert update_pos < install_pos

    def test_apt_install_retries(self):
        definition = _build_definition("ubuntu:24.04", ["gcc"], "")
        assert "apt-get install failed after all retry attempts" in definition
        assert "timeout -k 15s 1200s apt-get" in definition


class TestAptSnapshotScript:
    def test_removes_deb822_snapshot_field(self):
        block = _apt_snapshot_script("20240101")
        assert "Snapshot: 20240101T000000Z" not in block
        assert "/^[[:space:]]*Snapshot:" in block

    def test_ubuntu_snapshot_url_for_sources_list(self):
        block = _apt_snapshot_script("20240101")
        assert "http://snapshot.ubuntu.com/ubuntu/20240101T000000Z/" in block

    def test_debian_snapshot_urls_use_http(self):
        block = _apt_snapshot_script("20240101")
        assert "http://snapshot.debian.org/archive/debian/20240101T000000Z/" in block
        assert "http://snapshot.debian.org/archive/debian-security/20240101T000000Z/" in block

    def test_debian_security_pattern_before_debian_main(self):
        block = _apt_snapshot_script("20240101")
        sec_pos = block.find("debian-security/20240101T000000Z")
        main_pos = block.rfind("deb.debian.org/debian|")
        assert sec_pos < main_pos


class TestAptUpdateScript:
    def test_treats_failed_indexes_as_fatal(self):
        block = _apt_update_script()
        assert "APT::Update::Error-Mode=any" in block
        assert "apt-get update failed after all retry attempts" in block

    def test_retries_multiple_times(self):
        block = _apt_update_script(attempts=4)
        assert "seq 1 4" in block
        assert 'if [ "$i" = "4" ]' in block

    def test_has_outer_timeout(self):
        block = _apt_update_script()
        assert "timeout -k 15s 300s apt-get" in block


class TestAptInstallScript:
    def test_retries_multiple_times(self):
        block = _apt_install_script(["gcc"], attempts=4)
        assert "seq 1 4" in block
        assert 'if [ "$i" = "4" ]' in block

    def test_uses_snapshot_friendly_options(self):
        block = _apt_install_script(["gcc"])
        assert "Acquire::Retries=5" in block
        assert "Acquire::http::Timeout=60" in block
        assert "Acquire::https::Timeout=60" in block
        assert "Acquire::ForceIPv4=true" in block
        assert "Acquire::http::No-Cache=true" in block
        assert "timeout -k 15s 1200s apt-get" in block
        assert "install -y --no-install-recommends gcc" in block


class TestMakeApptainerExecArgs:
    def _base_config(self, **kwargs) -> ApptainerConfig:
        return ApptainerConfig(image="image.sif", **kwargs)

    def test_starts_with_apptainer_exec_cleanenv(self):
        args = make_apptainer_exec_args("echo hi", self._base_config())
        assert args[:3] == ["apptainer", "exec", "--cleanenv"]

    def test_image_before_command(self):
        args = make_apptainer_exec_args("echo hi", self._base_config())
        img_idx = args.index("image.sif")
        assert args[img_idx + 1] == "sh"
        assert args[img_idx + 2] == "-c"
        assert args[img_idx + 3] == "echo hi"

    def test_env_vars_included(self):
        cfg = self._base_config(env_vars={"FOO": "bar", "X": 42})
        args = make_apptainer_exec_args("echo", cfg)
        env_pairs = [args[i + 1] for i, a in enumerate(args) if a == "--env"]
        assert "FOO=bar" in env_pairs
        assert "X=42" in env_pairs

    def test_path_bind_mount(self, tmp_path):
        cfg = self._base_config(mounts=[tmp_path])
        args = make_apptainer_exec_args("echo", cfg)
        bind_arg = args[args.index("--bind") + 1]
        abs_path = str(tmp_path.resolve())
        assert bind_arg == f"{abs_path}:{abs_path}"

    def test_binding_mount_readonly(self, tmp_path):
        source = tmp_path / "gurobi.lic"
        source.write_text("license", encoding="utf-8")
        cfg = self._base_config(
            mounts=[ApptainerMount(source=source, target=tmp_path / "container.lic", readonly=True)]
        )
        args = make_apptainer_exec_args("echo", cfg)
        bind_arg = args[args.index("--bind") + 1]
        assert bind_arg == f"{source.resolve()}:{tmp_path / 'container.lic'}:ro"

    def test_workdir(self, tmp_path):
        cfg = self._base_config(workdir=tmp_path)
        args = make_apptainer_exec_args("echo", cfg)
        assert "--pwd" in args
        assert args[args.index("--pwd") + 1] == str(tmp_path.resolve())

    def test_cpu_limit(self):
        cfg = self._base_config(cpu_limit=8)
        args = make_apptainer_exec_args("echo", cfg)
        assert "--cpus" in args
        assert args[args.index("--cpus") + 1] == "8"

    def test_memory_limit(self):
        cfg = self._base_config(memory_limit="16GB")
        args = make_apptainer_exec_args("echo", cfg)
        assert "--memory" in args
        assert args[args.index("--memory") + 1] == str(16 * 1024**3)

    def test_memory_to_bytes(self):
        assert memory_to_bytes("512MB") == 512 * 1024**2
        assert memory_to_bytes("1.5GB") == int(1.5 * 1024**3)

    def test_does_not_use_docker_network_identity_flags(self):
        cfg = self._base_config(
            hostname="ignored",
            mac_address="02:42:ac:11:00:02",
        )
        args = make_apptainer_exec_args("echo", cfg)
        assert "--hostname" not in args
        assert "--mac-address" not in args


class TestConfigPropagation:
    def test_run_shell_uses_apptainer_args(self, tmp_path, monkeypatch):
        from ygor.runner import _run_shell

        captured: dict[str, ApptainerConfig] = {}

        def fake_make_apptainer_exec_args(cmd, config):
            captured["config"] = config
            return ["true"]

        monkeypatch.setattr(
            apptainer_mod,
            "make_apptainer_exec_args",
            fake_make_apptainer_exec_args,
        )

        code, reason = _run_shell(
            "echo ok",
            tmp_path,
            tmp_path / "run.log",
            timeout=10,
            attempt=0,
            docker_config=ApptainerConfig(image="image.sif"),
        )

        assert code == 0, reason
        assert captured["config"].image == "image.sif"

    def test_prepare_environment_rejects_missing_requirements(self, tmp_path, monkeypatch):
        import ygor.cli as cli_module
        from ygor.bindings import ResolvedBindings
        from ygor.schema import Recipe

        monkeypatch.setattr(cli_module, "check_apptainer", lambda: None)
        recipe_path = tmp_path / "recipe.yaml"
        recipe_path.write_text("command: echo\n", encoding="utf-8")
        recipe = Recipe.model_validate({
            "environment": {
                "base-image": "ubuntu:24.04",
                "apps": {"python": {"requirements": "missing.txt"}},
            },
            "command": "echo",
        })

        with pytest.raises(typer.Exit) as exc:
            cli_module._prepare_apptainer_environment(
                recipe,
                recipe_path,
                tmp_path / "store",
                ResolvedBindings(),
            )
        assert exc.value.exit_code == 1

    def test_app_build_uses_apptainer_args(self, tmp_path, monkeypatch):
        from ygor.sources import _run_build_in_apptainer

        captured: dict[str, ApptainerConfig] = {}

        def fake_make_apptainer_exec_args(cmd, config):
            captured["config"] = config
            return ["true"]

        monkeypatch.setattr(
            apptainer_mod,
            "make_apptainer_exec_args",
            fake_make_apptainer_exec_args,
        )

        _run_build_in_apptainer(
            "echo build",
            tmp_path,
            ApptainerConfig(image="image.sif", cpu_limit=2, memory_limit="4GB"),
        )

        assert captured["config"].image == "image.sif"
        assert captured["config"].cpu_limit == 2
        assert captured["config"].memory_limit == "4GB"


class TestEnsureYgorImage:
    def test_creates_image_tag_with_packages(self):
        key = _env_fingerprint("ubuntu:24.04", ["gcc"], "")
        expected_path = f"images/{key}/image.sif"
        assert expected_path.endswith("image.sif")

    def test_builds_sif_with_definition(self, tmp_path, monkeypatch):
        monkeypatch.setattr(apptainer_mod, "image_exists_locally", lambda _image: False)
        calls: list[list[str]] = []

        def fake_run(args, **_kwargs):
            calls.append(list(args))

        monkeypatch.setattr(apptainer_mod.subprocess, "run", fake_run)

        image_build_run = ["touch /opt/custom"]
        image = ensure_ygor_image(
            "ubuntu:24.04",
            ["curl"],
            "",
            tmp_path / "store",
            image_build_run=image_build_run,
        )

        key = _env_fingerprint(
            "ubuntu:24.04",
            ["curl"],
            "",
            image_build_run=image_build_run,
        )
        expected_image = tmp_path / "store" / "images" / key / "image.sif"
        build_call = next(call for call in calls if call[:2] == ["apptainer", "build"])

        assert image == str(expected_image)
        assert build_call[2] == str(expected_image)
        assert (tmp_path / "store" / "images" / key / "ygor.def").exists()

    def test_retries_build_ignoring_fakeroot_command(self, tmp_path, monkeypatch):
        monkeypatch.setattr(apptainer_mod, "image_exists_locally", lambda _image: False)
        calls: list[list[str]] = []
        statuses: list[str] = []

        def fake_run(args, **_kwargs):
            calls.append(list(args))
            if len(calls) == 1:
                raise apptainer_mod.subprocess.CalledProcessError(1, args)

        monkeypatch.setattr(apptainer_mod.subprocess, "run", fake_run)

        image = ensure_ygor_image(
            "ubuntu:24.04",
            [],
            "",
            tmp_path / "store",
            on_status=statuses.append,
        )

        assert image.endswith("image.sif")
        assert calls[0][:2] == ["apptainer", "build"]
        assert calls[1][:3] == ["apptainer", "build", "--ignore-fakeroot-command"]
        assert any("--ignore-fakeroot-command" in msg for msg in statuses)


class TestCheckApptainer:
    @requires_apptainer
    def test_check_apptainer_passes(self):
        check_apptainer()

    def test_apptainer_available_returns_bool(self):
        result = apptainer_available()
        assert isinstance(result, bool)


@requires_apptainer
class TestApptainerIntegration:
    """Integration tests that actually run Apptainer containers."""

    def test_run_shell_with_apptainer(self, tmp_path):
        from ygor.runner import _run_shell

        try:
            image = ensure_ygor_image("alpine:latest", [], "", tmp_path / "store")
        except ApptainerBuildError as exc:
            pytest.skip(f"Apptainer build is not usable on this machine: {exc}")
        log = tmp_path / "test.log"
        log.touch()
        cfg = ApptainerConfig(image=image, mounts=[tmp_path])
        out_file = tmp_path / "out.txt"

        code, reason = _run_shell(
            f"echo hello_apptainer > {out_file.resolve()}",
            tmp_path,
            log,
            timeout=30,
            attempt=0,
            docker_config=cfg,
        )
        assert code == 0, reason
        assert out_file.read_text().strip() == "hello_apptainer"
