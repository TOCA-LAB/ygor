"""Read existing xLab configuration and cache markers without rewriting them."""

from pathlib import Path


LEGACY_CONFIG_FILE = ".labrc.yaml"
_LEGACY_CACHE_MARKERS = {
    ".ygor_cloned": ".lab_cloned",
    ".ygor_downloaded": ".lab_downloaded",
    ".ygor_image": ".lab_image",
}


def cache_marker_exists(directory: Path, marker: str) -> bool:
    """Accept the current marker or its historical equivalent, if defined."""
    if (directory / marker).exists():
        return True
    legacy = _LEGACY_CACHE_MARKERS.get(marker)
    return legacy is not None and (directory / legacy).exists()
