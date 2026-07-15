# Что мы хотим сделать

Документ фиксирует цели проекта по работе с видео с регистратора **70mai**.

## Основная задача

Автоматизировать путь от SD-карты до удобных для просмотра и хранения видеофайлов на локальном диске.

## Шаг 1 — экспорт с флеш-карты

- Считывать видео с SD-карты регистратора (типичный путь: `/Volumes/Untitled` или аналог).
- Копировать файлы в локальный каталог проекта (`video/`), не трогая оригиналы на карте.
- Учитывать структуру папок 70mai:
  - `Normal/` — непрерывная запись
  - `Event/` — события
  - `Parking/` — парковочный режим
  - `Lapse/` — таймлапс
- Внутри каждой категории — подпапки `Front/` и `Back` (передняя и задняя камеры).
- Имена файлов содержат дату и время, например: `NO20260425-130119-040747F.MP4`.

## Шаг 2 — сборка видео по 10 минут

Регистратор пишет короткие клипы (~1 минута). Нужно склеивать их в более длинные ролики **~10 минут**.

- Группировать клипы по камере (`Front` / `Back`), категории (`Normal`, `Event` и т.д.) и хронологии.
- Сохранять результат в локальный каталог с понятной структурой и именами.
- Сохранять порядок записи; не смешивать разные камеры в одном файле.

## Открытый вопрос — сжатие

**Пока не решено**, нужно ли перекодировать видео для экономии места.

| Вариант | Плюсы | Минусы |
|---------|-------|--------|
| Без сжатия (копия / concat без перекодирования) | Быстро, без потери качества | Много места на диске |
| Со сжатием (H.264/H.265, CRF и т.п.) | Меньше объём | Дольше, возможна потеря качества |

Решение можно принять позже, когда станет ясен типичный объём сырых файлов и требования к качеству.

## Шаг 3 — публикация 2-cam на YouTube

- **`lib/plan_estimate.py`** — pre-flight: поездки, куски, `publish_plan.md`
- **`lib/compose_2cam_70mai.py`** — Front↑ Back↓ vertical, wall-clock sync
- **`lib/publish_70mai.py`** — trip chunks → compose → YouTube → delete; `--per-trip-upload`, `--upload-only`, `--resume-upload`, `--mark-uploaded`
- **`lib/youtube_upload.py`** — OAuth + resumable upload (256 MB chunks по умолчанию, `--upload-chunk-mb 0` = один PUT, `.upload.json` resume) + playlist
- **`lib/youtube_upload_diagnostics.py`** + **`scripts/analyze_youtube_upload.py`** — JSONL diag log + failure analysis
- По умолчанию загрузка **private** (не public/unlisted)

Target chunk: **2 ч по поездкам** (короткие поездки склеиваются; длинная ≥2 ч — solo).

### План публикации Normal (Apr 2026)

| Chunk | Поездки | ~MB | Статус |
|-------|---------|-----|--------|
| 1 | 1–5 | 6074 | upload готовых MP4 (trip 1–2 ✅, 3–5 в процессе) |
| 2 | 6–7 | 403 | compose → upload → delete |
| 5 | 11 | 26 | compose → upload → delete (быстрый хвост) |
| 4 | 9–10 | 6008 | compose → upload → delete |
| 3 | 8 | 7729 | compose → upload → delete (самый долгий) |

**Статус Jul 2026:** Normal **11/11** + Event **1/1** на YouTube ✅ (см. `отчеты/` или `/.70mai/reports/` на SD — `./scripts/generate_card_reports.sh`).

### Следующий этап — Parking

Parking входит в **дефолт autopilot** (`Normal Event Parking`). На карте **~506** клипов — **1 YouTube-видео** (как Event). Если merge ещё не делался:

```bash
./scripts/publish_all_70mai.sh --types Parking          # import + publish
./scripts/publish_all_70mai.sh --types Parking --skip-import   # если PA уже merged
```

Watchdog `./scripts/watch_publish_all_70mai.sh` подхватывает Parking автоматически (без явного `--types`).

**Self-healing (Jul 2026):** короткий/stale `PA_` больше не скипается import’ом (tolerance Event/Parking **0.98**; fingerprint `clip_count`/`last_clip`). Autopilot `--repair auto` диагностирует gap до compose, удаляет битый merge и пересобирает; compose fallback = `min(trip, front, back)`.

### Ускорение пайплайна (Jul 2026)

- **Import:** 2 параллельных ffmpeg concat, фоновый прогрев page cache следующего чанка, общий ffprobe-кэш (`.probe_cache.json`) для plan/import/publish, `-probesize 1M -analyzeduration 0`.
- **Compose:** hw decode включён по умолчанию (fastest-first fallback), профиль `hevc` (~3.5 Mbps ≈ H.264 6.5 Mbps → upload в ~1.9× быстрее) с авто-фолбэком на h264, `-prio_speed 1`.
- **Publish:** конвейер compose(N+1) ∥ upload(N) (выкл: `--no-overlap`), disk guard `--min-free-gb`, `--prune-merged after-upload|after-compose` (merged-файлы удаляются после использования; исходники остаются на SD).

### Автопилот (один скрипт, без Cursor)

**`publish_all_70mai.py`** / **`scripts/publish_all_70mai.sh`** — вставил флешку → import → compose → YouTube → delete:

```bash
./scripts/publish_all_70mai.sh --wait
```

- Авто-поиск SD в `/Volumes/Untitled` или любой том с `Normal/Front` + `Normal/Back`
- **Статус загрузки на флешке:** `/.70mai/publish/publish_Normal.state.json` + `sessions/*.upload.json` (resume)
- **OAuth на флешке (по умолчанию):** `/.70mai/auth/` — переносимая авторизация; отключение: `--no-auth-on-sd`
- **Новая флешка:** автопилот сам создаёт `.70mai/` (OAuth с хоста + browser login + пустой state), затем import → compose → upload
- **Инвентарь на флешке:** `/.70mai/import/CARD_SUMMARY.txt` — поездки, даты, статус склейки; переносим между Mac
- При повторном запуске или на **другом Mac** — продолжает с места остановки (`--resume` автоматически)
- Локально — только кэш state и временные MP4 (удаляются после upload)
- Лог: `video/Output/.publish_tmp/publish_all.log` (`tail -f …`)
- **Resume встроен** (state + `sessions/*.upload.json` на SD); **автоперезапуск при падении upload — нет** — после kill/crash: `./scripts/publish_all_70mai.sh --skip-import` или **`./scripts/watch_publish_all_70mai.sh --skip-import`** (watchdog с restart)
- `monitor_compose.sh` — отдельный watchdog только для compose (ffmpeg), не для upload и не внутри autopilot

## Что не входит в текущие цели (пока)

- Веб-интерфейс.
- Автоматический запуск по подключению карты (можно добавить отдельно).

## Backlog — позже

### GPS-телеметрия и миникарта в видео

**Статус: отключено** (`TELEMETRY_ENABLED = False` в `telemetry_overlay.py`). Черновик кода остаётся в репо; `--telemetry` в compose/publish игнорируется.

Наложение данных из `GPSData*.txt` на итоговое 2-cam видео (стиль 70mai RS / dashcam HUD).

**Что хотим на экране:**

| Элемент | Источник | Статус данных |
|---------|----------|---------------|
| Миникарта + хвост маршрута | lat/lon из GPS | ✅ есть в `GPSData*.txt` |
| Скорость (KM/H) | поле скорости в GPS | ✅ есть |
| Компас / направление | heading или bearing между точками | ✅ вычисляется |
| Координаты + время | GPS + wall-clock sync | ✅ есть |
| G-force | поля аксelerometer в GPS или Δspeed | ⚠️ на карте часто нули; fallback — из изменения скорости |
| Рамки машин, стрелки полос (AR) | приложение 70mai | ❌ в сырых файлах нет — только постобработка в приложении |

**Технический план (черновик):**

1. Парсинг `GPSData*.txt` по wall-clock диапазону видео (сканирование в `--scan` уже есть).
2. Рендер HUD-панели: OpenStreetMap-тайлы (кэш `~/.cache/70mai/map_tiles`), Pillow.
3. Наложение через ffmpeg `overlay` в `compose_2cam_70mai.py` — флаг `--telemetry`.
4. Прокинуть `--telemetry` в `publish_70mai.py` для YouTube-роликов.

**Заметки с пробы:**

- GPS покрывает не весь wall-clock диапазон клипов — нужен overlap или предупреждение.
- Рендер overlay + qtrle в filter_complex сильно замедляет encode; оптимизировать (5 Hz HUD, lighter codec).
- Черновик кода: `gps_70mai.py`, `telemetry_overlay.py` (WIP, не в prod-пайплайне).
- **Фиксы (Jul 2026):** авто `--gps-offset`; follow-map z18 + CARTO Voyager; movement-gate для скорости при стоянке; 2% прозрачность панели.

**Отдельно (низкий приоритет):** экспорт GPX/HTML-карты для просмотра треков без видео.

## Ожидаемый результат

1. Скрипт(ы) в `scripts/` для импорта с карты.
2. Скрипт(ы) для склейки клипов в сегменты ~10 минут.
3. Итоговые файлы в `video/Output/` — готовые к просмотру и архивации.
