"""Compatibility shim for the Apptainer container backend."""

from ygor.apptainer import (  # noqa: F401
    ApptainerBuildError,
    ApptainerBuildSecret,
    ApptainerConfig,
    ApptainerMount,
    ApptainerNotAvailable,
    DockerBuildError,
    DockerBuildSecret,
    DockerConfig,
    DockerMount,
    DockerNotAvailable,
    _apt_snapshot_script,
    _build_definition,
    _build_dockerfile,
    _env_fingerprint,
    apptainer_available,
    check_apptainer,
    check_docker,
    docker_available,
    ensure_ygor_image,
    image_exists_locally,
    make_apptainer_exec_args,
    make_docker_run_args,
)

_apt_snapshot_run = _apt_snapshot_script
YGOR_CONTAINER_HOSTNAME = None
