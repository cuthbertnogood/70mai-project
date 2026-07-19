"""Compose/upload temp paths under .publish_tmp (scoped by record_type)."""

from __future__ import annotations

from pathlib import Path

RECORD_TYPES = ("Normal", "Event", "Parking")


def compose_chunk_dir(temp_dir: Path, record_type: str, chunk_index: int) -> Path:
    return temp_dir / record_type / f"chunk_{chunk_index:02d}"


def compose_trip_path(
    temp_dir: Path,
    record_type: str,
    chunk_index: int,
    trip_index: int,
) -> Path:
    return compose_chunk_dir(temp_dir, record_type, chunk_index) / (
        f"trip_{trip_index:02d}.mp4"
    )


def legacy_compose_chunk_dir(temp_dir: Path, chunk_index: int) -> Path:
    return temp_dir / f"chunk_{chunk_index:02d}"


def legacy_compose_trip_path(
    temp_dir: Path, chunk_index: int, trip_index: int
) -> Path:
    return legacy_compose_chunk_dir(temp_dir, chunk_index) / (
        f"trip_{trip_index:02d}.mp4"
    )


def resolve_compose_trip_path(
    temp_dir: Path,
    record_type: str,
    chunk_index: int,
    trip_index: int,
) -> Path:
    """Prefer typed path; fall back to legacy flat chunk_NN/ layout."""
    typed = compose_trip_path(temp_dir, record_type, chunk_index, trip_index)
    if typed.is_file():
        return typed
    legacy = legacy_compose_trip_path(temp_dir, chunk_index, trip_index)
    if legacy.is_file():
        return legacy
    return typed


def is_legacy_compose_path(temp_dir: Path, path: Path) -> bool:
    try:
        rel = path.resolve().relative_to(temp_dir.resolve())
    except ValueError:
        return False
    parts = rel.parts
    return len(parts) == 2 and parts[0].startswith("chunk_")


def compose_part_path(temp_dir: Path, record_type: str, chunk_index: int) -> Path:
    return temp_dir / record_type / f"part_{chunk_index:02d}.mp4"


def iter_compose_video_roots(temp_dir: Path):
    """Yield dirs that may contain composed *.mp4 (typed + legacy)."""
    if not temp_dir.is_dir():
        return
    for record_type in RECORD_TYPES:
        root = temp_dir / record_type
        if root.is_dir():
            yield root
    for root in temp_dir.glob("chunk_*"):
        if root.is_dir():
            yield root


def publish_temp_dir(path: Path) -> Path | None:
    """Return `.publish_tmp` directory containing a compose output path."""
    parts = path.parts
    try:
        idx = parts.index(".publish_tmp")
    except ValueError:
        return None
    return Path(*parts[: idx + 1])


def parse_compose_output_path(path: Path) -> tuple[str | None, int, int] | None:
    """Parse record_type, chunk_index, trip_index from a compose output path."""
    parts = path.parts
    try:
        idx = parts.index(".publish_tmp")
    except ValueError:
        return None
    tail = parts[idx + 1 :]
    if len(tail) >= 3 and tail[0] in RECORD_TYPES and tail[1].startswith("chunk_"):
        record_type = tail[0]
        chunk_index = int(tail[1].split("_", 1)[1])
        trip_index = int(Path(tail[2]).stem.split("_", 1)[1])
        return record_type, chunk_index, trip_index
    if len(tail) >= 2 and tail[0].startswith("chunk_"):
        chunk_index = int(tail[0].split("_", 1)[1])
        trip_index = int(Path(tail[1]).stem.split("_", 1)[1])
        return None, chunk_index, trip_index
    return None
