#!/usr/bin/env python3
"""Detect → remediate → retry: Parking/Event merge/compose consistency repair."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol

from import_70mai import log
from plan_estimate import SINGLE_VIDEO_TYPES

REPAIR_LOG_FILENAME = "repair_log.jsonl"
COVERAGE_THRESHOLD = 0.98
FB_MISMATCH_RATIO = 0.02  # >2% Front/Back duration spread
MAX_REMEDIATIONS_PER_CHUNK = 2

REBUILD_CODES = frozenset(
    {
        "merge_short",
        "merge_stale",
        "merge_fb_mismatch",
        "compose_gap",
        "compose_part_stale",
        "state_drift",
        "manifest_missing",
    }
)


@dataclass(frozen=True)
class HealthIssue:
    code: str
    record_type: str
    camera: str | None
    severity: str  # blocker | warn
    message: str
    remediation: str  # rebuild_merge | none
    path: Path | None = None


class _ImportStoreLike(Protocol):
    def invalidate_merge(
        self, *, record_type: str, camera: str, filename: str
    ) -> None: ...

    def get_merge_entry(
        self, *, record_type: str, camera: str, filename: str
    ) -> dict | None: ...

    def compact_event_state(self, record_type: str) -> int: ...


def repair_log_path(temp_dir: Path) -> Path:
    return temp_dir / REPAIR_LOG_FILENAME


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def append_repair_log(
    temp_dir: Path,
    *,
    action: str,
    detail: str,
    record_type: str = "",
    camera: str = "",
    code: str = "",
) -> None:
    temp_dir.mkdir(parents=True, exist_ok=True)
    entry = {
        "ts": _utc_now(),
        "action": action,
        "detail": detail,
        "record_type": record_type,
        "camera": camera,
        "code": code,
    }
    path = repair_log_path(temp_dir)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry, ensure_ascii=False) + "\n")


def read_recent_repairs(temp_dir: Path, *, limit: int = 8) -> list[dict]:
    path = repair_log_path(temp_dir)
    if not path.is_file():
        return []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    out: list[dict] = []
    for line in lines[-limit:]:
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def _clip_duration(clip: Any) -> float | None:
    dur = getattr(clip, "duration", None)
    if dur is not None:
        return float(dur)
    return None


def _segment_coverage(
    video_dir: Path,
    record_type: str,
    camera: str,
    trip_start,
    duration_sec: float,
) -> tuple[float | None, str | None]:
    """Return (covered_sec, error). covered_sec is None on hard failure."""
    from compose_70mai import plan_segments, scan_merged_clips

    try:
        clips = scan_merged_clips(
            video_dir, camera, record_type=record_type, probe=True
        )
    except (OSError, RuntimeError, ValueError) as exc:
        return None, str(exc)
    if not clips:
        return None, f"no {record_type}/{camera} merges"
    try:
        segments = plan_segments(clips, trip_start, duration_sec, 0.0)
    except (ValueError, OSError, RuntimeError) as exc:
        return None, str(exc)
    covered = sum(seg.duration for seg in segments)
    return covered, None


def _merged_duration(
    video_dir: Path, record_type: str, camera: str
) -> tuple[float | None, Path | None]:
    from compose_70mai import scan_merged_clips

    clips = scan_merged_clips(video_dir, camera, record_type=record_type, probe=True)
    if not clips:
        return None, None
    # Event/Parking: one mega-file; Normal: sum of covering clips handled via plan_segments
    if record_type in SINGLE_VIDEO_TYPES:
        clip = clips[0]
        return _clip_duration(clip), clip.path
    return sum(_clip_duration(c) or 0.0 for c in clips), clips[0].path


def _manifest_matches_file(merge_path: Path | None) -> bool:
    """True when a fresh timeline manifest exists whose duration matches the
    merge file (so compose can align gaps with black instead of drifting)."""
    if merge_path is None:
        return False
    try:
        from clip_timeline import manifest_matches_merge

        return manifest_matches_merge(merge_path)
    except Exception:
        return False


def _aligned_ready(video_dir: Path, record_type: str) -> bool:
    from clip_timeline import merges_timeline_ready

    ok, _ = merges_timeline_ready(video_dir, record_type)
    return ok


def diagnose_chunk(
    source: Path | None,
    video_dir: Path,
    chunk: Any,
    *,
    import_store: _ImportStoreLike | None = None,
    uploaded: bool = False,
) -> list[HealthIssue]:
    """Return health issues for a pending chunk (empty if OK)."""
    if uploaded:
        return []

    record_type = chunk.record_type
    issues: list[HealthIssue] = []
    trip_duration = float(chunk.duration_sec)

    front_dur, front_path = _merged_duration(video_dir, record_type, "Front")
    back_dur, back_path = _merged_duration(video_dir, record_type, "Back")

    if front_dur is None or back_dur is None:
        issues.append(
            HealthIssue(
                code="compose_gap",
                record_type=record_type,
                camera=None,
                severity="blocker",
                message=(
                    f"Missing Front/Back merges for {record_type} "
                    f"(front={front_dur}, back={back_dur})"
                ),
                remediation="rebuild_merge",
                path=front_path or back_path,
            )
        )
        return issues

    # When both merges carry a fresh timeline manifest, compose is slot-aligned
    # and fills missing/short clips with black — coverage gaps are then expected
    # (warn), not blockers. A missing/stale manifest stays a blocker so import
    # rebuilds it.
    aligned = _aligned_ready(video_dir, record_type)
    cov_sev = "warn" if aligned else "blocker"
    if not aligned:
        from compose_70mai import scan_merged_clips

        for camera in ("Front", "Back"):
            for clip in scan_merged_clips(
                video_dir, camera, record_type=record_type, probe=False
            ):
                if not _manifest_matches_file(clip.path):
                    issues.append(
                        HealthIssue(
                            code="manifest_missing",
                            record_type=record_type,
                            camera=camera,
                            severity="blocker",
                            message=(
                                f"{record_type}/{camera} {clip.path.name} has no "
                                "fresh timeline manifest — rebuild for slot-aligned "
                                "compose"
                            ),
                            remediation="rebuild_merge",
                            path=clip.path,
                        )
                    )

    if record_type in SINGLE_VIDEO_TYPES:
        for camera, dur, path in (
            ("Front", front_dur, front_path),
            ("Back", back_dur, back_path),
        ):
            if dur < trip_duration * COVERAGE_THRESHOLD:
                if path is not None:
                    from import_70mai import user_accepted_short_merge

                    if user_accepted_short_merge(path):
                        continue
                issues.append(
                    HealthIssue(
                        code="merge_short",
                        record_type=record_type,
                        camera=camera,
                        severity=cov_sev,
                        message=(
                            f"{record_type}/{camera} merge {dur:.1f}s < "
                            f"{COVERAGE_THRESHOLD:.0%} of trip {trip_duration:.1f}s"
                        ),
                        remediation="rebuild_merge",
                        path=path,
                    )
                )

        longer = max(front_dur, back_dur)
        shorter = min(front_dur, back_dur)
        if longer > 0 and (longer - shorter) / longer > FB_MISMATCH_RATIO:
            short_cam = "Front" if front_dur < back_dur else "Back"
            issues.append(
                HealthIssue(
                    code="merge_fb_mismatch",
                    record_type=record_type,
                    camera=short_cam,
                    severity=cov_sev,
                    message=(
                        f"{record_type} Front/Back duration mismatch: "
                        f"{front_dur:.1f}s vs {back_dur:.1f}s"
                    ),
                    remediation="rebuild_merge",
                    path=front_path if short_cam == "Front" else back_path,
                )
            )

        if import_store is not None:
            for camera, path in (("Front", front_path), ("Back", back_path)):
                if path is None:
                    continue
                entry = import_store.get_merge_entry(
                    record_type=record_type,
                    camera=camera,
                    filename=path.name,
                )
                expected = (entry or {}).get("expected_duration_sec")
                if not expected or float(expected) <= 0:
                    continue
                file_dur = front_dur if camera == "Front" else back_dur
                if file_dur < float(expected) * COVERAGE_THRESHOLD:
                    issues.append(
                        HealthIssue(
                            code="merge_stale",
                            record_type=record_type,
                            camera=camera,
                            severity="blocker",
                            message=(
                                f"{record_type}/{camera} {path.name} shorter "
                                f"than expected {float(expected):.1f}s "
                                f"(got {file_dur:.1f}s)"
                            ),
                            remediation="rebuild_merge",
                            path=path,
                        )
                    )

    for trip in chunk.trips:
        for camera in ("Front", "Back"):
            covered, err = _segment_coverage(
                video_dir,
                record_type,
                camera,
                trip.start,
                trip.duration_sec,
            )
            if err or covered is None:
                issues.append(
                    HealthIssue(
                        code="compose_gap",
                        record_type=record_type,
                        camera=camera,
                        severity=cov_sev,
                        message=(
                            f"plan_segments {record_type}/{camera} trip "
                            f"{trip.index}: {err or 'no coverage'}"
                        ),
                        remediation="rebuild_merge",
                        path=(
                            front_path if camera == "Front" else back_path
                        ),
                    )
                )
            elif covered < trip.duration_sec * COVERAGE_THRESHOLD:
                issues.append(
                    HealthIssue(
                        code="compose_gap",
                        record_type=record_type,
                        camera=camera,
                        severity=cov_sev,
                        message=(
                            f"{record_type}/{camera} covers {covered:.1f}s of "
                            f"needed {trip.duration_sec:.1f}s "
                            f"(trip {trip.index})"
                        ),
                        remediation="rebuild_merge",
                        path=(
                            front_path if camera == "Front" else back_path
                        ),
                    )
                )

    # Deduplicate by (code, camera, path)
    seen: set[tuple] = set()
    unique: list[HealthIssue] = []
    for issue in issues:
        key = (issue.code, issue.camera, str(issue.path) if issue.path else "")
        if key in seen:
            continue
        seen.add(key)
        unique.append(issue)
    return unique


def remediate(
    issues: list[HealthIssue],
    *,
    video_dir: Path,
    temp_dir: Path | None = None,
    import_store: _ImportStoreLike | None = None,
    dry_run: bool = False,
) -> list[str]:
    """Apply remediations. Returns human-readable action lines."""
    actions: list[str] = []
    rebuild_paths: set[Path] = set()

    for issue in issues:
        if issue.severity != "blocker":
            continue
        if issue.remediation != "rebuild_merge":
            continue
        if issue.path is not None:
            rebuild_paths.add(issue.path)
        elif issue.camera and issue.record_type in SINGLE_VIDEO_TYPES:
            folder = video_dir / issue.record_type / issue.camera
            prefix = "PA_" if issue.record_type == "Parking" else "EV_"
            if folder.is_dir():
                for path in folder.glob(f"{prefix}*.mp4"):
                    rebuild_paths.add(path)

    for path in sorted(rebuild_paths):
        parts = path.parts
        # .../Parking/Front/PA_....mp4
        try:
            camera = parts[-2]
            record_type = parts[-3]
        except IndexError:
            camera, record_type = "", ""
        detail = f"delete {path}"
        if dry_run:
            line = f"[repair] would rebuild: {path.name} ({record_type}/{camera})"
            actions.append(line)
            log(line)
            if temp_dir:
                append_repair_log(
                    temp_dir,
                    action="would_rebuild",
                    detail=detail,
                    record_type=record_type,
                    camera=camera,
                    code="rebuild_merge",
                )
            continue

        if path.is_file():
            path.unlink()
            _delete_manifest_sidecar(path)
            line = f"[repair] rebuilt (deleted merge): {path.name}"
            actions.append(line)
            log(line)
            if temp_dir:
                append_repair_log(
                    temp_dir,
                    action="deleted_merge",
                    detail=detail,
                    record_type=record_type,
                    camera=camera,
                    code="rebuild_merge",
                )
        if import_store is not None and record_type and camera:
            import_store.invalidate_merge(
                record_type=record_type,
                camera=camera,
                filename=path.name,
            )
            if record_type in SINGLE_VIDEO_TYPES:
                n = import_store.compact_event_state(record_type)
                if n:
                    line = (
                        f"[repair] compacted {n} stale import-state "
                        f"entries for {record_type}"
                    )
                    actions.append(line)
                    log(line)

    return actions


def _delete_manifest_sidecar(merge_path: Path) -> None:
    try:
        from clip_timeline import manifest_path_for

        manifest_path_for(merge_path).unlink(missing_ok=True)
    except Exception:
        pass


def capped_compose_duration(
    trip_duration: float,
    front_dur: float | None,
    back_dur: float | None,
) -> float:
    """Safe compose duration = min(trip, front, back) when merges exist."""
    candidates = [trip_duration]
    if front_dur is not None and front_dur > 0:
        candidates.append(front_dur)
    if back_dur is not None and back_dur > 0:
        candidates.append(back_dur)
    return min(candidates)


def diagnose_and_repair(
    source: Path | None,
    video_dir: Path,
    chunk: Any,
    *,
    temp_dir: Path,
    import_store: _ImportStoreLike | None = None,
    uploaded: bool = False,
    mode: str = "auto",
) -> tuple[bool, list[HealthIssue], list[str]]:
    """Diagnose and optionally remediate.

    Returns (ok_to_publish_without_reimport, issues, actions).
    When issues remain after diagnose-only, ok is False.
    When mode=auto and remediations ran, ok is False (caller must reimport).
    When no blockers, ok is True.
    """
    if mode == "off":
        return True, [], []

    log(f"[repair] diagnosing {chunk.record_type} chunk {chunk.index}...")
    issues = diagnose_chunk(
        source,
        video_dir,
        chunk,
        import_store=import_store,
        uploaded=uploaded,
    )
    blockers = [i for i in issues if i.severity == "blocker"]
    if not blockers:
        log(f"[repair] {chunk.record_type} chunk {chunk.index}: OK")
        return True, issues, []

    for issue in blockers:
        log(f"[repair] {issue.code}: {issue.message}")
        append_repair_log(
            temp_dir,
            action="diagnosed",
            detail=issue.message,
            record_type=issue.record_type,
            camera=issue.camera or "",
            code=issue.code,
        )

    if mode == "diagnose":
        return False, issues, []

    actions = remediate(
        blockers,
        video_dir=video_dir,
        temp_dir=temp_dir,
        import_store=import_store,
        dry_run=False,
    )
    return False, issues, actions
