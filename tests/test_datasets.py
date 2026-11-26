"""Tests for dataset resolution (src/ygor/datasets.py)."""

from __future__ import annotations

import subprocess
import tarfile
import zipfile
from pathlib import Path
from unittest.mock import patch

import pytest

from ygor.datasets import (
    DatasetResolutionError,
    _git_target,
    _http_target,
    _resolve_local,
    _resolve_store,
    ensure_datasets,
    find_cached_datasets,
    resolve_instance_globs,
)
from ygor.schema import Dataset, GitSource, HttpSource, LocalSource, StoreSource


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _http_dataset(url: str, sha256: str | None = None, extract: str | None = None) -> Dataset:
    return Dataset.model_validate(
        {"source": {"type": "http", "url": url}, "sha256": sha256, "extract": extract}
    )


def _git_dataset(url: str, ref: str) -> Dataset:
    return Dataset.model_validate(
        {"source": {"type": "git", "url": url, "ref": ref}}
    )


def _local_dataset(path: str) -> Dataset:
    return Dataset.model_validate({"source": {"type": "local", "path": path}})


def _store_dataset(hash_val: str) -> Dataset:
    return Dataset.model_validate({"source": {"type": "store"}, "hash": hash_val})


def _store_source_dataset(hash_val: str) -> Dataset:
    return Dataset.model_validate({"source": {"type": "store", "hash": hash_val}})


# ---------------------------------------------------------------------------
# Local
# ---------------------------------------------------------------------------

class TestResolveLocal:
    def test_existing_file(self, tmp_path):
        f = tmp_path / "data.csv"
        f.write_text("a,b")
        d = _local_dataset(str(f))
        result = _resolve_local("d", d.source)  # type: ignore[arg-type]
        assert result == f.resolve()

    def test_existing_directory(self, tmp_path):
        d_path = tmp_path / "instances"
        d_path.mkdir()
        d = _local_dataset(str(d_path))
        result = _resolve_local("d", d.source)  # type: ignore[arg-type]
        assert result.is_dir()

    def test_missing_path_raises(self, tmp_path):
        d = _local_dataset(str(tmp_path / "nonexistent"))
        with pytest.raises(DatasetResolutionError, match="local path not found"):
            _resolve_local("d", d.source)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------

class TestResolveStore:
    def test_full_hash_found(self, tmp_path):
        run_hash = "a" * 64
        run_dir = tmp_path / "runs" / run_hash
        run_dir.mkdir(parents=True)
        d = _store_dataset(run_hash)
        result = _resolve_store("d", d, tmp_path)
        assert result == run_dir

    def test_prefix_match(self, tmp_path):
        run_hash = "abcdef" + "0" * 58
        run_dir = tmp_path / "runs" / run_hash
        run_dir.mkdir(parents=True)
        d = _store_dataset("abcdef")
        result = _resolve_store("d", d, tmp_path)
        assert result == run_dir

    def test_hash_inside_source(self, tmp_path):
        run_hash = "13c32451" + "0" * 56
        run_dir = tmp_path / "runs" / run_hash
        run_dir.mkdir(parents=True)
        d = _store_source_dataset("13c32451")
        result = _resolve_store("d", d, tmp_path)
        assert result == run_dir

    def test_not_found_raises_with_pull_hint(self, tmp_path):
        (tmp_path / "runs").mkdir()
        d = _store_dataset("deadbeef")
        with pytest.raises(DatasetResolutionError, match="ygor pull deadbeef"):
            _resolve_store("d", d, tmp_path)

    def test_no_runs_dir_raises(self, tmp_path):
        d = _store_dataset("deadbeef")
        with pytest.raises(DatasetResolutionError, match="ygor pull deadbeef"):
            _resolve_store("d", d, tmp_path)

    def test_ambiguous_prefix_raises(self, tmp_path):
        (tmp_path / "runs").mkdir()
        for suffix in ("aabb" + "0" * 60, "aabb" + "1" * 60):
            (tmp_path / "runs" / suffix).mkdir()
        d = _store_dataset("aabb")
        with pytest.raises(DatasetResolutionError, match="ambiguous"):
            _resolve_store("d", d, tmp_path)

    def test_missing_hash_field_raises(self, tmp_path):
        d = Dataset.model_validate({"source": {"type": "store"}})
        with pytest.raises(DatasetResolutionError, match="hash"):
            _resolve_store("d", d, tmp_path)

    def test_find_cached_datasets_includes_store_source(self, tmp_path):
        run_hash = "13c32451" + "0" * 56
        run_dir = tmp_path / "runs" / run_hash
        run_dir.mkdir(parents=True)
        d = _store_source_dataset("13c32451")
        result = find_cached_datasets({"runs": d}, tmp_path)
        assert result == {"runs": run_dir}


# ---------------------------------------------------------------------------
# HTTP (mocked)
# ---------------------------------------------------------------------------

class TestResolveHttp:
    def _fake_download(self, content: bytes):
        """Return a side_effect for urlretrieve that writes content to dest."""
        def _side_effect(url, dest):
            Path(dest).write_bytes(content)
        return _side_effect

    def test_download_no_extract(self, tmp_path):
        content = b"hello data"
        with patch("urllib.request.urlretrieve", side_effect=self._fake_download(content)):
            d = _http_dataset("https://example.com/data.bin")
            result = ensure_datasets({"d": d}, tmp_path)
        assert (result["d"] / "data.bin").read_bytes() == content

    def test_skips_if_sentinel_exists(self, tmp_path):
        target = _http_target(
            HttpSource(type="http", url="https://example.com/x.bin"), None, tmp_path
        )
        target.mkdir(parents=True)
        (target / ".ygor_downloaded").touch()

        called = []
        with patch("urllib.request.urlretrieve", side_effect=lambda *a: called.append(a)):
            d = _http_dataset("https://example.com/x.bin")
            ensure_datasets({"d": d}, tmp_path)

        assert called == [], "urlretrieve should not be called when sentinel exists"

    def test_sha256_mismatch_raises(self, tmp_path):
        with patch("urllib.request.urlretrieve", side_effect=self._fake_download(b"bad content")):
            d = _http_dataset("https://example.com/f.tar.gz", sha256="a" * 64)
            with pytest.raises(DatasetResolutionError, match="sha256 mismatch"):
                ensure_datasets({"d": d}, tmp_path)

    def test_download_with_tar_gz(self, tmp_path):
        # Create a real tar.gz in memory
        archive_path = tmp_path / "archive.tar.gz"
        with tarfile.open(archive_path, "w:gz") as tf:
            content_path = tmp_path / "hello.mtx"
            content_path.write_text("hello")
            tf.add(content_path, arcname="hello.mtx")

        archive_bytes = archive_path.read_bytes()

        with patch("urllib.request.urlretrieve", side_effect=self._fake_download(archive_bytes)):
            d = _http_dataset("https://example.com/data.tar.gz", extract="tar.gz")
            result = ensure_datasets({"d": d}, tmp_path)

        assert (result["d"] / "hello.mtx").read_text() == "hello"
        assert (result["d"] / ".ygor_downloaded").exists()

    def test_rejects_tar_path_traversal(self, tmp_path):
        archive_path = tmp_path / "archive.tar.gz"
        source_dir = tmp_path / "source"
        source_dir.mkdir()
        with tarfile.open(archive_path, "w:gz") as tf:
            content_path = source_dir / "evil.txt"
            content_path.write_text("evil")
            tf.add(content_path, arcname="../evil.txt")

        with patch("urllib.request.urlretrieve", side_effect=self._fake_download(archive_path.read_bytes())):
            d = _http_dataset("https://example.com/data.tar.gz", extract="tar.gz")
            with pytest.raises(DatasetResolutionError, match="unsafe archive"):
                ensure_datasets({"d": d}, tmp_path)

        assert not (tmp_path / "datasets" / "evil.txt").exists()

    def test_rejects_zip_path_traversal(self, tmp_path):
        archive_path = tmp_path / "archive.zip"
        with zipfile.ZipFile(archive_path, "w") as zf:
            zf.writestr("../evil.txt", "evil")

        with patch("urllib.request.urlretrieve", side_effect=self._fake_download(archive_path.read_bytes())):
            d = _http_dataset("https://example.com/data.zip", extract="zip")
            with pytest.raises(DatasetResolutionError, match="unsafe archive"):
                ensure_datasets({"d": d}, tmp_path)

        assert not (tmp_path / "datasets" / "evil.txt").exists()

    def test_download_with_bz2(self, tmp_path):
        import bz2
        original = b"raw file content"
        compressed = bz2.compress(original)

        with patch("urllib.request.urlretrieve", side_effect=self._fake_download(compressed)):
            d = _http_dataset("https://example.com/data.csv.bz2", extract="bz2")
            result = ensure_datasets({"d": d}, tmp_path)

        # decompressed file should strip .bz2 suffix
        assert (result["d"] / "data.csv").read_bytes() == original
        assert (result["d"] / ".ygor_downloaded").exists()

    def test_download_with_rar(self, tmp_path):
        from unittest.mock import MagicMock, patch

        extracted_files: list[Path] = []

        def fake_extractall(dest):
            # simulate rarfile writing one file into dest
            out = Path(dest) / "data.mtx"
            out.write_text("rar content")
            extracted_files.append(out)

        mock_rf = MagicMock()
        mock_rf.__enter__ = lambda s: s
        mock_rf.__exit__ = MagicMock(return_value=False)
        mock_rf.extractall.side_effect = fake_extractall

        with (
            patch("urllib.request.urlretrieve", side_effect=self._fake_download(b"fake-rar-bytes")),
            patch("rarfile.RarFile", return_value=mock_rf),
        ):
            d = _http_dataset("https://example.com/data.rar", extract="rar")
            result = ensure_datasets({"d": d}, tmp_path)

        mock_rf.extractall.assert_called_once()
        assert (result["d"] / "data.mtx").read_text() == "rar content"
        assert (result["d"] / ".ygor_downloaded").exists()

    def test_download_failure_raises(self, tmp_path):
        with patch("urllib.request.urlretrieve", side_effect=OSError("network error")):
            d = _http_dataset("https://example.com/f.bin")
            with pytest.raises(DatasetResolutionError, match="download failed"):
                ensure_datasets({"d": d}, tmp_path)


# ---------------------------------------------------------------------------
# Git
# ---------------------------------------------------------------------------

def _make_local_git_repo(tmp_path: Path) -> tuple[Path, str]:
    """Create a local git repo with one commit; return (repo_path, commit_hash)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    env = {"GIT_CONFIG_NOSYSTEM": "1", "HOME": str(tmp_path)}
    for cmd in [
        ["git", "init", str(repo)],
        ["git", "-C", str(repo), "config", "user.email", "t@t.com"],
        ["git", "-C", str(repo), "config", "user.name", "T"],
    ]:
        subprocess.run(cmd, check=True, capture_output=True, env=env)
    (repo / "data.txt").write_text("dataset content")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True, capture_output=True, env=env)
    subprocess.run(
        ["git", "-C", str(repo), "commit", "-m", "init"],
        check=True, capture_output=True, env=env,
    )
    result = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        check=True, capture_output=True, text=True, env=env,
    )
    return repo, result.stdout.strip()


class TestResolveGit:
    def test_clone_and_checkout(self, tmp_path):
        repo, commit = _make_local_git_repo(tmp_path)
        d = _git_dataset(f"file://{repo}", commit)
        result = ensure_datasets({"d": d}, tmp_path / "store")
        cloned_dir = result["d"]
        assert (cloned_dir / "data.txt").read_text() == "dataset content"
        assert (cloned_dir / ".ygor_cloned").exists()

    def test_skips_if_sentinel_exists(self, tmp_path):
        target = _git_target(
            GitSource(type="git", url="file:///fake", ref="abc123"),
            tmp_path / "store",
        )
        target.mkdir(parents=True)
        (target / ".ygor_cloned").touch()

        called = []
        original = subprocess.run

        def _mock_run(*args, **kwargs):
            called.append(args)
            return original(*args, **kwargs)

        d = _git_dataset("file:///fake", "abc123")
        # ensure_datasets should return immediately without cloning
        result = ensure_datasets({"d": d}, tmp_path / "store")
        assert result["d"] == target

    def test_invalid_ref_raises(self, tmp_path):
        repo, _ = _make_local_git_repo(tmp_path)
        d = _git_dataset(f"file://{repo}", "nonexistent-branch-xyz")
        with pytest.raises(DatasetResolutionError, match="git failed"):
            ensure_datasets({"d": d}, tmp_path / "store")

    def test_invalid_url_raises(self, tmp_path):
        d = _git_dataset("file:///nonexistent/repo", "main")
        with pytest.raises(DatasetResolutionError, match="git failed"):
            ensure_datasets({"d": d}, tmp_path / "store")


# ---------------------------------------------------------------------------
# find_cached_datasets
# ---------------------------------------------------------------------------

class TestFindCachedDatasets:
    def test_local_existing_returned(self, tmp_path):
        ds_dir = tmp_path / "data"
        ds_dir.mkdir()
        d = _local_dataset(str(ds_dir))
        result = find_cached_datasets({"d": d}, tmp_path / "store")
        assert "d" in result
        assert result["d"] == ds_dir.resolve()

    def test_local_missing_skipped(self, tmp_path):
        d = _local_dataset(str(tmp_path / "nonexistent"))
        result = find_cached_datasets({"d": d}, tmp_path / "store")
        assert result == {}

    def test_http_with_sentinel_returned(self, tmp_path):
        d = _http_dataset("https://example.com/data.tar.gz", sha256="abc123")
        store = tmp_path / "store"
        target = _http_target(d.source, d.sha256, store)
        target.mkdir(parents=True)
        (target / ".ygor_downloaded").touch()
        result = find_cached_datasets({"d": d}, store)
        assert "d" in result
        assert result["d"] == target

    def test_http_without_sentinel_skipped(self, tmp_path):
        d = _http_dataset("https://example.com/data.tar.gz", sha256="abc123")
        store = tmp_path / "store"
        result = find_cached_datasets({"d": d}, store)
        assert result == {}

    def test_git_with_sentinel_returned(self, tmp_path):
        d = _git_dataset("https://github.com/example/repo.git", ref="abc123")
        store = tmp_path / "store"
        target = _git_target(d.source, store)
        target.mkdir(parents=True)
        (target / ".ygor_cloned").touch()
        result = find_cached_datasets({"d": d}, store)
        assert "d" in result

    def test_git_without_sentinel_skipped(self, tmp_path):
        d = _git_dataset("https://github.com/example/repo.git", ref="abc123")
        result = find_cached_datasets({"d": d}, tmp_path / "store")
        assert result == {}

    def test_empty_datasets(self, tmp_path):
        assert find_cached_datasets({}, tmp_path / "store") == {}

    def test_mixed_returns_only_available(self, tmp_path):
        local_dir = tmp_path / "local"
        local_dir.mkdir()
        datasets = {
            "local": _local_dataset(str(local_dir)),
            "remote": _http_dataset("https://example.com/data.tar.gz"),
        }
        result = find_cached_datasets(datasets, tmp_path / "store")
        assert set(result.keys()) == {"local"}


# ---------------------------------------------------------------------------
# ensure_datasets — integration
# ---------------------------------------------------------------------------

class TestEnsureDatasets:
    def test_empty_datasets(self, tmp_path):
        assert ensure_datasets({}, tmp_path) == {}

    def test_multiple_datasets(self, tmp_path):
        f1 = tmp_path / "a.csv"
        f1.write_text("x")
        f2 = tmp_path / "b.csv"
        f2.write_text("y")
        datasets = {
            "ds1": _local_dataset(str(f1)),
            "ds2": _local_dataset(str(f2)),
        }
        result = ensure_datasets(datasets, tmp_path / "store")
        assert set(result.keys()) == {"ds1", "ds2"}

    def test_on_status_callback(self, tmp_path):
        f = tmp_path / "data.csv"
        f.write_text("x")
        statuses: list[tuple[str, str]] = []
        ensure_datasets(
            {"d": _local_dataset(str(f))},
            tmp_path / "store",
            on_status=lambda n, m: statuses.append((n, m)),
        )
        assert any(n == "d" for n, _ in statuses)

    def test_first_error_stops_resolution(self, tmp_path):
        datasets = {
            "bad": _store_dataset("notfound"),
            "good": _local_dataset(str(tmp_path)),
        }
        with pytest.raises(DatasetResolutionError, match="bad"):
            ensure_datasets(datasets, tmp_path / "store")


# ---------------------------------------------------------------------------
# ygor run with dataset context injected into command
# ---------------------------------------------------------------------------

class TestYgorRunWithDatasets:
    def test_dataset_path_in_command(self, tmp_path):
        """dataset['name'] must be available in the Jinja2 command context."""
        from typer.testing import CliRunner
        from ygor.cli import app

        data_dir = tmp_path / "data"
        data_dir.mkdir()
        (data_dir / "input.txt").write_text("hello")

        out_file = tmp_path / "result.txt"
        recipe_content = (
            f"datasets:\n"
            f"  mydata:\n"
            f"    source:\n"
            f"      type: local\n"
            f"      path: '{data_dir}'\n"
            f"command: \"echo {{{{ dataset['mydata'] }}}} > {out_file}\"\n"
        )
        recipe_file = tmp_path / "recipe.yaml"
        recipe_file.write_text(recipe_content)

        runner = CliRunner()
        result = runner.invoke(
            app,
            ["run", str(recipe_file), "--store", str(tmp_path / "store"), "--yes"],
        )
        assert result.exit_code == 0
        assert str(data_dir) in out_file.read_text()


# ---------------------------------------------------------------------------
# resolve_instance_globs
# ---------------------------------------------------------------------------

class TestResolveInstanceGlobs:
    def _local_dataset(self, path: str) -> "Dataset":
        return _local_dataset(path)

    def test_local_dataset_resolved(self, tmp_path):
        ds_dir = tmp_path / "dimacs"
        ds_dir.mkdir()
        (ds_dir / "g1.mtx").write_text("a")
        (ds_dir / "g2.mtx").write_text("b")
        (ds_dir / "notes.txt").write_text("ignore")
        datasets = {"dimacs": _local_dataset(str(ds_dir))}
        result = resolve_instance_globs(["dataset:dimacs/*.mtx"], datasets)
        assert len(result) == 2
        assert all(r.endswith(".mtx") for r in result)
        assert all("dimacs" in r for r in result)

    def test_local_dataset_sorted(self, tmp_path):
        ds_dir = tmp_path / "d"
        ds_dir.mkdir()
        (ds_dir / "b.mtx").write_text("")
        (ds_dir / "a.mtx").write_text("")
        datasets = {"d": _local_dataset(str(ds_dir))}
        result = resolve_instance_globs(["dataset:d/*.mtx"], datasets)
        assert result == sorted(result)

    def test_local_dataset_subdir_glob(self, tmp_path):
        ds_dir = tmp_path / "bench"
        (ds_dir / "instances" / "big").mkdir(parents=True)
        (ds_dir / "instances" / "big" / "a.mtx").write_text("")
        (ds_dir / "instances" / "big" / "b.mtx").write_text("")
        datasets = {"bench": _local_dataset(str(ds_dir))}
        result = resolve_instance_globs(["dataset:bench/instances/big/*.mtx"], datasets)
        assert len(result) == 2

    def test_no_matches_returns_pattern(self, tmp_path):
        ds_dir = tmp_path / "empty"
        ds_dir.mkdir()
        datasets = {"empty": _local_dataset(str(ds_dir))}
        result = resolve_instance_globs(["dataset:empty/*.mtx"], datasets)
        assert result == ["dataset:empty/*.mtx"]

    def test_remote_dataset_not_resolved(self, tmp_path):
        datasets = {
            "remote": Dataset.model_validate({
                "source": {"type": "http", "url": "https://example.com/data.tar.gz"}
            })
        }
        result = resolve_instance_globs(["dataset:remote/*.mtx"], datasets)
        assert result == ["dataset:remote/*.mtx"]

    def test_missing_dataset_name_passthrough(self, tmp_path):
        result = resolve_instance_globs(["dataset:unknown/*.mtx"], {})
        assert result == ["dataset:unknown/*.mtx"]

    def test_non_dataset_pattern_passthrough(self):
        result = resolve_instance_globs(["/absolute/path/file.mtx"], {})
        assert result == ["/absolute/path/file.mtx"]

    def test_missing_slash_in_pattern_passthrough(self):
        result = resolve_instance_globs(["dataset:nodatasetslash"], {})
        assert result == ["dataset:nodatasetslash"]

    def test_multiple_patterns_combined(self, tmp_path):
        d1 = tmp_path / "d1"
        d1.mkdir()
        (d1 / "a.mtx").write_text("")
        d2 = tmp_path / "d2"
        d2.mkdir()
        (d2 / "b.mtx").write_text("")
        datasets = {
            "d1": _local_dataset(str(d1)),
            "d2": _local_dataset(str(d2)),
        }
        result = resolve_instance_globs(
            ["dataset:d1/*.mtx", "dataset:d2/*.mtx"], datasets
        )
        assert len(result) == 2
        assert any("a.mtx" in r for r in result)
        assert any("b.mtx" in r for r in result)

    def test_resolved_paths_override_local_source(self, tmp_path):
        real_dir = tmp_path / "real"
        real_dir.mkdir()
        (real_dir / "c.mtx").write_text("")
        decoy_dir = tmp_path / "decoy"
        decoy_dir.mkdir()
        (decoy_dir / "x.mtx").write_text("")
        datasets = {"ds": _local_dataset(str(decoy_dir))}
        result = resolve_instance_globs(
            ["dataset:ds/*.mtx"],
            datasets,
            resolved_paths={"ds": real_dir},
        )
        assert len(result) == 1
        assert "c.mtx" in result[0]

    def test_missing_local_path_passthrough(self, tmp_path):
        datasets = {"ds": _local_dataset(str(tmp_path / "nonexistent"))}
        result = resolve_instance_globs(["dataset:ds/*.mtx"], datasets)
        assert result == ["dataset:ds/*.mtx"]
