# 70mai — SD → YouTube

Автопилот: карта 70mai → склейка → 2-cam MP4 → YouTube.

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
| `./run scripts/update_youtube_metadata.py` | Обновить title/description/comment у уже залитых роликов |

Python — в `lib/`, тесты — в `tests/` (`./scripts/run-tests.sh`). Вручную: `./run publish_70mai.py …`.

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

# Прогресс (отдельное окно): copy/merge/compose/upload; compose % из autopilot_status.json
# (не stale Encode из publish_all.log); все *.log в .publish_tmp; proc: publish_70mai = publish;
# блок «Локальные файлы» (open …/chunk_XX/trip_YY.mp4 до YouTube); внизу «Сбои»;
# Parking: сейчас Xs / цель ~7309s; после 3× short — [i]gnore/[r]etry (parts keep).
# Битый клип (moov/ffprobe) → quarantine `*.MP4.bad`, merge без него; счётчик в «Сбои»;
# история: host `video/Output/.publish_tmp/bad_clips.jsonl` + SD `/.70mai/import/bad_clips.jsonl`
# Compose/upload: вторая строка как у copy/merge — % · размер · скорость (Nx / MB/s) · ETA.
# Compose ждёт Front+Back ≥98%; в TUI — живое покрытие % по каждой камере.
# Правки экрана — lib/autopilot_dashboard_view.py (автоперезагрузка).
./scripts/autopilot_dashboard.sh
```

По умолчанию типы: **Normal Event Parking**.

**Синхронизация камер (Normal, Event, Parking):** import пишет рядом с **каждым** merge timeline-manifest (`<merge>.timeline.json`); compose **всегда** выравнивает Front/Back по общим слотам (Event/Parking — slot, Normal — wall-clock) и заменяет пропавшую/короткую камеру чёрным + тишиной. Без manifest compose не стартует — нужен re-import. Логи: `Slots/Black fill`, `[sync] output duration`. Подробнее — [GOALS.md](GOALS.md).

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

Статус на карте: `/.70mai/` (publish state, OAuth, inventory). При смене физической карты (новый `card_id.txt`) autopilot автоматически очищает publish/import state со старой карты; OAuth (`auth/`) сохраняется.
