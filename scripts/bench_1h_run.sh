#!/usr/bin/env bash
# Timed 1h SD → import → compose → YouTube (private) bench for bottleneck hunting.
#
#   ./scripts/bench_1h_run.sh
#   ./scripts/bench_1h_run.sh --dry-run
#   ./scripts/bench_1h_run.sh --from "2026-07-18 11:12" --to "2026-07-18 12:12"
#   BENCH_FROM=... BENCH_TO=... ./scripts/bench_1h_run.sh
#
# Parallel dashboard: ./scripts/bench_1h_dashboard.sh
#
# Artifacts: video/Output/.publish_tmp/bench_1h/{bench.log,timing.jsonl,summary.md,...}

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

SOURCE="${BENCH_SOURCE:-/Volumes/Untitled}"
VIDEO_DIR="${BENCH_VIDEO_DIR:-$ROOT/video/Output}"
TEMP_DIR="${BENCH_TEMP_DIR:-$VIDEO_DIR/.publish_tmp}"
BENCH_DIR="$TEMP_DIR/bench_1h"
LOCK_DIR="$TEMP_DIR/.publish_all.lock"
PROFILE="${BENCH_PROFILE:-balanced}"
TITLE="${BENCH_TITLE:-70mai bench 1h}"
CHUNK_MINUTES=60
DRY_RUN=0
SKIP_IMPORT=0
FROM_ARG="${BENCH_FROM:-}"
TO_ARG="${BENCH_TO:-}"

usage() {
  cat <<'EOF'
Usage: bench_1h_run.sh [options]

  --source PATH       SD mount (default: /Volumes/Untitled)
  --from DATETIME     Window start (default: auto from scan)
  --to DATETIME       Window end exclusive (default: start+60m)
  --profile NAME      Compose profile (default: balanced)
  --skip-import       Compose/upload only (merges already on disk)
  --dry-run           Import --dry-run + publish --estimate-only
  -h, --help          This help

Env: BENCH_SOURCE, BENCH_FROM, BENCH_TO, BENCH_PROFILE, BENCH_TITLE
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --source) SOURCE="$2"; shift 2 ;;
    --from) FROM_ARG="$2"; shift 2 ;;
    --to) TO_ARG="$2"; shift 2 ;;
    --profile) PROFILE="$2"; shift 2 ;;
    --skip-import) SKIP_IMPORT=1; shift ;;
    --dry-run) DRY_RUN=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown option: $1" >&2; usage >&2; exit 2 ;;
  esac
done

mkdir -p "$BENCH_DIR" "$TEMP_DIR"
TIMING_JSONL="$BENCH_DIR/timing.jsonl"
BENCH_LOG="$BENCH_DIR/bench.log"
SUMMARY_MD="$BENCH_DIR/summary.md"
WINDOW_JSON="$BENCH_DIR/window.json"
IMPORT_LOG="$BENCH_DIR/import.log"
PUBLISH_LOG="$BENCH_DIR/publish.log"
: >"$TIMING_JSONL"
: >"$BENCH_LOG"

log() {
  local line
  line="$(date '+%Y-%m-%d %H:%M:%S') $*"
  printf '%s\n' "$line" | tee -a "$BENCH_LOG"
}

json_escape() {
  python3 -c 'import json,sys; print(json.dumps(sys.stdin.read()[:-1] if False else sys.argv[1]))' "$1"
}

dir_bytes() {
  local path="$1"
  if [[ -d "$path" ]]; then
    du -sk "$path" 2>/dev/null | awk '{print $1 * 1024}'
  else
    echo 0
  fi
}

normal_bytes() {
  local f b
  f="$(dir_bytes "$VIDEO_DIR/Normal/Front")"
  b="$(dir_bytes "$VIDEO_DIR/Normal/Back")"
  echo $((f + b))
}

# Returns 0 if autopilot lock is free or stale.
check_autopilot_lock() {
  local pid=""
  if [[ -d "$LOCK_DIR" ]]; then
    pid="$(tr -d '[:space:]' <"$LOCK_DIR/pid" 2>/dev/null || true)"
  elif [[ -f "$LOCK_DIR" ]]; then
    pid="$(tr -d '[:space:]' <"$LOCK_DIR" 2>/dev/null || true)"
  else
    return 0
  fi
  if [[ -z "$pid" ]]; then
    return 0
  fi
  if kill -0 "$pid" 2>/dev/null; then
    log "ERROR: autopilot lock held by live pid $pid ($LOCK_DIR)"
    log "  Stop it or: ./scripts/publish_all_70mai.sh --force-restart --wait"
    return 1
  fi
  log "Note: stale autopilot lock pid $pid (not running) — continuing"
  return 0
}

PHASE=""
PHASE_T0=0
PHASE_BYTES0=0

bench_phase_start() {
  PHASE="$1"
  PHASE_T0="$(date +%s)"
  PHASE_BYTES0="$(normal_bytes)"
  log "[BENCH] phase=$PHASE start bytes_normal=$PHASE_BYTES0"
}

bench_phase_end() {
  local note="${1:-}"
  local t1 elapsed bytes1 delta msg
  t1="$(date +%s)"
  elapsed=$((t1 - PHASE_T0))
  [[ "$elapsed" -lt 1 ]] && elapsed=1
  bytes1="$(normal_bytes)"
  delta=$((bytes1 - PHASE_BYTES0))
  if [[ "$delta" -lt 0 ]]; then
    delta=0
  fi
  msg="$(
    PHASE="$PHASE" T0="$PHASE_T0" T1="$t1" ELAPSED="$elapsed" \
      BIN="$PHASE_BYTES0" BOUT="$bytes1" DELTA="$delta" NOTE="$note" \
      TIMING_JSONL="$TIMING_JSONL" python3 - <<'PY'
import json, os
from pathlib import Path
elapsed = int(os.environ["ELAPSED"])
delta = int(os.environ["DELTA"])
row = {
    "phase": os.environ["PHASE"],
    "t0": int(os.environ["T0"]),
    "t1": int(os.environ["T1"]),
    "elapsed_sec": elapsed,
    "bytes_in": int(os.environ["BIN"]),
    "bytes_out": int(os.environ["BOUT"]),
    "bytes_delta": delta,
    "mb_s": round(delta / 1048576 / elapsed, 3),
    "note": os.environ.get("NOTE", ""),
}
path = Path(os.environ["TIMING_JSONL"])
with path.open("a", encoding="utf-8") as f:
    f.write(json.dumps(row, ensure_ascii=False) + "\n")
print(
    f"phase={row['phase']} end elapsed={elapsed}s "
    f"delta_MB={round(delta/1048576,1)} mb_s={row['mb_s']} note={row['note']}"
)
PY
  )"
  log "[BENCH] $msg"
}

append_timing_extra() {
  # Extra phase row (compose/upload subnotes) without re-measuring Normal dirs.
  local phase="$1" elapsed="$2" bytes_delta="$3" mb_s="$4" note="$5"
  local t1
  t1="$(date +%s)"
  printf '{"phase":"%s","t0":%s,"t1":%s,"elapsed_sec":%s,"bytes_in":0,"bytes_out":0,"bytes_delta":%s,"mb_s":%s,"note":%s}\n' \
    "$phase" "$((t1 - elapsed))" "$t1" "$elapsed" "$bytes_delta" "$mb_s" "$(json_escape "$note")" \
    >>"$TIMING_JSONL"
}

pick_window() {
  if [[ -n "$FROM_ARG" && -n "$TO_ARG" ]]; then
    printf '%s\n%s\n' "$FROM_ARG" "$TO_ARG"
    return 0
  fi
  if [[ -n "$FROM_ARG" && -z "$TO_ARG" ]]; then
    TO_ARG="$(python3 -c "
from datetime import datetime, timedelta
s='$FROM_ARG'
for fmt in ('%Y-%m-%d %H:%M:%S','%Y-%m-%d %H:%M'):
  try:
    d=datetime.strptime(s, fmt)
    print((d+timedelta(minutes=60)).strftime('%Y-%m-%d %H:%M'))
    break
  except ValueError:
    pass
else:
  raise SystemExit('bad --from')
")"
    printf '%s\n%s\n' "$FROM_ARG" "$TO_ARG"
    return 0
  fi

  local scan_log="$BENCH_DIR/scan.log"
  log "Scanning SD for Normal sessions ≥60 min…"
  "$ROOT/run" import_70mai.py --scan --source "$SOURCE" >"$scan_log" 2>&1 || true
  tee -a "$BENCH_LOG" <"$scan_log" >/dev/null

  python3 - "$scan_log" <<'PY'
import re, sys
from datetime import datetime, timedelta

text = open(sys.argv[1], encoding="utf-8", errors="replace").read()
# Prefer Normal / Front block
m = re.search(
    r"Normal / Front.*?recording session\(s\):(.*?)(?:\n\nNormal / Back|\n\nEvent |\n=== |\Z)",
    text,
    re.S,
)
block = m.group(1) if m else text
pat = re.compile(
    r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\s*->\s*(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\s*\((\d+) clips\)"
)
best = None
for a, b, clips in pat.findall(block):
    start = datetime.strptime(a, "%Y-%m-%d %H:%M:%S")
    end = datetime.strptime(b, "%Y-%m-%d %H:%M:%S")
    dur = (end - start).total_seconds()
    if dur >= 3600 - 30:  # ~60 min
        to = start + timedelta(minutes=60)
        print(start.strftime("%Y-%m-%d %H:%M"))
        print(to.strftime("%Y-%m-%d %H:%M"))
        sys.exit(0)
    if best is None or dur > best[0]:
        best = (dur, start, end, clips)
if best and best[0] >= 1800:
    # Fallback: longest session ≥30m — still take up to 60m
    start, end = best[1], best[2]
    to = min(end, start + timedelta(minutes=60))
    print(start.strftime("%Y-%m-%d %H:%M"))
    print(to.strftime("%Y-%m-%d %H:%M"))
    sys.exit(0)
print("ERROR: no Normal/Front session ≥30 min on card", file=sys.stderr)
sys.exit(1)
PY
}

write_summary() {
  python3 - "$TIMING_JSONL" "$SUMMARY_MD" "$WINDOW_JSON" "$PUBLISH_LOG" <<'PY'
import json, re, sys
from pathlib import Path

jsonl, out, window_path, publish_log = map(Path, sys.argv[1:5])
rows = []
for line in jsonl.read_text(encoding="utf-8").splitlines():
    line = line.strip()
    if not line:
        continue
    try:
        rows.append(json.loads(line))
    except json.JSONDecodeError:
        pass

window = {}
if window_path.is_file():
    window = json.loads(window_path.read_text(encoding="utf-8"))

yt = []
if publish_log.is_file():
    text = publish_log.read_text(encoding="utf-8", errors="replace")
    yt = re.findall(r"https://youtu\.be/\S+|https://www\.youtube\.com/watch\?v=\S+", text)
    speeds = [float(x) for x in re.findall(r"speed=\s*([\d.]+)x", text)]
    ups = [float(x) for x in re.findall(r"([\d.]+)\s*MB/s", text)]
else:
    speeds, ups = [], []

# Rank bottlenecks by wall time (slowest first among timed phases)
ranked = sorted(
    [r for r in rows if r.get("elapsed_sec")],
    key=lambda r: -float(r["elapsed_sec"]),
)

lines = []
lines.append("# 70mai 1h bench summary")
lines.append("")
if window:
    lines.append(f"- Window: `{window.get('from')}` → `{window.get('to')}`")
    lines.append(f"- Source: `{window.get('source')}`")
    lines.append(f"- Profile: `{window.get('profile')}`")
    lines.append(f"- Dry-run: `{window.get('dry_run')}`")
lines.append("")
lines.append("| Rank | Phase | sec | ΔMB | MB/s | note |")
lines.append("|------|-------|-----|-----|------|------|")
for i, r in enumerate(ranked, 1):
    delta_mb = round(float(r.get("bytes_delta") or 0) / 1048576, 1)
    lines.append(
        f"| {i} | `{r.get('phase')}` | {r.get('elapsed_sec')} | {delta_mb} | {r.get('mb_s')} | {r.get('note') or ''} |"
    )
lines.append("")
if speeds:
    lines.append(f"- Encode speed samples (Nx): min={min(speeds):.2f} max={max(speeds):.2f} avg={sum(speeds)/len(speeds):.2f}")
if ups:
    lines.append(f"- Upload MB/s samples: min={min(ups):.2f} max={max(ups):.2f} avg={sum(ups)/len(ups):.2f}")
if yt:
    lines.append("- YouTube:")
    for u in dict.fromkeys(yt):
        lines.append(f"  - {u}")
lines.append("")
lines.append(f"Timing JSONL: `{jsonl}`")
out.write_text("\n".join(lines) + "\n", encoding="utf-8")
print(out)
PY
}

# --- main ---
check_autopilot_lock || exit 1

log "=== bench_1h start source=$SOURCE profile=$PROFILE dry_run=$DRY_RUN ==="

bench_phase_start "scan_pick_window"
WINDOW_OUT="$(pick_window)"
FROM_TS="$(printf '%s\n' "$WINDOW_OUT" | sed -n '1p')"
TO_TS="$(printf '%s\n' "$WINDOW_OUT" | sed -n '2p')"
if [[ -z "$FROM_TS" || -z "$TO_TS" ]]; then
  log "ERROR: could not determine window"
  exit 1
fi
python3 -c "
import json
from pathlib import Path
Path('$WINDOW_JSON').write_text(json.dumps({
  'from': '$FROM_TS',
  'to': '$TO_TS',
  'source': '$SOURCE',
  'profile': '$PROFILE',
  'dry_run': bool($DRY_RUN),
  'skip_import': bool($SKIP_IMPORT),
  'title': '$TITLE',
}, indent=2), encoding='utf-8')
"
bench_phase_end "from=$FROM_TS to=$TO_TS"
log "Window: $FROM_TS → $TO_TS"

if [[ "$SKIP_IMPORT" -eq 0 ]]; then
  bench_phase_start "import"
  IMPORT_ARGS=(
    import_70mai.py
    --source "$SOURCE"
    --output "$VIDEO_DIR"
    --from "$FROM_TS"
    --to "$TO_TS"
    --types Normal
    --status-dir "$TEMP_DIR"
  )
  if [[ "$DRY_RUN" -eq 1 ]]; then
    IMPORT_ARGS+=(--dry-run)
  fi
  set +e
  "$ROOT/run" "${IMPORT_ARGS[@]}" 2>&1 | tee "$IMPORT_LOG" | tee -a "$BENCH_LOG"
  import_rc=${PIPESTATUS[0]}
  set -e
  done_line="$(rg -n 'Done in' "$IMPORT_LOG" | tail -1 || true)"
  bench_phase_end "rc=$import_rc $done_line"
  if [[ "$import_rc" -ne 0 && "$DRY_RUN" -eq 0 ]]; then
    log "ERROR: import failed rc=$import_rc"
    write_summary
    exit "$import_rc"
  fi
else
  log "Skipping import (--skip-import)"
fi

COMPOSE_OUT="$BENCH_DIR/bench_1h.mp4"
COMPOSE_LOG="$BENCH_DIR/compose.log"
UPLOAD_LOG="$BENCH_DIR/upload.log"
# Keep PUBLISH_LOG as combined compose+upload for summary parser
: >"$PUBLISH_LOG"

bench_phase_start "compose"
COMPOSE_ARGS=(
  compose_2cam_70mai.py
  --from "$FROM_TS"
  --to "$TO_TS"
  --video-dir "$VIDEO_DIR"
  -o "$COMPOSE_OUT"
  --profile "$PROFILE"
)
if [[ "$DRY_RUN" -eq 1 ]]; then
  COMPOSE_ARGS+=(--dry-run)
fi
set +e
"$ROOT/run" "${COMPOSE_ARGS[@]}" 2>&1 | tee "$COMPOSE_LOG" | tee -a "$BENCH_LOG" | tee -a "$PUBLISH_LOG"
compose_rc=${PIPESTATUS[0]}
set -e
compose_sz=0
max_nx=""
if [[ -f "$COMPOSE_OUT" ]]; then
  compose_sz="$(stat -f%z "$COMPOSE_OUT" 2>/dev/null || stat -c%s "$COMPOSE_OUT" 2>/dev/null || echo 0)"
fi
max_nx="$(rg -o 'speed=\s*[\d.]+x|speed ([0-9.]+)x' "$COMPOSE_LOG" 2>/dev/null | rg -o '[0-9.]+' | sort -n | tail -1 || true)"
bench_phase_end "rc=$compose_rc bytes=$compose_sz max_encode_Nx=${max_nx:-?}"
if [[ -n "$max_nx" ]]; then
  append_timing_extra "compose_encode_hint" 0 "$compose_sz" 0 "max_speed_Nx=$max_nx"
fi
if [[ "$compose_rc" -ne 0 && "$DRY_RUN" -eq 0 ]]; then
  log "ERROR: compose failed rc=$compose_rc"
  write_summary
  exit "$compose_rc"
fi
if [[ "$compose_rc" -ne 0 && "$DRY_RUN" -eq 1 ]]; then
  log "Dry-run: compose not runnable without merges (expected) — continuing"
  compose_rc=0
fi

upload_rc=0
if [[ "$DRY_RUN" -eq 1 ]]; then
  log "Dry-run: skip YouTube upload (would upload $COMPOSE_OUT as private)"
  append_timing_extra "upload" 0 0 0 "skipped_dry_run"
else
  if [[ ! -f "$COMPOSE_OUT" ]]; then
    log "ERROR: compose output missing: $COMPOSE_OUT"
    write_summary
    exit 1
  fi
  bench_phase_start "upload"
  set +e
  "$ROOT/run" youtube_upload.py \
    "$COMPOSE_OUT" \
    --title "$TITLE" \
    --description "70mai 1h bench $FROM_TS → $TO_TS" \
    --privacy private \
    --resume-upload \
    --diag-log "$BENCH_DIR/youtube_upload.diag.jsonl" \
    2>&1 | tee "$UPLOAD_LOG" | tee -a "$BENCH_LOG" | tee -a "$PUBLISH_LOG"
  upload_rc=${PIPESTATUS[0]}
  set -e
  max_ups="$(rg -o '[0-9.]+ MB/s' "$UPLOAD_LOG" 2>/dev/null | rg -o '^[0-9.]+' | sort -n | tail -1 || true)"
  bench_phase_end "rc=$upload_rc bytes=$compose_sz max_upload_MBs=${max_ups:-?}"
  if [[ -n "$max_ups" ]]; then
    append_timing_extra "upload_hint" 0 "$compose_sz" "$max_ups" "peak_MB_s=$max_ups"
  fi
fi

SUMMARY_PATH="$(write_summary)"
log "=== bench_1h done compose_rc=$compose_rc upload_rc=$upload_rc ==="
log "Summary: $SUMMARY_PATH"
cat "$SUMMARY_MD"
if [[ "$upload_rc" -ne 0 ]]; then
  exit "$upload_rc"
fi
exit "$compose_rc"
