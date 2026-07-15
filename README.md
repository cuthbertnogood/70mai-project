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

Python — в `lib/`, тесты — в `tests/` (`./scripts/run-tests.sh`). Вручную: `./run publish_70mai.py …`.

---

## Как запускать

```bash
cd /Users/cuthbert/work_local/70mai_project

# Карты ещё нет — ждать вставки
./scripts/watch_publish_all_70mai.sh --wait

# Карта уже вставлена
./scripts/publish_all_70mai.sh

# Только Parking / только план / без import
./scripts/publish_all_70mai.sh --types Parking
./scripts/publish_all_70mai.sh --dry-run
./scripts/publish_all_70mai.sh --types Parking --skip-import

# Прогресс (отдельное окно): copy / merge / compose / upload — каждый на своей строке
./scripts/autopilot_dashboard.sh
```

По умолчанию типы: **Normal Event Parking**.

---

## Полезные параметры автопилота

| Флаг | Default | Смысл |
|------|---------|--------|
| `--wait` | off | Ждать SD |
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
```

Статус на карте: `/.70mai/` (publish state, OAuth, inventory).
