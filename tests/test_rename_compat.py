"""Existing configuration and expensive caches survive the package rename."""

from pathlib import Path

import pytest

from ygor import apptainer, datasets, git_cache
from ygor.cli import _read_project_config
from ygor.schema import Dataset, GitSource, HttpSource


def test_reads_existing_project_configuration(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".labrc.yaml").write_text(
        "sync:\n  remote: existing-store\nbindings:\n  data:\n    source: /existing/data\n"
    )

    config = _read_project_config()

    assert config["sync"]["remote"] == "existing-store"
    assert config["bindings"]["data"]["source"] == "/existing/data"
    assert not (tmp_path / ".ygorrc.yaml").exists()


def test_new_configuration_overrides_matching_settings(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".labrc.yaml").write_text(
        "sync:\n  remote: old-store\n  exclude: [logs]\n"
        "bindings:\n  data:\n    source: /existing/data\n"
    )
    (tmp_path / ".ygorrc.yaml").write_text(
        "sync:\n  remote: new-store\n"
        "bindings:\n  cache:\n    source: /new/cache\n"
    )

    config = _read_project_config()

    assert config["sync"] == {"remote": "new-store", "exclude": ["logs"]}
    assert set(config["bindings"]) == {"data", "cache"}


def test_reuses_existing_git_checkout_without_cloning(tmp_path, monkeypatch):
    target = tmp_path / "checkout"
    target.mkdir()
    (target / ".lab_cloned").touch()
    (target / "result.txt").write_text("existing")
    monkeypatch.setattr(
        git_cache.subprocess, "run", lambda *a, **kw: pytest.fail("unexpected clone")
    )

    result = git_cache.materialize_git_checkout(
        url="file:///unused", ref="main", target=target
    )

    assert result == target
    assert (target / "result.txt").read_text() == "existing"
    assert not (target / ".ygor_cloned").exists()


def test_discovers_and_reuses_existing_dataset_caches(tmp_path, monkeypatch):
    download = HttpSource(type="http", url="https://example.invalid/data.txt")
    checkout = GitSource(type="git", url="file:///unused", ref="main")
    download_path = datasets._http_target(download, None, tmp_path)
    checkout_path = datasets._git_target(checkout, tmp_path)
    download_path.mkdir(parents=True)
    checkout_path.mkdir(parents=True)
    (download_path / ".lab_downloaded").touch()
    (checkout_path / ".lab_cloned").touch()
    inputs = {"download": Dataset(source=download), "checkout": Dataset(source=checkout)}
    monkeypatch.setattr(
        datasets.urllib.request, "urlretrieve",
        lambda *a, **kw: pytest.fail("unexpected download"),
    )
    monkeypatch.setattr(
        git_cache.subprocess, "run", lambda *a, **kw: pytest.fail("unexpected clone")
    )
    expected = {"download": download_path, "checkout": checkout_path}

    assert datasets.find_cached_datasets(inputs, tmp_path) == expected
    assert datasets.ensure_datasets(inputs, tmp_path) == expected


def test_reuses_existing_apptainer_image_without_building(tmp_path, monkeypatch):
    key = apptainer._env_fingerprint("ubuntu:24.04", ["gcc"], "")
    image_dir = tmp_path / "images" / key
    image_dir.mkdir(parents=True)
    image = image_dir / "image.sif"
    image.write_bytes(b"existing image")
    (image_dir / ".lab_image").touch()
    monkeypatch.setattr(
        apptainer, "image_exists_locally", lambda p: Path(p).is_file()
    )
    monkeypatch.setattr(
        apptainer, "_run_apptainer_build",
        lambda *a, **kw: pytest.fail("unexpected image rebuild"),
    )

    assert apptainer.ensure_ygor_image("ubuntu:24.04", ["gcc"], "", tmp_path) == str(image)
    assert image.read_bytes() == b"existing image"
    assert not (image_dir / ".ygor_image").exists()
