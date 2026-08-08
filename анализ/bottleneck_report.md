# Bottleneck report — autopilot
**Обновлено:** 2026-08-08 03:14:23

## Live monitor (`perf_monitor.jsonl`)
- Снимков: **498**
- Текущая фаза: **upload** — 56% (292M) 1.05x
- Тип: Normal chunk 3
- Copy (сек/клип, tail avg): avg **0.29** (min 0.0, max 2.05, n=150)
- Merge (MB/s, tail avg): avg **193.82** (min 29.0, max 271.5, n=153)
- Encode (Nx realtime): avg **1.11** (min 0.56, max 1.46, n=465)
- Upload (MB/s): avg **2.58** (min 1.1, max 2.73, n=66)

### Timeline (последние переходы)
- `2026-08-08T03:07:04` **compose** 19% (93M) 0.81x (19%)
- `2026-08-08T03:07:34` **upload** 21% (104M) 0.84x (21%)
- `2026-08-08T03:08:04` **upload** 23% (117M) 0.86x (23%)
- `2026-08-08T03:08:34` **upload** 26% (130M) 0.89x (26%)
- `2026-08-08T03:09:04` **upload** 29% (142M) 0.91x (29%)
- `2026-08-08T03:09:34` **upload** 32% (155M) 0.94x (32%)
- `2026-08-08T03:10:04` **upload** 33% (166M) 0.94x (33%)
- `2026-08-08T03:10:34` **upload** 36% (180M) 0.96x (36%)
- `2026-08-08T03:11:04` **upload** 39% (192M) 0.97x (39%)
- `2026-08-08T03:11:34` **upload** 41% (205M) 0.98x (41%)
- `2026-08-08T03:12:04` **upload** 44% (216M) 0.99x (44%)
- `2026-08-08T03:12:34` **upload** 48% (243M) 1.03x (48%)
- `2026-08-08T03:13:04` **compose** 51% (267M) 1.04x (51%)
- `2026-08-08T03:13:34` **compose** 53% (281M) 1.05x (53%)
- `2026-08-08T03:14:04` **upload** 56% (292M) 1.05x (56%)

## Bench 1h: balanced vs hevc

| Phase | balanced (s) | hevc (s) | Δ | hevc note |
|-------|-------------|----------|---|-----------|
| compose | — | 341 | — | rc=247 bytes=0 max_encode_Nx=0.58 |
| upload | — | 435 | — | rc=0 bytes=1405927456 max_upload_MBs=3.6 |

## Выводы (авто)

- **HEVC:** `hevc_videotoolbox` недоступен — профиль `hevc` → H.264 fallback; для upload используй `youtube` (3 Mbps) или `balanced` (5 Mbps)
- **Import:** merge быстрый (~194 MB/s); copy доминирует (~0s/клип)

## Артефакты
- Monitor: `/Users/cuthbert/work_local/70mai_project/video/Output/.publish_tmp/perf_monitor.jsonl`
- Balanced bench: `/Users/cuthbert/work_local/70mai_project/video/Output/.publish_tmp/bench_1h/timing.jsonl`
- HEVC bench: `/Users/cuthbert/work_local/70mai_project/video/Output/.publish_tmp/bench_1h_hevc/timing.jsonl`
- Performance: `анализ/performance_report.md`

Перегенерация: `python3 анализ/generate_bottleneck_report.py`
