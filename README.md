# 70mai — SD → YouTube

Автопилот: карта 70mai → склейка → 2-cam MP4 → YouTube. **Последовательно** по каждому ~2h ролику: import (copy+merge) → compose → upload → следующий ролик.

Детали, OAuth, тюнинг: [детальное_описание.md](детальное_описание.md) · цели: [GOALS.md](GOALS.md).

---

## Один раз

```bash
cd /Users/cuthbert/work_local/70mai_project
scripts/setup-venv.sh
# OAuth: ~/.config/70mai/youtube_credentials.json  (первый upload — вход в браузере)
```

Нужны: Mac, Python 3.10+, ffmpeg, SD-карта 70mai (обычно `/Volumes/Untitled`).

---

## Основные скрипты (только эти)

Все команды — из каталога проекта:

```bash
cd /Users/cuthbert/work_local/70mai_project
```

| Скрипт | Зачем |
|--------|--------|
| `./scripts/publish_all_70mai.sh` | **Автопилот** — import → compose → YouTube |
| `./scripts/watch_publish_all_70mai.sh` | То же + авто-рестарт при падении (stall: import/compose/upload activity, не только `trip_*.mp4`) |
| `./scripts/autopilot_dashboard.sh` | Живой статус (второй терминал) |
| `./scripts/generate_card_reports.sh` | Отчёт по карте (MD/CSV) |
| `./scripts/run-tests.sh` | Unit-тесты (`tests/`) |
| `./scripts/smoke-test.sh` | **Smoke после правок** — тесты + синтаксис скриптов + `--help` CLI |
| `./scripts/bench_1h_run.sh` | **Тест ~1h** с SD: import → compose → private YouTube + `timing.jsonl` |
| `./scripts/bench_1h_dashboard.sh` | Дашборд для 1h-bench (те же пути, что у run) |
| `./scripts/bench_hevc_no_lock.sh` | Bench compose+upload hevc **без lock** (параллельно с автопилотом) |
| `./scripts/monitor_autopilot_perf.py` | Снимки метрик в `perf_monitor.jsonl` (copy/merge/encode/upload) |
| `./run scripts/update_youtube_metadata.py` | Обновить title/description/comment у уже залитых роликов |

Python — в `lib/`, тесты — в `tests/`. Вручную: `./run publish_70mai.py …`.

### Bench 1h (узкие места)

Тайминг по фазам → `video/Output/.publish_tmp/bench_1h/{timing.jsonl,summary.md,bench.log}`.

```bash
# terminal 1
./scripts/bench_1h_dashboard.sh
# terminal 2
./scripts/bench_1h_run.sh
# plan only
./scripts/bench_1h_run.sh --dry-run
# fixed window
./scripts/bench_1h_run.sh --from "2026-07-18 11:12" --to "2026-07-18 12:12"
```

По умолчанию: Normal Front+Back, первая сессия ≥60 мин на карте, profile `balanced` (bench), YouTube **`unlisted`** (нужно для комментария со списком клипов; `private` блокирует API). Автопилот: profile **`youtube`** + privacy **`unlisted`** в `70mai_runtime.json` (720p/20fps/3 Mbps H.264 — быстрый upload; `hevc` если HW доступен).

**Мониторинг узких мест:**

```bash
# terminal 1 — live snapshots каждые 30s
./scripts/monitor_autopilot_perf.py

# terminal 2 — отчёт balanced vs hevc + live stats
python3 анализ/generate_bottleneck_report.py
python3 анализ/analyze_performance.py   # исторический отчёт по publish_all.log
```

Артефакты: `video/Output/.publish_tmp/perf_monitor.jsonl`, `анализ/bottleneck_report.md`.

---

## Smoke-тесты после правок

После изменений в `lib/`, `scripts/` или `tests/` прогоняй smoke **до** запуска автопилота на карте:

```bash
cd /Users/cuthbert/work_local/70mai_project
./scripts/smoke-test.sh
```

Что проверяется:

| Шаг | Что |
|-----|-----|
| `bash -n` | Синтаксис `./run`, `publish_all_70mai.sh`, `autopilot_dashboard.sh`, … |
| `tests/` | Все unit-тесты (`unittest discover`) |
| `tests/test_smoke.py` | Импорт ключевых модулей, API `Dashboard` (`start`, `render`, …), `--help` у CLI |

Только smoke-модуль (быстрее):

```bash
./scripts/smoke-test.sh tests.test_smoke
```

Только unit-тесты без bash-проверок:

```bash
./scripts/run-tests.sh
```

**Если smoke падает** — чини код/скрипты и прогоняй снова, пока не будет `Smoke OK`. Типичные поломки после рефакторинга: метод класса оказался вне `@dataclass`, сломан `PYTHONPATH`, `--help` падает на импорте.

---

## Как запускать

```bash
cd /Users/cuthbert/work_local/70mai_project

# Карты ещё нет — ждать вставки
./scripts/watch_publish_all_70mai.sh --wait

# Карта уже вставлена
./scripts/publish_all_70mai.sh

# Перезапуск, если уже крутится другой автопилот (lock занят)
./scripts/publish_all_70mai.sh --force-restart --wait
# то же: --restart; в TTY без флага спросит [y/N]

# Только Parking / только план / без import
./scripts/publish_all_70mai.sh --types Parking
./scripts/publish_all_70mai.sh --dry-run
./scripts/publish_all_70mai.sh --types Parking --skip-import

# Прогресс (отдельное окно): copy/merge/compose/upload; строка prefetch в дашборде — только если в логе/proc ещё виден старый фоновый import (автопилот больше не запускает prefetch);
# шапка YouTube M/N = ~2h **ролики** (не поездки); в таблице `рM/N` — тот же счётчик; status.json сверяется с ffmpeg/publish CLI (автоисправление типа/chunk);
# compose tmp: `.publish_tmp/{Normal|Event|Parking}/chunk_NN/trip_NN.mp4` (legacy `chunk_NN/` только чтение);
# блок «Локальные файлы» — один путь (самая поздняя поездка, open до YouTube); внизу «Сбои»;
# Parking: сейчас Xs / цель ~7309s; после 3× short — [i]gnore/[r]etry (parts keep).
# Битый клип (moov/ffprobe) → quarantine `*.MP4.bad`, merge без него; счётчик в «Сбои»;
# история: host `video/Output/.publish_tmp/bad_clips.jsonl` + SD `/.70mai/import/bad_clips.jsonl`
# Compose/upload: вторая строка как у copy/merge — % · размер · скорость (Nx / MB/s) · ETA; % compose в шапке/таблице — **по ролику** (chunk), не сбрасывается при старте следующей поездки; в detail — trip N и % текущей поездки.
# Compose ждёт Front+Back ≥98%; в TUI — живое покрытие % по каждой камере.
# Блок «процессы»: watchdog/autopilot/import (pid, uptime, флаги), возраст publish_all.log, merge_stage на диске; если всё мертво — подсказка перезапуска.
# Компактный режим (<46 строк терминала): полный блок этапов (copy/merge/compose/upload); без рамки таблицы и легенды «конвейер».
# Upload Parking/Event: дашборд читает свежий `Upload part_…` из publish_all.log, если status.json застрял на compose.
# После «nothing to do» / Autopilot done — status → `phase=done` (не ghost upload@100% и не «ждёт» + compose-wait).
# Завершённые этапы: `✓ готово — <результат>` (не голое ✓), чтобы не путать с зависанием.
# Правки экрана — lib/autopilot_dashboard_view.py (автоперезагрузка).
./scripts/autopilot_dashboard.sh
```

### Этапы в дашборде (конвейер)

Один **ролик** (~2h, `рM/N`) проходит цепочку **по порядку**: import → compose → upload → следующий ролик.

| Этап | Что делает | Когда «► активно» |
|------|------------|-------------------|
| **copy** | Копирование минутных `.MP4` с флешки на SSD | `import_70mai` для текущего chunk |
| **merge** | Concat ~10‑мин `NO_*.mp4` Front/Back на SSD | После copy в том же import-окне |
| **compose** | 2‑cam vertical MP4 (~2h) из merged + black/silence sync | `publish_70mai` / ffmpeg encode |
| **upload** | Resumable PUT на YouTube | После compose, до `upload OK` в state |

**Маркеры:** `► активно` · `✓ готово` · `· ждёт`

**Шапка:** `YouTube 0/6 (0/18 поездок)` — 6 роликов на выгрузку, 18 строк-поездок в плане; `todo:6р` — роликов осталось. В таблице `р1/6` = ролик 1 из 6 (не «клип 1 из 18»).

**prefetch (legacy в UI):** дашборд может показать строку prefetch, если в `publish_all.log` или `proc` ещё висит старый `[prefetch background]` от предыдущих запусков; новый автопилот prefetch **не** запускает.

По умолчанию типы: **Normal Event Parking**.

**Parking / Event = один ролик:** все клипы типа за месяцы склеиваются в **один** 2-cam ролик (~длительность суммы клипов, часто ~2 ч), не «окно поездки по wall-clock». В логе merge `… final (30s)` / `1m 30s` — это **heartbeat прогресса** final concat, не длина файла.

**Синхронизация камер (Normal, Event, Parking):** import пишет рядом с **каждым** merge timeline-manifest (`<merge>.timeline.json`); compose **всегда** выравнивает Front/Back по общим слотам (Event/Parking — slot, Normal — wall-clock внутри окна поездки) и заменяет пропавшую/короткую камеру чёрным + тишиной. Compose берёт только клипы **окна текущей поездки** (Normal); Event/Parking — все слоты манифеста. Без manifest compose не стартует — нужен re-import. Имена merge хранят end как `%H%M%S` без даты: при пересечении полуночи `parse_merged_file` / Event-аналог делают `end += 1 day` (иначе prune/compose видят `end < start`). Логи: `Slots/Black fill`, `Window clips`, `[sync] output duration`. Подробнее — [GOALS.md](GOALS.md).

**YouTube — название и клипы:** при upload title = `70mai | {тип} | {начало} — {конец}` (тип: *простые записи* / *запись события* / *запись парковки*). В **описании и комментарии** — тот же список: `Клип N: дата время — дата время`. Комментарий через API работает только при **`unlisted` или `public`** (`private` → 403). Default autopilot: `privacy: unlisted` в `70mai_runtime.json`. OAuth после обновления кода: удалить token и войти снова (нужен scope для comment + update). Уже залитое видео:

```bash
./run scripts/update_youtube_metadata.py --types Parking
./run scripts/update_youtube_metadata.py --video-id VIDEO_ID --record-type Parking --apply
```

---

## Полезные параметры автопилота

| Флаг | Default | Смысл |
|------|---------|--------|
| `--wait` | off | Ждать SD |
| `--force-restart` / `--restart` | off | Убить предыдущий автопилот и взять lock. **Не** жать mid-import (`[copy]`) / mid-upload (`Upload part_`) — потеряешь resumable progress |
| `--types …` | Normal Event Parking | Что заливать |
| `--profile` | `youtube` | `balanced` / `draft` / `quality` / `hevc` / `youtube` (default в `70mai_runtime.json`) |
| `--privacy` | `unlisted` | YouTube privacy: `unlisted` (default, комментарии OK) / `private` / `public` |
| `--chunk-minutes` | `120` | Длина ролика (~мин) |
| `--min-free-gb` | `20` | Не compose, если мало места |
| `--prune-merged` | `after-compose` | Удалять 10‑мин склейки: `after-compose` / `after-upload` / `off`. После полной заливки автопилот ещё раз чистит merges + `.merge_stage` + compose tmp |
| `--repair` | `auto` | Чинить короткий Parking/Event merge: `auto` / `diagnose` / `off` |
| `--skip-import` | off | Только compose+upload (merge уже на диске) |
| `--no-overlap` | off | Отключить overlap compose∥upload внутри `publish_70mai` |
| `--no-prefetch-import` | off | Legacy no-op (фоновый prefetch import снят; import синхронный по чанку) |
| `--dry-run` | off | План без работы |
| `--no-dashboard` | off | Без таблицы в том же терминале (удобно с `autopilot_dashboard.sh`) |

Пример:

```bash
cd /Users/cuthbert/work_local/70mai_project
./scripts/watch_publish_all_70mai.sh --wait --profile youtube --min-free-gb 20
```

**Очистка SSD после успешного upload** (merges / `.merge_stage` / `part_*.mp4`; клипы на SD не трогает):

```bash
./scripts/cleanup_uploaded_70mai.sh --dry-run   # что удалит
./scripts/cleanup_uploaded_70mai.sh             # удалить
```

Автопилот вызывает ту же очистку сам, когда `pending=0` или прогон закончился без ошибок.

---

## Auto-recovery (автопилот)

| Проблема | Поведение |
|----------|-----------|
| **Watchdog stall (2 ч)** | Прогресс: `trip_*.mp4` / `part_*.mp4` / `.merge_stage` bytes; свежесть `publish_all.log` / `autopilot_status.json`; живые `import`/`publish`. Copy heartbeat на каждый `ok in`. Env: `WATCH_STALL_SEC`, `WATCH_LOG_ACTIVE_SEC`. |
| **Watchdog не убивает live work** | Если log/status свежие и жив import/ffmpeg/upload — cleanup **не** kill; второй watchdog ждёт. Default `WATCH_STOP_ON_SUCCESS=0` (крутить, пока есть pending). Force: `WATCH_FORCE_KILL=1`. Один instance: atomic mkdir lock (и для watchdog, и для `.publish_all.lock/` + `pid`). |
| **Мало места перед compose** | `guard_free_disk`: ждёт фоновый upload → prune merged + composed для уже залитых trips → retry до 4× (30 с). Prune merged зовёт `scan_merged_clips(probe=True)` (реальная длительность, не end из имени). В **chunk mode** prune только после `chunk_uploaded` (не по trip_parts внутри незалитого чанка). При неудаче chunk помечается failed, автопилот идёт дальше (`--continue-on-error`). |
| **Kill import / watchdog restart** | Import resume: готовые SSD merges + `chunk_merges_ready` → пропускает re-copy только при **Trip coverage ≥98%** (тот же порог, что compose; для Normal — aligned wall duration, не «есть ли хоть один entry»). `Trip.end` = конец footage (last clip start + duration), не timestamp последнего файла. Event/Parking slot-timeline: **не** фильтровать manifest по wall-clock окну поездки (месяцы клипов → один ролик). Repair **не** удаляет PA_*/EV_* при валидных `_part_*` или merge ≥98% — только force import resume. |
| **Upload OK, state не успел** | После `video_id` от YouTube state пишется на SD+host **до** удаления composed mp4 (`on_video_id` checkpoint). Comment 403 не откатывает upload. |
| **Проверка длины на YouTube** | После upload: `videos.list(contentDetails)` vs ffprobe локального mp4 (≥98%). Сразу после заливки YouTube часто отдаёт `P0D` (ещё processing) — ждём до ~15 мин с повтором. Короткий ролик → fail; `video_id` сохраняется в state до verify, чтобы не перезаливать. |
| **Dashboard: compose как upload** | 2-cam encode в `filter_complex` содержит `concat=n=…`. Дашборд отличает это от merge `-f concat` и показывает **compose** + правильный `trip_NN`, а не ложный upload/trip 1. |
| **Prefetch / BackgroundStep** | Фоновый prefetch import **снят**; `--no-prefetch-import` — no-op для совместимости со старыми watchdog-командами. |

### Чеклист (операции)

1. Питание + `WATCH_AWAKE=1` (default) / `caffeinate` — крышка только на AC.
2. **Не** `--force-restart`, пока в логе `[copy]` / `Upload part_`.
3. Смотреть: `tail -f video/Output/.publish_tmp/publish_all.log` или `./scripts/autopilot_dashboard.sh`.
4. После Parking upload — state `uploaded=true` и длина на YouTube ≥98% локального `part_01.mp4`.

---

```bash
cd /Users/cuthbert/work_local/70mai_project
tail -f video/Output/.publish_tmp/publish_all.log
tail -f video/Output/.publish_tmp/publish_all_watchdog.log
tail -f video/Output/.publish_tmp/repair_log.jsonl
tail -f video/Output/.publish_tmp/bad_clips.jsonl
```

Watchdog (`watch_publish_all_70mai.sh`) — см. **Auto-recovery** выше. Кратко: import/compose/upload activity, не только legacy `chunk_*/trip_*.mp4`.

Upload считается успешным даже если YouTube comment не прошёл (например `private` или processing) — state сохраняется, ролик не перезаливается.

Статус на карте: `/.70mai/` (publish state, OAuth, inventory). При смене физической карты (новый `card_id.txt`) autopilot очищает publish/import state на SD и локальный кэш (`autopilot_plan.json`, `import_*.state.json`); OAuth (`auth/`) сохраняется.

Uploaded-статус привязан не только к номеру чанка, а к **`wall_start` + `trip_indices`** (для Event/Parking ещё и `duration_sec`). Если на карте другой период съёмки при тех же индексах chunk 1/2/3 (перезапись, скопированный `.70mai/`, старый state), autopilot пишет `Publish state mismatch` / `Stale publish state: dropped …` и ставит такие чанки обратно в очередь — без полного сброса OAuth.
