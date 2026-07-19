# 70mai — SD → YouTube

Автопилот: карта 70mai → склейка → 2-cam MP4 → YouTube. Пока текущий ~2h чанк кодируется и заливается, следующий чанк может импортироваться с SD в фоне (prefetch import).

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
| `./scripts/watch_publish_all_70mai.sh` | То же + авто-рестарт при падении |
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

# Прогресс (отдельное окно): copy/merge/compose/upload; prefetch import следующего чанка — строка `prefetch` в этапах и `prefetch ch.N` в proc; compose % пишется в autopilot_status.json каждые ~1.5s (typed `.publish_tmp/Normal/chunk_NN/`); если файл >5 мин без обновления — дашборд подхватывает ffmpeg + heartbeat из log;
# шапка YouTube M/N = ~2h **ролики** (не поездки); в таблице `рM/N` — тот же счётчик; status.json сверяется с ffmpeg/publish CLI (автоисправление типа/chunk);
# compose tmp: `.publish_tmp/{Normal|Event|Parking}/chunk_NN/trip_NN.mp4` (legacy `chunk_NN/` только чтение);
# блок «Локальные файлы» — один путь (самая поздняя поездка, open до YouTube); внизу «Сбои»;
# Parking: сейчас Xs / цель ~7309s; после 3× short — [i]gnore/[r]etry (parts keep).
# Битый клип (moov/ffprobe) → quarantine `*.MP4.bad`, merge без него; счётчик в «Сбои»;
# история: host `video/Output/.publish_tmp/bad_clips.jsonl` + SD `/.70mai/import/bad_clips.jsonl`
# Compose/upload: вторая строка как у copy/merge — % · размер · скорость (Nx / MB/s) · ETA.
# Compose ждёт Front+Back ≥98%; в TUI — живое покрытие % по каждой камере.
# Правки экрана — lib/autopilot_dashboard_view.py (автоперезагрузка).
./scripts/autopilot_dashboard.sh
```

### Этапы в дашборде (конвейер)

Один **ролик** (~2h, `рM/N`) проходит цепочку. Пока текущий ролик на **compose/upload**, следующий может **prefetch**-иться в фоне.

| Этап | Что делает | Когда «► активно» |
|------|------------|-------------------|
| **prefetch** | Фоновый `import_70mai` **следующего** chunk (copy+merge на SD→SSD), параллельно publish текущего | `proc: prefetch ch.N` + строка в `publish_all.log` `[prefetch background]` |
| **copy** | Копирование минутных `.MP4` с флешки на SSD | Основной import (не prefetch) или тот же copy внутри prefetch |
| **merge** | Concat ~10‑мин `NO_*.mp4` Front/Back на SSD | После copy в том же import-окне |
| **compose** | 2‑cam vertical MP4 (~2h) из merged + black/silence sync | `publish_70mai` / ffmpeg encode |
| **upload** | Resumable PUT на YouTube | После compose, до `upload OK` в state |

**Маркеры:** `► активно` · `✓ гotово` · `· ждёт`

**Шапка:** `YouTube 0/6 (0/18 поездок)` — 6 роликов на выгрузку, 18 строк- поездок в плане; `todo:6р` — роликов осталось. В таблице `р1/6` = ролик 1 из 6 (не «клип 1 из 18»).

**Параллель (ваш пример):** compose **р1/6** (chunk 1) + prefetch **chunk 2** → в «этапах» prefetch ►, copy/merge ✓ (для chunk 1 уже сделаны), compose ►; copy/merge в prefetch идут в **строке prefetch**, не в основных copy/merge.

По умолчанию типы: **Normal Event Parking**.

**Синхронизация камер (Normal, Event, Parking):** import пишет рядом с **каждым** merge timeline-manifest (`<merge>.timeline.json`); compose **всегда** выравнивает Front/Back по общим слотам (Event/Parking — slot, Normal — wall-clock внутри окна поездки) и заменяет пропавшую/короткую камеру чёрным + тишиной. Клипы из prefetch других chunk'ов **не** попадают в timeline текущей поездки. Без manifest compose не стартует — нужен re-import. Логи: `Slots/Black fill`, `Window clips`, `[sync] output duration`. Подробнее — [GOALS.md](GOALS.md).

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
| `--no-prefetch-import` | off | Не запускать import следующего ~2h чанка параллельно compose/upload текущего |
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

Статус на карте: `/.70mai/` (publish state, OAuth, inventory). При смене физической карты (новый `card_id.txt`) autopilot очищает publish/import state на SD и локальный кэш (`autopilot_plan.json`, `import_*.state.json`); OAuth (`auth/`) сохраняется.
