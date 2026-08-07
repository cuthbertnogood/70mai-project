# Bottleneck report — autopilot
**Обновлено:** 2026-08-07 23:08:41

## Live monitor (`perf_monitor.jsonl`)
- Снимков: **7**
- Текущая фаза: **upload** — 7% (54M) 0.56x
- Тип: Normal chunk 1
- Copy (сек/клип, tail avg): avg **0.0** (min 0.0, max 0.0, n=7)
- Merge (MB/s, tail avg): avg **222.08** (min 220.58, max 231.07, n=7)
- Encode (Nx realtime): avg **0.59** (min 0.57, max 0.62, n=5)

### Timeline (последние переходы)
- `2026-08-07T23:05:20` **import** 43/44 · NO_20260729-224514_225314_B.mp4 (98%)
- `2026-08-07T23:05:50` **upload** 0% (2M)
- `2026-08-07T23:06:20` **compose** 1% (14M) 0.60x (1%)
- `2026-08-07T23:06:50` **compose** 2% (20M) 0.55x (2%)
- `2026-08-07T23:07:20` **upload** 3% (32M) 0.56x (3%)
- `2026-08-07T23:07:50` **upload** 5% (44M) 0.58x (5%)
- `2026-08-07T23:08:20` **upload** 7% (54M) 0.56x (7%)

## Bench 1h: balanced vs hevc

| Phase | balanced (s) | hevc (s) | Δ | hevc note |
|-------|-------------|----------|---|-----------|
| compose | — | — | — | — |
| upload | — | — | — | — |

## Выводы (авто)

- **HEVC:** `hevc_videotoolbox` недоступен — профиль `hevc` → H.264 fallback; для upload используй `youtube` (3 Mbps) или `balanced` (5 Mbps)

## Артефакты
- Monitor: `/Users/cuthbert/work_local/70mai_project/video/Output/.publish_tmp/perf_monitor.jsonl`
- Balanced bench: `/Users/cuthbert/work_local/70mai_project/video/Output/.publish_tmp/bench_1h/timing.jsonl`
- HEVC bench: `/Users/cuthbert/work_local/70mai_project/video/Output/.publish_tmp/bench_1h_hevc/timing.jsonl`
- Performance: `анализ/performance_report.md`

Перегенерация: `python3 анализ/generate_bottleneck_report.py`
