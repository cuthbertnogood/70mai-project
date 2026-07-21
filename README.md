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
| `./run scripts/update_youtube_metadata.py` | Обновить title/description/comment у уже залитых роликов |

Python — в `lib/`, тесты — в `tests/`. Вручную: `./run publish_70mai.py …`.

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

**Синхронизация камер (Normal, Event, Parking):** import пишет рядом с **каждым** merge timeline-manifest (`<merge>.timeline.json`); compose **всегда** выравнивает Front/Back по общим слотам (Event/Parking — slot, Normal — wall-clock внутри окна поездки) и заменяет пропавшую/короткую камеру чёрным + тишиной. Compose берёт только клипы **окна текущей поездки**. Без manifest compose не стартует — нужен re-import. Логи: `Slots/Black fill`, `Window clips`, `[sync] output duration`. Подробнее — [GOALS.md](GOALS.md).

**YouTube — название и клипы:** при upload title = `70mai | {тип} | {начало} — {конец}` (тип: *простые записи* / *запись события* / *запись парковки*). В **описании и комментарии** — тот же список: `Клип N: дата время — дата время`. OAuth после обновления кода: удалить token и войти снова (нужен scope для comment + update). Уже залитое видео:

```bash
./run scripts/update_youtube_metadata.py --types Parking
./run scripts/update_youtube_metadata.py --video-id VIDEO_ID --record-type Parking --apply
```

---

## Полезные параметры автопилота

| Флаг | Default | Смысл |
|------|---------|--------|
| `--wait` | off | Ждать SD |
| `--force-restart` / `--restart` | off | Убить предыдущий автопилот и взять lock |
| `--types …` | Normal Event Parking | Что заливать |
| `--profile` | `balanced` | `balanced` / `draft` / `quality` / `hevc` |
| `--chunk-minutes` | `120` | Длина ролика (~мин) |
| `--min-free-gb` | `20` | Не compose, если мало места |
| `--prune-merged` | `after-compose` | Удалять 10‑мин склейки: `after-compose` / `after-upload` / `off` |
| `--repair` | `auto` | Чинить короткий Parking/Event merge: `auto` / `diagnose` / `off` |
| `--skip-import` | off | Только compose+upload (merge уже на диске) |
| `--no-overlap` | off | Отключить overlap compose∥upload внутри `publish_70mai` |
| `--dry-run` | off | План без работы |
| `--no-dashboard` | off | Без таблицы в том же терминале (удобно с `autopilot_dashboard.sh`) |

Пример:

```bash
cd /Users/cuthbert/work_local/70mai_project
./scripts/watch_publish_all_70mai.sh --wait --profile balanced --min-free-gb 20
```

---

## Логи

```bash
cd /Users/cuthbert/work_local/70mai_project
tail -f video/Output/.publish_tmp/publish_all.log
tail -f video/Output/.publish_tmp/publish_all_watchdog.log
tail -f video/Output/.publish_tmp/repair_log.jsonl
tail -f video/Output/.publish_tmp/bad_clips.jsonl
```

Watchdog (`watch_publish_all_70mai.sh`) считает автопилот живым, пока растут `Normal/chunk_*/trip_*.mp4` / `part_*.mp4`, обновляется `publish_all.log` / `autopilot_status.json`, или работает `import_70mai` / `publish_70mai`. Раньше смотрел только legacy `chunk_*/trip_*.mp4` и убивал import/encode каждые 2 ч. Env: `WATCH_STALL_SEC`, `WATCH_LOG_ACTIVE_SEC` (см. header скрипта).

Статус на карте: `/.70mai/` (publish state, OAuth, inventory). При смене физической карты (новый `card_id.txt`) autopilot очищает publish/import state на SD и локальный кэш (`autopilot_plan.json`, `import_*.state.json`); OAuth (`auth/`) сохраняется.
