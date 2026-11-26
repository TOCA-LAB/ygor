"""Apptainer environment support: image building and container execution."""

from __future__ import annotations

import hashlib
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
import re

from ygor.compat import cache_marker_exists


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------

class ApptainerNotAvailable(Exception):
    """Raised when Apptainer CLI is not accessible."""


class ApptainerBuildError(Exception):
    def __init__(self, message: str) -> None:
        self.message = message
        super().__init__(message)


# ---------------------------------------------------------------------------
# Availability checks
# ---------------------------------------------------------------------------

def check_apptainer() -> None:
    """Verify Apptainer CLI is accessible."""
    if not shutil.which("apptainer"):
        raise ApptainerNotAvailable("apptainer CLI not found in PATH")
    result = subprocess.run(
        ["apptainer", "--version"], capture_output=True, timeout=10
    )
    if result.returncode != 0:
        raise ApptainerNotAvailable("apptainer CLI is not accessible")


def apptainer_available() -> bool:
    """Return True if Apptainer is usable."""
    try:
        check_apptainer()
        return True
    except (ApptainerNotAvailable, FileNotFoundError, subprocess.TimeoutExpired):
        return False


def image_exists_locally(image: str) -> bool:
    """Return True if the cached SIF image exists locally."""
    return Path(image).exists()


# ---------------------------------------------------------------------------
# Environment image building (cached per environment fingerprint)
# ---------------------------------------------------------------------------

def _env_fingerprint(
    base_image: str,
    apt_packages: list[str],
    python_reqs_content: str,
    apt_snapshot: str | None = None,
    image_build_run: list[str] | None = None,
) -> str:
    payload = "\0".join([
        base_image,
        apt_snapshot or "",
        "\n".join(sorted(apt_packages)),
        python_reqs_content,
        "\n".join(image_build_run or []),
    ])
    return hashlib.sha256(payload.encode()).hexdigest()[:16]


def _apt_snapshot_script(snapshot_date: str) -> str:
    """Return a shell block that pins Ubuntu/Debian apt sources to a snapshot."""
    ts = f"{snapshot_date}T000000Z"
    ubuntu_snap = f"http://snapshot.ubuntu.com/ubuntu/{ts}/"
    debian_snap = f"http://snapshot.debian.org/archive/debian/{ts}/"
    debian_sec_snap = f"http://snapshot.debian.org/archive/debian-security/{ts}/"
    return "\n".join([
        'printf \'Acquire::Check-Valid-Until "false";\\n\' > /etc/apt/apt.conf.d/99snapshot',
        "for f in /etc/apt/sources.list /etc/apt/sources.list.d/*.list /etc/apt/sources.list.d/*.sources; do",
        '  [ -f "$f" ] || continue',
        f'  sed -i -e "s|http://archive.ubuntu.com/ubuntu|{ubuntu_snap}|g" \\',
        f'         -e "s|http://security.ubuntu.com/ubuntu|{ubuntu_snap}|g" \\',
        f'         -e "s|https://archive.ubuntu.com/ubuntu|{ubuntu_snap}|g" \\',
        f'         -e "s|https://security.ubuntu.com/ubuntu|{ubuntu_snap}|g" \\',
        f'         -e "s|http://deb.debian.org/debian-security|{debian_sec_snap}|g" \\',
        f'         -e "s|http://security.debian.org/debian-security|{debian_sec_snap}|g" \\',
        f'         -e "s|https://deb.debian.org/debian-security|{debian_sec_snap}|g" \\',
        f'         -e "s|https://security.debian.org/debian-security|{debian_sec_snap}|g" \\',
        f'         -e "s|http://deb.debian.org/debian|{debian_snap}|g" \\',
        f'         -e "s|https://deb.debian.org/debian|{debian_snap}|g" "$f"',
        "done",
        "for f in /etc/apt/sources.list.d/*.sources; do",
        '  [ -f "$f" ] || continue',
        '  sed -i -e "/^[[:space:]]*Snapshot:[[:space:]]*/d" "$f"',
        "done",
    ])


def _apt_common_options(timeout: int = 60, retries: int = 5) -> str:
    return " ".join([
        f"-o Acquire::Retries={retries}",
        f"-o Acquire::http::Timeout={timeout}",
        f"-o Acquire::https::Timeout={timeout}",
        "-o Acquire::ForceIPv4=true",
        "-o Acquire::http::No-Cache=true",
        "-o Acquire::https::No-Cache=true",
    ])


def _apt_update_script(attempts: int = 5, command_timeout: str = "300s") -> str:
    """Return an apt-get update block that retries and treats index failures as fatal."""
    options = _apt_common_options()
    return "\n".join([
        f"for i in $(seq 1 {attempts}); do",
        f"  timeout -k 15s {command_timeout} apt-get {options} -o APT::Update::Error-Mode=any update && break",
        f'  if [ "$i" = "{attempts}" ]; then',
        '    echo "apt-get update failed after all retry attempts" >&2',
        "    exit 100",
        "  fi",
        "  sleep $((i * 5))",
        "done",
    ])


def _apt_install_script(
    packages: list[str],
    attempts: int = 5,
    command_timeout: str = "1200s",
) -> str:
    """Return an apt-get install block with retries for snapshot package fetches."""
    pkgs = " ".join(packages)
    options = _apt_common_options()
    return "\n".join([
        f"for i in $(seq 1 {attempts}); do",
        f"  timeout -k 15s {command_timeout} apt-get {options} install -y --no-install-recommends {pkgs} && break",
        f'  if [ "$i" = "{attempts}" ]; then',
        '    echo "apt-get install failed after all retry attempts" >&2',
        "    exit 100",
        "  fi",
        "  sleep $((i * 10))",
        "done",
    ])


def _apptainer_from_image(base_image: str) -> str:
    if base_image.startswith("docker://"):
        return base_image.removeprefix("docker://")
    return base_image


def _build_definition(
    base_image: str,
    apt_packages: list[str],
    python_reqs_content: str,
    apt_snapshot: str | None = None,
    image_build_run: list[str] | None = None,
    build_secrets: list["ApptainerBuildSecret"] | None = None,
    secret_dir: Path | None = None,
    requirements_path: Path | None = None,
) -> str:
    """Build an Apptainer definition file for a cached experiment image."""
    files: list[str] = []
    post: list[str] = ["set -e"]

    if python_reqs_content.strip():
        source = requirements_path if requirements_path is not None else Path("requirements.lock")
        files.append(f"{source} /tmp/ygor_requirements.lock")

    if build_secrets and secret_dir is not None:
        for secret in build_secrets:
            files.append(f"{secret_dir / secret.id} /run/secrets/{secret.id}")

    if apt_packages:
        post.append("export DEBIAN_FRONTEND=noninteractive")
        if apt_snapshot:
            post.append(_apt_snapshot_script(apt_snapshot))
        post.extend([
            _apt_update_script(),
            _apt_install_script(apt_packages),
            "rm -rf /var/lib/apt/lists/*",
        ])

    if python_reqs_content.strip():
        post.append("pip install --quiet -r /tmp/ygor_requirements.lock")

    post.extend(image_build_run or [])

    if build_secrets:
        post.append("rm -rf /run/secrets")

    lines = [
        "Bootstrap: docker",
        f"From: {_apptainer_from_image(base_image)}",
        "",
    ]
    if files:
        lines.append("%files")
        lines.extend(f"    {entry}" for entry in files)
        lines.append("")
    if len(post) > 1:
        lines.append("%post")
        lines.extend(f"    {line}" if line else "" for line in "\n".join(post).splitlines())
        lines.append("")
    return "\n".join(lines)


@dataclass
class ApptainerBuildSecret:
    """A secret value made available only during image-build customization."""
    id: str
    value: str


def ensure_ygor_image(
    base_image: str,
    apt_packages: list[str],
    python_reqs_content: str,
    store_root: Path,
    *,
    apt_snapshot: str | None = None,
    image_build_run: list[str] | None = None,
    build_secrets: list[ApptainerBuildSecret] | None = None,
    hostname: str | None = None,
    mac_address: str | None = None,
    on_status: Any = None,
) -> str:
    """Build and cache an Apptainer SIF image derived from *base_image*."""
    del hostname, mac_address
    image_build_run = image_build_run or []
    build_secrets = build_secrets or []

    key = _env_fingerprint(
        base_image,
        apt_packages,
        python_reqs_content,
        apt_snapshot,
        image_build_run,
    )
    image_dir = store_root / "images" / key
    image_path = image_dir / "image.sif"
    sentinel = image_dir / ".ygor_image"

    if cache_marker_exists(image_dir, sentinel.name) and image_exists_locally(str(image_path)):
        return str(image_path)

    image_dir.mkdir(parents=True, exist_ok=True)

    if on_status:
        on_status(f"Building Apptainer image {image_path.name}...")

    try:
        with tempfile.TemporaryDirectory(prefix="ygor-apptainer-build-") as tmp:
            tmp_path = Path(tmp)
            if python_reqs_content.strip():
                requirements_path = tmp_path / "requirements.lock"
                requirements_path.write_text(
                    python_reqs_content, encoding="utf-8"
                )
            else:
                requirements_path = None
            secret_dir = tmp_path / "secrets"
            if build_secrets:
                secret_dir.mkdir()
                for secret in build_secrets:
                    (secret_dir / secret.id).write_text(secret.value, encoding="utf-8")

            definition = _build_definition(
                base_image,
                apt_packages,
                python_reqs_content,
                apt_snapshot,
                image_build_run,
                build_secrets,
                secret_dir if build_secrets else None,
                requirements_path,
            )
            definition_path = tmp_path / "ygor.def"
            definition_path.write_text(definition, encoding="utf-8")
            (image_dir / "ygor.def").write_text(definition, encoding="utf-8")

            _run_apptainer_build(image_path, definition_path, on_status=on_status)
    except subprocess.CalledProcessError as exc:
        raise ApptainerBuildError("apptainer image preparation failed - see output above") from exc

    sentinel.touch()
    return str(image_path)


def _run_apptainer_build(image_path: Path, definition_path: Path, *, on_status: Any = None) -> None:
    first_args = ["apptainer", "build", str(image_path), str(definition_path)]
    retry_args = [
        "apptainer",
        "build",
        "--ignore-fakeroot-command",
        str(image_path),
        str(definition_path),
    ]
    try:
        subprocess.run(first_args, check=True)
    except subprocess.CalledProcessError:
        if on_status:
            on_status("Apptainer build failed; retrying with --ignore-fakeroot-command...")
        try:
            image_path.unlink(missing_ok=True)
        except OSError:
            pass
        subprocess.run(retry_args, check=True)


# ---------------------------------------------------------------------------
# ApptainerConfig and command construction
# ---------------------------------------------------------------------------

@dataclass
class ApptainerMount:
    """A bind mount from a host path to a container path."""
    source: Path
    target: Path | None = None
    readonly: bool = False


@dataclass
class ApptainerConfig:
    """Parameters for running a command inside an Apptainer container."""
    image: str
    env_vars: dict[str, Any] = field(default_factory=dict)
    mounts: list[Path | ApptainerMount] = field(default_factory=list)
    hostname: str | None = None
    mac_address: str | None = None
    name: str | None = None
    remove: bool = True
    cpu_limit: int | None = None
    memory_limit: str | None = None
    workdir: Path | None = None


_MEMORY_RE = re.compile(r"^(?P<value>\d+(?:\.\d+)?)(?P<unit>B|KB|MB|GB|TB)$")
_MEMORY_UNITS = {
    "B": 1,
    "KB": 1024,
    "MB": 1024**2,
    "GB": 1024**3,
    "TB": 1024**4,
}


def memory_to_bytes(value: str) -> int:
    """Convert a recipe memory value like ``16GB`` to bytes for Apptainer."""
    match = _MEMORY_RE.match(value)
    if not match:
        raise ValueError(f"memory must be like '8GB', got {value!r}")
    return int(float(match.group("value")) * _MEMORY_UNITS[match.group("unit")])


def make_apptainer_exec_args(cmd: str, config: ApptainerConfig) -> list[str]:
    """Build the ``apptainer exec`` argument list for *cmd* under *config*."""
    args = ["apptainer", "exec", "--cleanenv"]

    if config.cpu_limit is not None:
        args += ["--cpus", str(config.cpu_limit)]

    if config.memory_limit:
        args += ["--memory", str(memory_to_bytes(config.memory_limit))]

    for k, v in config.env_vars.items():
        args += ["--env", f"{k}={v}"]

    for mount in config.mounts:
        if isinstance(mount, ApptainerMount):
            source = str(mount.source.resolve())
            target_path = mount.target if mount.target is not None else mount.source
            target = str(target_path)
            bind = f"{source}:{target}"
            if mount.readonly:
                bind += ":ro"
            args += ["--bind", bind]
        else:
            abs_path = str(mount.resolve())
            args += ["--bind", f"{abs_path}:{abs_path}"]

    if config.workdir is not None:
        args += ["--pwd", str(config.workdir.resolve())]

    args += [config.image, "sh", "-c", cmd]
    return args


# Backward-compatible aliases for modules that still import Docker-named symbols.
DockerNotAvailable = ApptainerNotAvailable
DockerBuildError = ApptainerBuildError
DockerBuildSecret = ApptainerBuildSecret
DockerMount = ApptainerMount
DockerConfig = ApptainerConfig
check_docker = check_apptainer
docker_available = apptainer_available
make_docker_run_args = make_apptainer_exec_args
_build_dockerfile = _build_definition
