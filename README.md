# 70mai — от флешки до YouTube

Проект берёт запись с регистратора **70mai**, склеивает ролики и заливает на YouTube.

Кратко для запуска. Флаги, OAuth, профили, тюнинг — в [детальное_описание.md](детальное_описание.md).

---

## Схема пайплайна

```mermaid
flowchart LR
  SD["SD-карта<br/>клипы ~1 мин"] --> IMP["1. Import<br/>склейка"]
  IMP --> MERGED["Локально<br/>~10 мин Front/Back"]
  MERGED --> COMP["2. Compose<br/>вертикальный 2-cam"]
  COMP --> YT["3. YouTube<br/>upload"]
  YT --> CLEAN["4. Очистка<br/>локальных файлов"]
  YT --> META["5. Статус на SD<br/>.70mai/"]
```

---

## Что происходит по шагам

### 1. Import — склейка с флешки

Короткие клипы (~1 мин) с SD (`Normal` / `Event` / `Parking`, Front и Back) читаются с карты (обычно `/Volumes/Untitled`).

Внутри одного чанка:

```mermaid
flowchart LR
  A["Клипы на SD"] --> B["Копия на SSD<br/>пачками stage_batch_clips"]
  B --> C["ffmpeg concat<br/>-c copy"]
  C --> D["Готовый файл<br/>~chunk_clips минут"]
  D --> E["Удалить<br/>минутные копии"]
```

На выходе: файлы вида `NO_…_F.mp4` / `…_B.mp4` в `video/Output/`.

### 2. Compose — вертикальное видео

Front сверху, Back снизу → один ролик на поездку/чанк (профиль из конфига, по умолчанию `balanced`).

### 3. Upload — YouTube

Ролик уходит на канал (по умолчанию private). Размер кусков для загрузки задаёт `publish_chunk_minutes`.

### 4. Очистка Mac

После успешной загрузки (или после compose — см. `prune_merged`) временные склейки удаляются, чтобы освободить диск.

### 5. Статус на флешке

В `/.70mai/` на SD пишутся статусы, ссылки YouTube и краткий отчёт — можно продолжить на другом Mac.

---

## Как запустить

Нужны: Mac, Python 3.10+, ffmpeg, вставленная SD-карта 70mai.

```bash
scripts/setup-venv.sh          # первый раз
./scripts/publish_all_70mai.sh --wait
./scripts/watch_publish_all_70mai.sh --wait   # то же + авто-рестарт
./scripts/autopilot_dashboard.sh              # прогресс в другом окне
```

Карта уже вставлена: те же команды без `--wait`.

---

## Первый запуск YouTube

Положите OAuth-файл в `~/.config/70mai/youtube_credentials.json` и при первом upload войдите в браузере.  
Подробности: [детальное_описание.md](детальное_описание.md#youtube-oauth-one-time).

---

## Тюнинг на ходу

Параметры — в [`70mai_runtime.json`](70mai_runtime.json)  
(или override: `video/Output/.publish_tmp/70mai_runtime.json`).

```mermaid
flowchart TB
  CFG["Правите 70mai_runtime.json"] --> Q{Когда подхватит?}
  Q -->|stage_batch_clips, retries, prefetch| M["Сразу — следующий merge"]
  Q -->|chunk_clips, probe_workers| G["Со следующей группы камеры"]
  Q -->|profile, min_free_gb, prune_merged| P["Перед следующим publish"]
  Q -->|chunk_minutes, merge_workers, gap| R["Нужен новый import / рестарт"]
```

Полная таблица всех ключей: [Runtime config](детальное_описание.md#runtime-config-70mai_runtimejson).

---

## Полезное

| Действие | Команда |
|----------|---------|
| Что на карте | `python3 import_70mai.py --scan` |
| Только план | `./scripts/publish_all_70mai.sh --dry-run` |
| Лог автопилота | `tail -f video/Output/.publish_tmp/publish_all.log` |
| Лог watchdog | `tail -f video/Output/.publish_tmp/publish_all_watchdog.log` |
| Отчёт по карте | `./scripts/generate_card_reports.sh` |

Цели: [GOALS.md](GOALS.md). Детали: [детальное_описание.md](детальное_описание.md).
