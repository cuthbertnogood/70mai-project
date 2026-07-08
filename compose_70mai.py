#!/usr/bin/env python3
"""Compose vertical video: Screen Recording + Front + Back dashcam."""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
import tempfile
import time
import wave
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from import_70mai import log

SCREEN_RE = re.compile(
    r"^ScreenRecording_(\d{2}-\d{2}-\d{4}) (\d{2}-\d{2}-\d{2})",
    re.IGNORECASE,
)
MERGED_RE = re.compile(
    r"^NO_(\d{8})-(\d{6})_(\d{6})_([FB])\.mp4$",
    re.IGNORECASE,
)
EVENT_EXPORT_RE = re.compile(
    r"^EV_(\d{8})-(\d{6})_([FB])\.mp4$",
    re.IGNORECASE,
)
EVENT_MERGED_RE = re.compile(
    r"^EV_(\d{8})-(\d{6})_(\d{6})_([FB])\.mp4$",
    re.IGNORECASE,
)

DEFAULT_PROFILE = "balanced"
DEFAULT_DURATION = 600.0  # 10 minutes
BAR_WIDTH = 36
FFMPEG_TIME_RE = re.compile(r"time=(\d{2}):(\d{2}):(\d{2}\.\d{2})")
FFMPEG_SPEED_RE = re.compile(r"speed=\s*([\d.]+)x")

PROFILES: dict[str, dict[str, int | bool | str]] = {
    "balanced": {
        "hw": True,
        "hw_quality": 65,
        "width": 1206,
        "fps": 25,
        "hw_decode": True,
        "use_vt_scale": False,
        "codec": "h264",
    },
    "draft": {
        "hw": True,
        "hw_quality": 50,
        "width": 960,
        "fps": 20,
        "hw_decode": True,
        "use_vt_scale": False,
        "codec": "h264",
    },
    "quality": {
        "hw": True,
        "hw_quality": 75,
        "width": 1206,
        "fps": 25,
        "hw_decode": True,
        "use_vt_scale": False,
        "codec": "h264",
    },
    # HEVC ~3.5 Mbps visually matches H.264 6.5 Mbps → ~1.9x smaller upload.
    "hevc": {
        "hw": True,
        "hw_quality": 35,
        "width": 1206,
        "fps": 25,
        "hw_decode": True,
        "use_vt_scale": False,
        "codec": "hevc",
    },
}


@dataclass(frozen=True)
class MergedClip:
    path: Path
    start: datetime
    end: datetime
    camera: str  # "Front" or "Back"
    duration: float | None = None


def format_duration(seconds: float) -> str:
    seconds = max(0, int(seconds))
    hours, rem = divmod(seconds, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}h {minutes:02d}m {secs:02d}s"
    if minutes:
        return f"{minutes}m {secs:02d}s"
    return f"{secs}s"


def format_bar(ratio: float, width: int = BAR_WIDTH) -> str:
    ratio = max(0.0, min(1.0, ratio))
    filled = int(width * ratio)
    return "█" * filled + "░" * (width - filled)


def is_tty() -> bool:
    return sys.stderr.isatty()


def parse_ffmpeg_time(line: str) -> float | None:
    match = FFMPEG_TIME_RE.search(line)
    if not match:
        return None
    hours, minutes, seconds = int(match.group(1)), int(match.group(2)), float(match.group(3))
    return hours * 3600 + minutes * 60 + seconds


class EncodeProgress:
    """Live progress bar while ffmpeg encodes."""

    def __init__(self, duration_sec: float) -> None:
        self.duration_sec = max(duration_sec, 0.1)
        self.start = time.monotonic()
        self.current_sec = 0.0
        self.speed = 0.0
        self._last_logged_pct = -1
        self._render(force_log=True)

    def update_from_line(self, line: str) -> None:
        current = parse_ffmpeg_time(line)
        if current is not None:
            self.current_sec = current
        speed_match = FFMPEG_SPEED_RE.search(line)
        if speed_match:
            self.speed = float(speed_match.group(1))
        if current is not None or speed_match:
            self._render()

    def _render(self, force_log: bool = False) -> None:
        ratio = min(1.0, self.current_sec / self.duration_sec)
        elapsed = time.monotonic() - self.start
        pct = 100 * ratio
        eta = 0.0
        if self.speed > 0:
            remaining = max(0.0, self.duration_sec - self.current_sec)
            eta = remaining / self.speed
        elif ratio > 0:
            eta = elapsed * (1.0 - ratio) / ratio
        bar = format_bar(ratio)
        speed_txt = f"{self.speed:.2f}x" if self.speed > 0 else "—"
        line = (
            f"Encode: [{bar}] {format_duration(self.current_sec)}/"
            f"{format_duration(self.duration_sec)} ({pct:.1f}%) "
            f"| {format_duration(elapsed)} elapsed | ETA {format_duration(eta)} "
            f"| speed {speed_txt}"
        )

        if is_tty() and not force_log:
            sys.stderr.write("\r\033[K" + line)
            sys.stderr.flush()
            return

        pct_bucket = int(pct)
        if force_log or pct_bucket > self._last_logged_pct or ratio >= 1.0:
            log(line)
            self._last_logged_pct = pct_bucket

    def finish(self) -> None:
        self.current_sec = self.duration_sec
        self._render(force_log=True)
        if is_tty():
            sys.stderr.write("\n")
            sys.stderr.flush()


def run_ffmpeg_with_progress(cmd: list[str], *, duration_sec: float) -> None:
    progress = EncodeProgress(duration_sec)
    stderr_chunks: list[str] = []
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
    )
    assert proc.stderr is not None
    buf = ""
    while True:
        chunk = proc.stderr.read(4096)
        if not chunk:
            break
        stderr_chunks.append(chunk)
        buf += chunk
        while True:
            idx_r = buf.find("\r")
            idx_n = buf.find("\n")
            if idx_r == -1 and idx_n == -1:
                break
            if idx_r != -1 and (idx_n == -1 or idx_r <= idx_n):
                segment, buf = buf[:idx_r], buf[idx_r + 1 :]
            else:
                segment, buf = buf[:idx_n], buf[idx_n + 1 :]
            segment = segment.strip()
            if segment and "frame=" in segment:
                progress.update_from_line(segment)
    proc.wait()
    progress.finish()
    if proc.returncode != 0:
        raise subprocess.CalledProcessError(
            proc.returncode,
            cmd,
            stderr="".join(stderr_chunks),
        )


def parse_screen_start(path: Path) -> datetime:
    match = SCREEN_RE.match(path.name)
    if not match:
        raise ValueError(
            f"Cannot parse screen recording start from {path.name!r}. "
            "Expected: ScreenRecording_MM-DD-YYYY HH-MM-SS_*.mp4"
        )
    date_part, time_part = match.groups()
    return datetime.strptime(
        date_part.replace("-", "/") + " " + time_part.replace("-", ":"),
        "%m/%d/%Y %H:%M:%S",
    )


def parse_merged_file(path: Path) -> MergedClip | None:
    match = MERGED_RE.match(path.name)
    if not match:
        return None
    date_part, start_part, end_part, cam_suffix = match.groups()
    start = datetime.strptime(date_part + start_part, "%Y%m%d%H%M%S")
    end = datetime.strptime(date_part + end_part, "%Y%m%d%H%M%S")
    camera = "Front" if cam_suffix.upper() == "F" else "Back"
    return MergedClip(path=path, start=start, end=end, camera=camera)


def parse_event_export_file(path: Path) -> MergedClip | None:
    match = EVENT_MERGED_RE.match(path.name)
    if match:
        date_part, start_part, end_part, cam_suffix = match.groups()
        start = datetime.strptime(date_part + start_part, "%Y%m%d%H%M%S")
        end = datetime.strptime(date_part + end_part, "%Y%m%d%H%M%S")
        camera = "Front" if cam_suffix.upper() == "F" else "Back"
        return MergedClip(path=path, start=start, end=end, camera=camera)
    match = EVENT_EXPORT_RE.match(path.name)
    if not match:
        return None
    date_part, time_part, cam_suffix = match.groups()
    start = datetime.strptime(date_part + time_part, "%Y%m%d%H%M%S")
    camera = "Front" if cam_suffix.upper() == "F" else "Back"
    return MergedClip(path=path, start=start, end=start, camera=camera)


def scan_merged_clips(
    video_dir: Path,
    camera: str,
    *,
    record_type: str = "Normal",
    probe: bool = True,
) -> list[MergedClip]:
    folder = video_dir / record_type / camera
    if not folder.is_dir():
        return []
    if record_type == "Event":
        glob_pattern = "EV_*.mp4"
        parse_fn = parse_event_export_file
    else:
        glob_pattern = "NO_*.mp4"
        parse_fn = parse_merged_file
    clips: list[MergedClip] = []
    for path in sorted(folder.glob(glob_pattern)):
        parsed = parse_fn(path)
        if parsed:
            duration = probe_duration(path) if probe else None
            end = parsed.start + timedelta(seconds=duration) if duration else parsed.end
            clips.append(
                MergedClip(
                    path=parsed.path,
                    start=parsed.start,
                    end=end,
                    camera=parsed.camera,
                    duration=duration,
                )
            )
    return clips


def clip_covers(clip: MergedClip, moment: datetime) -> bool:
    if moment < clip.start:
        return False
    if clip.duration is not None:
        return moment < clip.start + timedelta(seconds=clip.duration)
    return moment <= clip.end


def find_clip_at(clips: list[MergedClip], moment: datetime) -> tuple[MergedClip, float]:
    for clip in clips:
        if clip_covers(clip, moment):
            offset = (moment - clip.start).total_seconds()
            return clip, offset
    raise ValueError(f"No merged clip covers {moment:%Y-%m-%d %H:%M:%S}")


def resolve_moment(clips: list[MergedClip], moment: datetime) -> datetime:
    """Return moment if inside a clip; otherwise jump to the next clip start (merge gap)."""
    for clip in clips:
        if clip_covers(clip, moment):
            return moment
    for clip in clips:
        if clip.start > moment:
            return clip.start
    raise ValueError(f"No merged clip covers {moment:%Y-%m-%d %H:%M:%S}")


def _probe_duration_uncached(path: Path) -> float:
    result = subprocess.run(
        [
            "ffprobe",
            "-v",
            "quiet",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(path),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    return float(result.stdout.strip())


def probe_duration(path: Path) -> float:
    from probe_cache import cached_probe_duration

    return cached_probe_duration(path, _probe_duration_uncached)


@dataclass(frozen=True)
class Segment:
    path: Path
    ss: float
    duration: float


@dataclass(frozen=True)
class AudioAnalysis:
    mode: str  # screen, front, mix
    offset_front: float  # seconds; positive delays front audio
    envelope_corr: float
    waveform_corr: float
    music_rms_screen: float
    music_rms_front: float


ENVELOPE_MIX_THRESHOLD = 0.45
ENVELOPE_FRONT_THRESHOLD = 0.15
AUDIO_ANALYZE_SEC = 12.0
AUDIO_SAMPLE_RATE = 16000


def plan_segments(
    clips: list[MergedClip],
    wall_start: datetime,
    duration: float,
    sync_offset: float,
) -> list[Segment]:
    """Build one or more segments when duration crosses chunk boundaries."""
    moment = wall_start + timedelta(seconds=sync_offset)
    wall_end = wall_start + timedelta(seconds=duration)
    segments: list[Segment] = []

    while moment < wall_end - timedelta(seconds=0.01):
        moment = resolve_moment(clips, moment)
        if moment >= wall_end:
            break
        clip, offset = find_clip_at(clips, moment)
        clip_duration = (
            clip.duration if clip.duration is not None else probe_duration(clip.path)
        )
        clip_end = clip.start + timedelta(seconds=clip_duration)
        segment_end = min(wall_end, clip_end)
        take = (segment_end - moment).total_seconds()
        if take <= 0.01:
            moment = clip_end
            continue
        segments.append(Segment(path=clip.path, ss=offset, duration=take))
        moment = segment_end

    if not segments:
        raise ValueError(f"No merged clip covers {wall_start:%Y-%m-%d %H:%M:%S}")

    return segments


def extract_audio_wav(
    path: Path,
    ss: float,
    duration: float,
    out: Path,
    *,
    sample_rate: int = AUDIO_SAMPLE_RATE,
) -> bool:
    result = subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-ss",
            f"{ss:.3f}",
            "-t",
            f"{duration:.3f}",
            "-i",
            str(path),
            "-vn",
            "-ac",
            "1",
            "-ar",
            str(sample_rate),
            str(out),
        ],
        capture_output=True,
    )
    return result.returncode == 0 and out.is_file() and out.stat().st_size > 1000


def load_wav_mono(path: Path) -> tuple[list[float], int]:
    with wave.open(str(path)) as handle:
        sample_rate = handle.getframerate()
        raw = handle.readframes(handle.getnframes())
    import numpy as np

    samples = np.frombuffer(raw, dtype=np.int16).astype(np.float32)
    return samples, sample_rate


def _bandpass(samples, sample_rate: int, lo: float, hi: float):
    import numpy as np
    from scipy.signal import butter, sosfiltfilt

    sos = butter(4, [lo, hi], btype="band", fs=sample_rate, output="sos")
    return sosfiltfilt(sos, samples)


def _xcorr_lag(a, b, sample_rate: int, *, max_seconds: float = 3.0) -> tuple[float, float]:
    import numpy as np
    from scipy.signal import correlate, correlation_lags

    a = a - a.mean()
    b = b - b.mean()
    norm_a = np.linalg.norm(a)
    norm_b = np.linalg.norm(b)
    if norm_a < 1e-6 or norm_b < 1e-6:
        return 0.0, 0.0
    a = a / norm_a
    b = b / norm_b
    max_lag = int(max_seconds * sample_rate)
    corr = correlate(a, b, mode="full", method="fft")
    lags = correlation_lags(len(a), len(b), mode="full")
    mask = (lags >= -max_lag) & (lags <= max_lag)
    best_lag = int(lags[mask][np.argmax(corr[mask])])
    peak = float(np.max(corr[mask]))
    return best_lag / sample_rate, peak


def _envelope_corr(a, b, sample_rate: int, *, max_seconds: float = 3.0) -> tuple[float, float]:
    import numpy as np

    step = max(sample_rate // 10, 1)
    env_a = np.abs(a)
    env_b = np.abs(b)
    env_a = env_a[: len(env_a) // step * step].reshape(-1, step).mean(axis=1)
    env_b = env_b[: len(env_b) // step * step].reshape(-1, step).mean(axis=1)
    env_rate = sample_rate / step
    return _xcorr_lag(env_a, env_b, int(env_rate), max_seconds=max_seconds)


def choose_audio_mode(envelope_corr: float) -> str:
    if envelope_corr >= ENVELOPE_MIX_THRESHOLD:
        return "mix"
    if envelope_corr >= ENVELOPE_FRONT_THRESHOLD:
        return "front"
    return "screen"


def analyze_audio_sync(
    screen: Path,
    front_segments: list[Segment],
    *,
    from_offset: float,
    compose_duration: float,
    sample_offset: float | None = None,
    sample_duration: float = AUDIO_ANALYZE_SEC,
) -> AudioAnalysis:
    """Compare screen system audio with front cabin mic; pick mode and offset."""
    fallback = AudioAnalysis(
        mode="screen",
        offset_front=0.0,
        envelope_corr=0.0,
        waveform_corr=0.0,
        music_rms_screen=0.0,
        music_rms_front=0.0,
    )
    if not front_segments:
        return fallback

    analyze_at = sample_offset
    if analyze_at is None:
        analyze_at = min(30.0, max(5.0, compose_duration - sample_duration - 2.0))
    if analyze_at + sample_duration > compose_duration:
        sample_duration = max(4.0, compose_duration - analyze_at - 0.5)
    if sample_duration < 4.0:
        return fallback

    front_seg = front_segments[0]
    front_ss = front_seg.ss + analyze_at
    if front_ss + sample_duration > front_seg.ss + front_seg.duration + 0.5:
        return fallback

    screen_ss = from_offset + analyze_at

    try:
        import numpy as np
    except ImportError:
        log("Audio analyze: numpy not installed, using screen audio only")
        return fallback

    try:
        from scipy.signal import butter  # noqa: F401
    except ImportError:
        log("Audio analyze: scipy not installed, using screen audio only")
        return fallback

    with tempfile.TemporaryDirectory(prefix="compose_audio_") as tmp:
        tmp_path = Path(tmp)
        screen_wav = tmp_path / "screen.wav"
        front_wav = tmp_path / "front.wav"
        if not extract_audio_wav(screen, screen_ss, sample_duration, screen_wav):
            log("Audio analyze: could not extract screen audio, using screen mode")
            return fallback
        if not extract_audio_wav(front_seg.path, front_ss, sample_duration, front_wav):
            log("Audio analyze: could not extract front audio, using screen mode")
            return fallback

        screen_samples, sample_rate = load_wav_mono(screen_wav)
        front_samples, _ = load_wav_mono(front_wav)

        music_screen = _bandpass(screen_samples, sample_rate, 300, 3000)
        music_front = _bandpass(front_samples, sample_rate, 300, 3000)
        rms_screen = float(np.sqrt(np.mean(music_screen**2)))
        rms_front = float(np.sqrt(np.mean(music_front**2)))

        waveform_lag, waveform_corr = _xcorr_lag(
            screen_samples, front_samples, sample_rate
        )
        envelope_lag, envelope_corr = _envelope_corr(
            music_screen, music_front, sample_rate
        )

    mode = choose_audio_mode(envelope_corr)
    # Positive lag: front is ahead of screen → delay front to align.
    offset_front = round(max(-3.0, min(3.0, envelope_lag)), 2)
    if mode == "screen":
        offset_front = 0.0

    return AudioAnalysis(
        mode=mode,
        offset_front=offset_front,
        envelope_corr=envelope_corr,
        waveform_corr=waveform_corr,
        music_rms_screen=rms_screen,
        music_rms_front=rms_front,
    )


def resolve_audio_settings(
    *,
    audio: str,
    audio_offset: float | None,
    no_audio_analyze: bool,
    screen: Path,
    front_segments: list[Segment],
    from_offset: float,
    duration: float,
) -> tuple[str, float, AudioAnalysis | None]:
    if audio != "auto" and no_audio_analyze and audio_offset is not None:
        return audio, audio_offset, None

    analysis: AudioAnalysis | None = None
    if not no_audio_analyze:
        log("Audio analyze: extracting samples and comparing music-band envelope...")
        analysis = analyze_audio_sync(
            screen,
            front_segments,
            from_offset=from_offset,
            compose_duration=duration,
        )
        log(
            f"Audio analyze: envelope_corr={analysis.envelope_corr:.3f} "
            f"waveform_corr={analysis.waveform_corr:.3f} "
            f"RMS screen={analysis.music_rms_screen:.0f} front={analysis.music_rms_front:.0f}"
        )

    if audio == "auto":
        mode = analysis.mode if analysis else "screen"
    else:
        mode = audio

    if audio_offset is not None:
        offset = audio_offset
    elif analysis and mode in ("front", "mix"):
        offset = analysis.offset_front
    else:
        offset = 0.0

    if analysis:
        if audio == "auto":
            log(f"Audio:         {mode} (auto, offset_front={offset:+.2f}s)")
        else:
            log(f"Audio:         {mode} (manual, offset_front={offset:+.2f}s)")
    else:
        log(f"Audio:         {mode} (offset_front={offset:+.2f}s)")

    return mode, offset, analysis


def build_audio_filter(
    front_base: int,
    front_count: int,
    mode: str,
    offset_front: float,
) -> tuple[str, str | None]:
    if mode == "screen":
        return "", None

    parts: list[str] = []
    if front_count == 1:
        parts.append(
            f"[{front_base}:a]aresample=44100,"
            f"aformat=sample_fmts=fltp:channel_layouts=stereo[fa0]"
        )
    else:
        labels = "".join(f"[{front_base + i}:a]" for i in range(front_count))
        parts.append(f"{labels}concat=n={front_count}:v=0:a=1[fac]")
        parts.append(
            "[fac]aresample=44100,aformat=sample_fmts=fltp:channel_layouts=stereo[fa0]"
        )

    chain = "[fa0]"
    if offset_front > 0.01:
        delay_ms = int(round(offset_front * 1000))
        parts.append(f"{chain}adelay={delay_ms}|{delay_ms}[fa1]")
        chain = "[fa1]"
    elif offset_front < -0.01:
        parts.append(
            f"{chain}atrim=start={abs(offset_front):.3f},asetpts=PTS-STARTPTS[fa1]"
        )
        chain = "[fa1]"

    if mode == "front":
        parts.append(f"{chain}asetpts=PTS-STARTPTS[aout]")
    else:
        parts.append(
            f"[0:a]{chain}amix=inputs=2:duration=first:dropout_transition=0:"
            f"weights=1 0.65[aout]"
        )

    return ";".join(parts), "[aout]"


def build_filter(
    width: int,
    screen_inputs: int,
    front_inputs: int,
    back_inputs: int,
    *,
    use_vt_scale: bool = False,
) -> str:
    parts: list[str] = []
    idx = 0

    def scale_chain(label_in: str, label_out: str) -> None:
        if use_vt_scale:
            parts.append(
                f"[{label_in}:v]scale_vt=w={width},"
                f"hwdownload,format=nv12,setsar=1[{label_out}]"
            )
        else:
            parts.append(f"[{label_in}:v]scale={width}:-2,setsar=1[{label_out}]")

    if screen_inputs == 1:
        scale_chain("0", "v0")
        screen_label = "v0"
    else:
        labels = []
        for i in range(screen_inputs):
            out = f"s{i}"
            scale_chain(str(i), out)
            labels.append(f"[{out}]")
        parts.append("".join(labels) + f"concat=n={screen_inputs}:v=1:a=0[v0]")
        screen_label = "v0"

    front_base = screen_inputs
    if front_inputs == 1:
        scale_chain(str(front_base), "v1")
        front_label = "v1"
    else:
        labels = []
        for i in range(front_inputs):
            inp = front_base + i
            out = f"f{i}"
            scale_chain(str(inp), out)
            labels.append(f"[{out}]")
        parts.append("".join(labels) + f"concat=n={front_inputs}:v=1:a=0[v1]")
        front_label = "v1"

    back_base = front_base + front_inputs
    if back_inputs == 1:
        scale_chain(str(back_base), "v2")
        back_label = "v2"
    else:
        labels = []
        for i in range(back_inputs):
            inp = back_base + i
            out = f"b{i}"
            scale_chain(str(inp), out)
            labels.append(f"[{out}]")
        parts.append("".join(labels) + f"concat=n={back_inputs}:v=1:a=0[v2]")
        back_label = "v2"

    parts.append(f"[{screen_label}][{front_label}][{back_label}]vstack=inputs=3[vout]")
    return ";".join(parts)


_HEVC_VT_AVAILABLE: bool | None = None


def hevc_encoder_available() -> bool:
    """Probe hevc_videotoolbox with a tiny encode (cached per process)."""
    global _HEVC_VT_AVAILABLE
    if _HEVC_VT_AVAILABLE is None:
        result = subprocess.run(
            [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-f",
                "lavfi",
                "-i",
                "color=size=320x240:rate=25:duration=0.2",
                "-c:v",
                "hevc_videotoolbox",
                "-b:v",
                "1000k",
                "-allow_sw",
                "1",
                "-f",
                "null",
                "-",
            ],
            capture_output=True,
        )
        _HEVC_VT_AVAILABLE = result.returncode == 0
    return _HEVC_VT_AVAILABLE


def resolve_codec(codec: str, hw_quality: int, *, hw: bool) -> tuple[str, int]:
    """Fall back to h264 (at full bitrate) when hevc_videotoolbox is unavailable."""
    if not hw or codec != "hevc":
        return codec, hw_quality
    if hevc_encoder_available():
        return codec, hw_quality
    log("HEVC encoder unavailable on this Mac — falling back to h264_videotoolbox")
    return "h264", max(hw_quality, 65)


def append_hwaccel_args(cmd: list[str], *, keep_hw_frames: bool = True) -> None:
    cmd.extend(["-hwaccel", "videotoolbox"])
    if keep_hw_frames:
        # Frames stay in GPU memory — only usable with scale_vt.
        cmd.extend(["-hwaccel_output_format", "videotoolbox_vld"])


def append_video_encode_args(
    cmd: list[str],
    *,
    hw: bool,
    crf: int,
    preset: str,
    hw_quality: int,
    fps: int,
    codec: str = "h264",
) -> None:
    cmd.extend(["-r", str(fps)])
    if hw:
        bitrate_k = max(hw_quality, 1) * 100
        encoder = "hevc_videotoolbox" if codec == "hevc" else "h264_videotoolbox"
        cmd.extend(
            [
                "-c:v",
                encoder,
                "-b:v",
                f"{bitrate_k}k",
                "-maxrate",
                f"{bitrate_k}k",
                "-bufsize",
                f"{bitrate_k * 2}k",
                "-allow_sw",
                "1",
                "-prio_speed",
                "1",
                "-pix_fmt",
                "yuv420p",
            ]
        )
        if codec == "hevc":
            # hvc1 tag required for QuickTime/YouTube compatibility.
            cmd.extend(["-tag:v", "hvc1"])
    else:
        cmd.extend(
            [
                "-c:v",
                "libx264",
                "-crf",
                str(crf),
                "-preset",
                preset,
            ]
        )
    cmd.extend(["-c:a", "aac", "-b:a", "128k"])


def build_compose_cmd(
    screen: Path,
    front_segments: list[Segment],
    back_segments: list[Segment],
    output: Path,
    *,
    from_offset: float,
    duration: float,
    width: int,
    crf: int,
    preset: str,
    fps: int,
    hw: bool,
    hw_quality: int,
    hw_decode: bool,
    use_vt_scale: bool,
    codec: str = "h264",
    audio_mode: str = "screen",
    audio_offset_front: float = 0.0,
) -> list[str]:
    cmd: list[str] = ["ffmpeg", "-y"]

    if hw_decode:
        append_hwaccel_args(cmd, keep_hw_frames=use_vt_scale)
    cmd.extend(["-ss", str(from_offset), "-t", str(duration), "-i", str(screen)])

    for seg in front_segments:
        if hw_decode:
            append_hwaccel_args(cmd, keep_hw_frames=use_vt_scale)
        cmd.extend(["-ss", f"{seg.ss:.3f}", "-t", f"{seg.duration:.3f}", "-i", str(seg.path)])
    for seg in back_segments:
        if hw_decode:
            append_hwaccel_args(cmd, keep_hw_frames=use_vt_scale)
        cmd.extend(["-ss", f"{seg.ss:.3f}", "-t", f"{seg.duration:.3f}", "-i", str(seg.path)])

    filter_complex = build_filter(
        width,
        screen_inputs=1,
        front_inputs=len(front_segments),
        back_inputs=len(back_segments),
        use_vt_scale=use_vt_scale,
    )
    front_base = 1
    audio_filter, audio_map = build_audio_filter(
        front_base,
        len(front_segments),
        audio_mode,
        audio_offset_front,
    )
    if audio_filter:
        filter_complex = f"{filter_complex};{audio_filter}"
    cmd.extend(["-filter_complex", filter_complex])
    cmd.extend(["-map", "[vout]"])
    if audio_map:
        cmd.extend(["-map", audio_map])
    else:
        cmd.extend(["-map", "0:a?"])
    append_video_encode_args(
        cmd,
        hw=hw,
        crf=crf,
        preset=preset,
        hw_quality=hw_quality,
        fps=fps,
        codec=codec,
    )
    cmd.append(str(output))
    return cmd


def describe_pipeline(*, hw: bool, hw_decode: bool, use_vt_scale: bool) -> str:
    if not hw:
        return "software (libx264)"
    if hw_decode and use_vt_scale:
        return "full VT (hw decode + scale_vt + hw encode)"
    if hw_decode:
        return "hw decode + CPU scale + hw encode"
    return "hw encode only (CPU decode/scale)"


def run_compose(
    screen: Path,
    video_dir: Path,
    output: Path,
    *,
    from_offset: float,
    duration: float,
    sync_offset_front: float,
    sync_offset_back: float,
    width: int,
    crf: int,
    preset: str,
    fps: int,
    hw: bool,
    hw_quality: int,
    hw_decode: bool,
    use_vt_scale: bool,
    codec: str = "h264",
    audio: str,
    audio_offset: float | None,
    no_audio_analyze: bool,
    dry_run: bool,
) -> None:
    codec, hw_quality = resolve_codec(codec, hw_quality, hw=hw)
    screen_start = parse_screen_start(screen)
    wall_start = screen_start + timedelta(seconds=from_offset)
    wall_end = wall_start + timedelta(seconds=duration)

    front_clips = scan_merged_clips(video_dir, "Front")
    back_clips = scan_merged_clips(video_dir, "Back")
    if not front_clips:
        raise SystemExit(f"No Front merged clips in {video_dir / 'Normal' / 'Front'}")
    if not back_clips:
        raise SystemExit(f"No Back merged clips in {video_dir / 'Normal' / 'Back'}")

    front_segments = plan_segments(
        front_clips, wall_start, duration, sync_offset_front
    )
    back_segments = plan_segments(
        back_clips, wall_start, duration, sync_offset_back
    )

    log(f"Screen start:  {screen_start:%Y-%m-%d %H:%M:%S}")
    log(f"Wall range:    {wall_start:%Y-%m-%d %H:%M:%S} -> {wall_end:%Y-%m-%d %H:%M:%S}")
    log(f"Duration:      {duration:g} sec")
    log(f"Front offset:  {sync_offset_front:+g} sec")
    log(f"Back offset:   {sync_offset_back:+g} sec")
    encoder = (
        f"{'hevc' if codec == 'hevc' else 'h264'}_videotoolbox ({hw_quality * 100}k)"
        if hw
        else f"libx264 (crf {crf}, {preset})"
    )
    log(f"Encoder:       {encoder}")
    log(f"Pipeline:      {describe_pipeline(hw=hw, hw_decode=hw_decode, use_vt_scale=use_vt_scale)}")
    log("")
    log("Front segments:")
    for seg in front_segments:
        log(f"  ss={seg.ss:.1f} t={seg.duration:.1f}  {seg.path.name}")
    log("Back segments:")
    for seg in back_segments:
        log(f"  ss={seg.ss:.1f} t={seg.duration:.1f}  {seg.path.name}")

    audio_mode, audio_offset_front, _analysis = resolve_audio_settings(
        audio=audio,
        audio_offset=audio_offset,
        no_audio_analyze=no_audio_analyze,
        screen=screen,
        front_segments=front_segments,
        from_offset=from_offset,
        duration=duration,
    )
    log("")

    common = dict(
        from_offset=from_offset,
        duration=duration,
        width=width,
        crf=crf,
        preset=preset,
        fps=fps,
        hw=hw,
        hw_quality=hw_quality,
        codec=codec,
    )

    if hw and hw_decode:
        # Fastest pipeline first, degrade gracefully on failure.
        attempts: list[tuple[bool, bool, str]] = []
        if use_vt_scale:
            attempts.append((True, True, "full VT (hw decode + scale_vt)"))
        attempts.append((True, False, "hw decode + CPU scale"))
        attempts.append((False, False, "hw encode only"))
    elif hw:
        attempts = [(False, False, "hw encode only")]
    else:
        attempts = [(False, False, "software encode")]

    last_error: subprocess.CalledProcessError | None = None

    for attempt_hw_decode, attempt_vt_scale, label in attempts:
        cmd = build_compose_cmd(
            screen,
            front_segments,
            back_segments,
            output,
            hw_decode=attempt_hw_decode,
            use_vt_scale=attempt_vt_scale,
            audio_mode=audio_mode,
            audio_offset_front=audio_offset_front,
            **common,
        )

        log("")
        log(f"Attempt: {label}")
        log("Command:")
        log(" ".join(f'"{a}"' if " " in a else a for a in cmd))

        if dry_run:
            return

        output.parent.mkdir(parents=True, exist_ok=True)
        try:
            run_ffmpeg_with_progress(cmd, duration_sec=duration)
            if label != attempts[0][2]:
                log(f"\nNote: fell back to {label}")
            log(f"\nDone: {output}")
            return
        except subprocess.CalledProcessError as exc:
            last_error = exc
            if output.is_file():
                output.unlink()
            if label != attempts[-1][2]:
                log(f"\n{label} failed (exit {exc.returncode}), trying fallback...")
            else:
                raise

    if last_error is not None:
        raise last_error


def apply_profile(args: argparse.Namespace) -> None:
    if not args.profile:
        return
    profile = PROFILES[args.profile]
    args.hw = bool(profile["hw"])
    args.hw_quality = int(profile["hw_quality"])
    args.width = int(profile["width"])
    args.fps = int(profile["fps"])
    args.hw_decode = bool(profile["hw_decode"])
    args.use_vt_scale = bool(profile["use_vt_scale"])
    args.codec = str(profile.get("codec", "h264"))


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Vertical compose: Screen Recording (top) + Front + Back"
    )
    parser.add_argument(
        "screen",
        type=Path,
        help="Screen recording MP4 (sync reference)",
    )
    parser.add_argument(
        "--video-dir",
        type=Path,
        default=Path("video/Output"),
        help="Directory with Normal/Front and Normal/Back merged clips",
    )
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        help="Output MP4 path (default: video/Output/<name>)",
    )
    parser.add_argument(
        "--from-offset",
        type=float,
        default=0.0,
        help="Start offset in seconds from screen recording start (default: 0)",
    )
    parser.add_argument(
        "-d",
        "--duration",
        type=float,
        default=DEFAULT_DURATION,
        help=f"Output duration in seconds (default: {DEFAULT_DURATION:g} = 10 min)",
    )
    parser.add_argument(
        "--sync-offset-front",
        type=float,
        default=0.0,
        help="Fine-tune Front sync in seconds relative to wall clock",
    )
    parser.add_argument(
        "--sync-offset-back",
        type=float,
        default=0.0,
        help="Fine-tune Back sync in seconds relative to wall clock",
    )
    parser.add_argument("--width", type=int, default=1206, help="Output width in px")
    parser.add_argument("--crf", type=int, default=20, help="x264 CRF quality")
    parser.add_argument("--preset", default="medium", help="x264 preset")
    parser.add_argument("--fps", type=int, default=25, help="Output frame rate")
    parser.add_argument(
        "--hw",
        action="store_true",
        help="Use macOS hardware encoder (h264_videotoolbox) — much faster",
    )
    parser.add_argument(
        "--hw-quality",
        type=int,
        default=65,
        metavar="Q",
        help="VideoToolbox target quality 1–100 → bitrate Q×100k (default: 65 ≈ 6.5 Mbps)",
    )
    parser.add_argument(
        "--profile",
        choices=sorted(PROFILES),
        default=DEFAULT_PROFILE,
        help=f"Encoding profile (default: {DEFAULT_PROFILE} — hw encode, 6.5 Mbps, 1206px, 25fps)",
    )
    parser.add_argument(
        "--hw-decode",
        action="store_true",
        help="Hardware-decode inputs with VideoToolbox (-hwaccel videotoolbox)",
    )
    parser.add_argument(
        "--no-vt-scale",
        action="store_true",
        help="Use CPU scale filter instead of scale_vt (when hw decode is enabled)",
    )
    parser.add_argument(
        "--codec",
        choices=("h264", "hevc"),
        default=None,
        help="HW encoder codec (default: from profile; hevc ≈ half the bitrate)",
    )
    parser.add_argument(
        "--audio",
        choices=("auto", "screen", "front", "mix"),
        default="auto",
        help="Audio source: auto (analyze and pick), screen, front, or mix (default: auto)",
    )
    parser.add_argument(
        "--audio-offset",
        type=float,
        default=None,
        metavar="SEC",
        help="Shift front audio vs screen (+ delays front). Default: from audio analyze",
    )
    parser.add_argument(
        "--no-audio-analyze",
        action="store_true",
        help="Skip audio analysis; use --audio screen and --audio-offset 0",
    )
    parser.add_argument("--dry-run", action="store_true", help="Print plan only")
    args = parser.parse_args()

    args.use_vt_scale = False
    hw_decode_explicit = args.hw_decode
    codec_explicit = args.codec
    apply_profile(args)

    if hw_decode_explicit:
        args.hw_decode = True
        args.use_vt_scale = not args.no_vt_scale
    elif args.no_vt_scale:
        args.use_vt_scale = False
    if codec_explicit:
        args.codec = codec_explicit

    if not args.screen.is_file():
        parser.error(f"Screen recording not found: {args.screen}")

    if args.output:
        output = args.output
    else:
        start = parse_screen_start(args.screen)
        moment = start + timedelta(seconds=args.from_offset)
        end = moment + timedelta(seconds=args.duration)
        output = (
            args.video_dir
            / f"NO_{moment:%Y%m%d-%H%M%S}_{end:%H%M%S}_3cam.mp4"
        )

    try:
        run_compose(
            args.screen,
            args.video_dir,
            output,
            from_offset=args.from_offset,
            duration=args.duration,
            sync_offset_front=args.sync_offset_front,
            sync_offset_back=args.sync_offset_back,
            width=args.width,
            crf=args.crf,
            preset=args.preset,
            fps=args.fps,
            hw=args.hw,
            hw_quality=args.hw_quality,
            hw_decode=args.hw_decode,
            use_vt_scale=args.use_vt_scale,
            codec=args.codec,
            audio=args.audio,
            audio_offset=args.audio_offset,
            no_audio_analyze=args.no_audio_analyze,
            dry_run=args.dry_run,
        )
    except subprocess.CalledProcessError as exc:
        raise SystemExit(exc.returncode) from exc


if __name__ == "__main__":
    from project_env import ensure_venv_python

    ensure_venv_python()
    main()
