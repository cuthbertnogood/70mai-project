#!/usr/bin/env bash
# Compose+upload 1h bench with hevc — no autopilot lock check (parallel with import).
# Uses same window as bench_1h/balanced baseline when window.json exists.

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

VIDEO_DIR="${BENCH_VIDEO_DIR:-$ROOT/video/Output}"
TEMP_DIR="${BENCH_TEMP_DIR:-$VIDEO_DIR/.publish_tmp}"
BENCH_DIR="$TEMP_DIR/bench_1h_hevc"
WINDOW_JSON="$TEMP_DIR/bench_1h/window.json"
PROFILE="${BENCH_PROFILE:-hevc}"
TITLE="${BENCH_TITLE:-70mai bench 1h hevc}"

FROM_ARG="${BENCH_FROM:-}"
TO_ARG="${BENCH_TO:-}"

if [[ -f "$WINDOW_JSON" && -z "$FROM_ARG" ]]; then
  FROM_ARG="$(python3 -c "import json; print(json.load(open('$WINDOW_JSON'))['from'])")"
  TO_ARG="$(python3 -c "import json; print(json.load(open('$WINDOW_JSON'))['to'])")"
fi

if [[ -z "$FROM_ARG" || -z "$TO_ARG" ]]; then
  echo "Need --from/--to or existing $WINDOW_JSON" >&2
  exit 2
fi

mkdir -p "$BENCH_DIR"
TIMING_JSONL="$BENCH_DIR/timing.jsonl"
BENCH_LOG="$BENCH_DIR/bench.log"
COMPOSE_OUT="$BENCH_DIR/bench_1h.mp4"
COMPOSE_LOG="$BENCH_DIR/compose.log"
UPLOAD_LOG="$BENCH_DIR/upload.log"
: >"$BENCH_LOG"

log() {
  printf '%s %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*" | tee -a "$BENCH_LOG"
}

append_timing() {
  local phase="$1" t0="$2" t1="$3" note="$4"
  local elapsed=$((t1 - t0))
  python3 -c "
import json, time
from pathlib import Path
p = Path('$TIMING_JSONL')
row = {'phase': '$phase', 't0': $t0, 't1': $t1, 'elapsed_sec': $elapsed, 'note': '''$note'''}
with p.open('a') as f:
    f.write(json.dumps(row) + '\n')
"
}

log "=== bench_hevc (no lock) profile=$PROFILE window=$FROM_ARG → $TO_ARG ==="

t0=$(date +%s)
log "[BENCH] compose start"
set +e
"$ROOT/run" compose_2cam_70mai.py \
  --from "$FROM_ARG" \
  --to "$TO_ARG" \
  --video-dir "$VIDEO_DIR" \
  -o "$COMPOSE_OUT" \
  --profile "$PROFILE" \
  2>&1 | tee "$COMPOSE_LOG" | tee -a "$BENCH_LOG"
compose_rc=${PIPESTATUS[0]}
set -e
t1=$(date +%s)
compose_sz=0
[[ -f "$COMPOSE_OUT" ]] && compose_sz=$(stat -f%z "$COMPOSE_OUT" 2>/dev/null || stat -c%s "$COMPOSE_OUT")
max_nx=$(rg -o 'speed=\s*[\d.]+x|speed ([0-9.]+)x' "$COMPOSE_LOG" 2>/dev/null | rg -o '[0-9.]+' | sort -n | tail -1 || true)
append_timing "compose" "$t0" "$t1" "rc=$compose_rc bytes=$compose_sz max_encode_Nx=${max_nx:-?}"
log "compose done rc=$compose_rc size=$compose_sz max_Nx=${max_nx:-?}"

if [[ "$compose_rc" -ne 0 ]]; then
  log "ERROR: compose failed"
  exit "$compose_rc"
fi

t0=$(date +%s)
log "[BENCH] upload start"
set +e
"$ROOT/run" youtube_upload.py \
  "$COMPOSE_OUT" \
  --title "$TITLE" \
  --description "70mai 1h bench hevc $FROM_ARG → $TO_ARG" \
  --privacy private \
  --resume-upload \
  --diag-log "$BENCH_DIR/youtube_upload.diag.jsonl" \
  2>&1 | tee "$UPLOAD_LOG" | tee -a "$BENCH_LOG"
upload_rc=${PIPESTATUS[0]}
set -e
t1=$(date +%s)
max_ups=$(rg -o '[0-9.]+ MB/s' "$UPLOAD_LOG" 2>/dev/null | rg -o '^[0-9.]+' | sort -n | tail -1 || true)
append_timing "upload" "$t0" "$t1" "rc=$upload_rc bytes=$compose_sz max_upload_MBs=${max_ups:-?}"
log "upload done rc=$upload_rc max_MB/s=${max_ups:-?}"

# Write summary
python3 <<PY
import json
from pathlib import Path

bench = Path("$BENCH_DIR")
balanced = Path("$TEMP_DIR/bench_1h/timing.jsonl")
rows = []
for line in (bench / "timing.jsonl").read_text().splitlines():
    if line.strip():
        rows.append(json.loads(line))
bal = []
if balanced.is_file():
    for line in balanced.read_text().splitlines():
        if line.strip():
            bal.append(json.loads(line))

def by_phase(rs, ph):
    return next((r for r in rs if r.get("phase") == ph), {})

lines = [
    "# 70mai 1h bench — hevc (parallel run)",
    "",
    f"- Window: \`$FROM_ARG\` → \`$TO_ARG\`",
    f"- Profile: \`$PROFILE\`",
    "",
    "| Phase | hevc sec | balanced sec | Δ |",
    "|-------|----------|--------------|---|",
]
for ph in ("compose", "upload"):
    h = by_phase(rows, ph)
    b = by_phase(bal, ph)
    hs, bs = h.get("elapsed_sec", 0), b.get("elapsed_sec", 0)
    delta = ""
    if bs and hs:
        pct = (hs - bs) / bs * 100
        delta = f"{pct:+.0f}%"
    lines.append(f"| {ph} | {hs} | {bs or '—'} | {delta or '—'} |")

hevc_sz = int("$compose_sz")
bal_note = by_phase(bal, "compose").get("note", "")
import re
bal_m = re.search(r"bytes=(\d+)", bal_note)
if bal_m and hevc_sz:
    bal_sz = int(bal_m.group(1))
    ratio = bal_sz / hevc_sz if hevc_sz else 0
    lines += ["", f"- hevc output: {hevc_sz/1e9:.2f} GB"]
    lines.append(f"- balanced output: {bal_sz/1e9:.2f} GB (from baseline)")
    lines.append(f"- size ratio: {ratio:.2f}x smaller with hevc")

(bench / "summary.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
print(bench / "summary.md")
PY

log "=== bench_hevc done compose_rc=$compose_rc upload_rc=$upload_rc ==="
cat "$BENCH_DIR/summary.md"
exit "${upload_rc:-0}"
