"""Dataset resolution: ensure each declared dataset is available locally."""

from __future__ import annotations

import bz2
import hashlib
import shutil
import tarfile
import urllib.request
import zipfile
from collections.abc import Callable

import rarfile
from pathlib import Path

from ygor.git_cache import GitCacheError, materialize_git_checkout
from ygor.compat import cache_marker_exists
from ygor.schema import Dataset, GitSource, HttpSource, LocalSource, StoreSource


# ---------------------------------------------------------------------------
# Error
# ---------------------------------------------------------------------------

class DatasetResolutionError(Exception):
    def __init__(self, name: str, message: str) -> None:
        self.name = name
        self.message = message
        super().__init__(f"dataset '{name}': {message}")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _http_target(source: HttpSource, sha256: str | None, store_root: Path) -> Path:
    key = sha256 if sha256 else hashlib.sha256(source.url.encode()).hexdigest()[:32]
    return store_root / "datasets" / key


def _git_target(source: GitSource, store_root: Path) -> Path:
    key = hashlib.sha256(f"{source.url}\0{source.ref}".encode()).hexdigest()[:32]
    return store_root / "datasets" / key


def _is_relative_to(path: Path, base: Path) -> bool:
    try:
        path.relative_to(base)
        return True
    except ValueError:
        return False


def _safe_join_extract_path(name: str, target: Path, member_name: str) -> Path:
    destination = (target / member_name).resolve()
    if not _is_relative_to(destination, target.resolve()):
        raise DatasetResolutionError(
            name,
            f"unsafe archive member path: {member_name!r}",
        )
    return destination


def _safe_extract_tar(name: str, archive: tarfile.TarFile, target: Path) -> None:
    for member in archive.getmembers():
        _safe_join_extract_path(name, target, member.name)
        if member.issym() or member.islnk():
            link_name = member.linkname
            link_target = Path(link_name)
            if not link_target.is_absolute():
                link_target = (
                    _safe_join_extract_path(name, target, member.name).parent / link_target
                ).resolve()
            if not _is_relative_to(link_target.resolve(), target.resolve()):
                raise DatasetResolutionError(
                    name,
                    f"unsafe archive link target: {member.name!r} -> {member.linkname!r}",
                )
    archive.extractall(target)


def _safe_extract_zip(name: str, archive: zipfile.ZipFile, target: Path) -> None:
    for member in archive.infolist():
        _safe_join_extract_path(name, target, member.filename)
    archive.extractall(target)


# ---------------------------------------------------------------------------
# Per-type resolution
# ---------------------------------------------------------------------------

def _resolve_http(name: str, dataset: Dataset, source: HttpSource, store_root: Path) -> Path:
    target = _http_target(source, dataset.sha256, store_root)
    if cache_marker_exists(target, ".ygor_downloaded"):
        return target

    target.mkdir(parents=True, exist_ok=True)
    tmp_file = target.parent / f".tmp_{target.name}"

    try:
        urllib.request.urlretrieve(source.url, tmp_file)
    except Exception as exc:
        raise DatasetResolutionError(name, f"download failed: {exc}") from exc

    if dataset.sha256:
        actual = _sha256_file(tmp_file)
        if actual != dataset.sha256:
            tmp_file.unlink(missing_ok=True)
            raise DatasetResolutionError(
                name,
                f"sha256 mismatch — expected {dataset.sha256!r}, got {actual!r}",
            )

    try:
        ext = dataset.extract
        if ext == "tar.gz":
            with tarfile.open(tmp_file, "r:gz") as tf:
                _safe_extract_tar(name, tf, target)
        elif ext in ("tar.bz2", "tar.xz"):
            with tarfile.open(tmp_file) as tf:
                _safe_extract_tar(name, tf, target)
        elif ext == "bz2":
            url_name = Path(source.url).name or "downloaded.bz2"
            out_name = url_name[:-4] if url_name.endswith(".bz2") else url_name
            with bz2.open(tmp_file, "rb") as f_in, (target / out_name).open("wb") as f_out:
                shutil.copyfileobj(f_in, f_out)
        elif ext == "rar":
            with rarfile.RarFile(tmp_file) as rf:
                rf.extractall(target)
        elif ext == "zip":
            with zipfile.ZipFile(tmp_file) as zf:
                _safe_extract_zip(name, zf, target)
        else:
            filename = Path(source.url).name or "downloaded"
            shutil.move(str(tmp_file), target / filename)
            tmp_file = None  # already moved
    except DatasetResolutionError:
        raise
    except Exception as exc:
        raise DatasetResolutionError(name, f"extraction failed: {exc}") from exc
    finally:
        if tmp_file is not None and Path(tmp_file).exists():
            Path(tmp_file).unlink()

    (target / ".ygor_downloaded").touch()
    return target


def _resolve_git(name: str, source: GitSource, store_root: Path) -> Path:
    target = _git_target(source, store_root)
    try:
        return materialize_git_checkout(
            url=source.url,
            ref=source.ref,
            target=target,
        )
    except GitCacheError as exc:
        raise DatasetResolutionError(name, f"{exc.phase} failed: {exc.message}") from exc


def _resolve_local(name: str, source: LocalSource) -> Path:
    path = Path(source.path)
    if not path.exists():
        raise DatasetResolutionError(name, f"local path not found: {path}")
    return path.resolve()


def _resolve_store(name: str, dataset: Dataset, store_root: Path) -> Path:
    hash_prefix = dataset.hash or dataset.source.hash
    if not hash_prefix:
        raise DatasetResolutionError(name, "store source requires a 'hash' field")

    runs_dir = store_root / "runs"
    if not runs_dir.is_dir():
        raise DatasetResolutionError(
            name,
            f"run '{hash_prefix}' not found in store — use `ygor pull {hash_prefix}` to sync",
        )

    matches = [d for d in runs_dir.iterdir() if d.name.startswith(hash_prefix)]
    if not matches:
        raise DatasetResolutionError(
            name,
            f"run '{hash_prefix}' not found in store — use `ygor pull {hash_prefix}` to sync",
        )
    if len(matches) > 1:
        short = [d.name[:8] for d in matches]
        raise DatasetResolutionError(
            name,
            f"ambiguous hash prefix '{hash_prefix}' matches {len(matches)} runs: {short}",
        )
    return matches[0]


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def ensure_datasets(
    datasets: dict[str, Dataset],
    store_root: Path,
    *,
    on_status: Callable[[str, str], None] | None = None,
) -> dict[str, Path]:
    """Ensure every declared dataset is available locally.

    Returns a mapping ``name → resolved local path``.
    Raises :class:`DatasetResolutionError` on the first unresolvable dataset.

    *on_status* is an optional callback ``(name, message)`` for progress reporting.
    """
    resolved: dict[str, Path] = {}
    for name, dataset in datasets.items():
        source = dataset.source

        if on_status:
            on_status(name, "checking…")

        if isinstance(source, HttpSource):
            path = _resolve_http(name, dataset, source, store_root)
        elif isinstance(source, GitSource):
            path = _resolve_git(name, source, store_root)
        elif isinstance(source, LocalSource):
            path = _resolve_local(name, source)
        elif isinstance(source, StoreSource):
            path = _resolve_store(name, dataset, store_root)
        else:
            raise DatasetResolutionError(name, f"unknown source type: {type(source).__name__}")

        if on_status:
            on_status(name, f"ready → {path}")
        resolved[name] = path

    return resolved


def find_cached_datasets(
    datasets: dict[str, Dataset],
    store_root: Path,
) -> dict[str, Path]:
    """Return paths for datasets that are already on disk, without downloading.

    Local sources are resolved immediately.
    HTTP/git sources are returned only if previously downloaded (sentinel exists).
    Datasets not yet available are silently skipped.
    """
    found: dict[str, Path] = {}
    for name, dataset in datasets.items():
        source = dataset.source
        if isinstance(source, LocalSource):
            p = Path(source.path)
            if p.exists():
                found[name] = p.resolve()
        elif isinstance(source, HttpSource):
            target = _http_target(source, dataset.sha256, store_root)
            if cache_marker_exists(target, ".ygor_downloaded"):
                found[name] = target
        elif isinstance(source, GitSource):
            target = _git_target(source, store_root)
            if cache_marker_exists(target, ".ygor_cloned"):
                found[name] = target
        elif isinstance(source, StoreSource):
            try:
                found[name] = _resolve_store(name, dataset, store_root)
            except DatasetResolutionError:
                pass
    return found


# ---------------------------------------------------------------------------
# Instance glob resolution
# ---------------------------------------------------------------------------

def resolve_instance_globs(
    patterns: list[str],
    datasets: dict[str, Dataset],
    resolved_paths: dict[str, Path] | None = None,
) -> list[str]:
    """Expand ``dataset:name/glob`` patterns to concrete file paths.

    For each pattern:
    - If *resolved_paths* contains the dataset name, globs against that path.
    - Otherwise, for local-source datasets whose path exists, globs directly.
    - Remote datasets (http/git) or missing paths are kept as-is.

    Patterns that do not start with ``dataset:`` are passed through unchanged.
    """
    result: list[str] = []

    for pattern in patterns:
        if not pattern.startswith("dataset:"):
            result.append(pattern)
            continue

        rest = pattern[len("dataset:"):]
        try:
            slash_idx = rest.index("/")
        except ValueError:
            result.append(pattern)
            continue

        name, glob_pat = rest[:slash_idx], rest[slash_idx + 1:]

        # Determine base path: prefer pre-resolved, fall back to local source
        base: Path | None = None
        if resolved_paths and name in resolved_paths:
            base = resolved_paths[name]
        else:
            dataset = datasets.get(name)
            if dataset is not None and isinstance(dataset.source, LocalSource):
                candidate = Path(dataset.source.path)
                if candidate.exists():
                    base = candidate.resolve()

        if base is None:
            result.append(pattern)
            continue

        matches = sorted(base.glob(glob_pat))
        if matches:
            result.extend(str(m) for m in matches)
        else:
            result.append(pattern)

    return result if result else list(patterns)
