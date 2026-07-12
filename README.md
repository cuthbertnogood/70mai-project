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

### 1. Import — два конвейера параллельно

Короткие клипы (~1 мин) с SD (`Normal` / `Event` / `Parking`, Front и Back).

```mermaid
flowchart TB
  SD["SD-карта"] --> COPY["Конвейер 1: [copy]<br/>SD → SSD"]
  COPY --> Q["Очередь на SSD<br/>готовые чанки"]
  Q --> MERGE["Конвейер 2: [merge]<br/>ffmpeg concat -c copy"]
  MERGE --> OUT["video/Output/<br/>~10 мин файлы"]
  MERGE --> DEL["Удалить минутные<br/>копии со SSD"]
  COPY -.->|пока merge идёт| COPY
```

- **[copy]** копирует следующий чанк с флешки на SSD (вперёд до `prefetch_batches` чанков).
- **[merge]** как только на SSD набрался полный чанк (`chunk_clips`), склеивает **с диска**, не читая SD.
- Пока идёт склейка, copy уже тянет следующий чанк — поэтому не «сначала всё скопировать, потом всё склеить», а **overlap**.

В логе ищите строки:
`[copy] START … SD→SSD` → `[copy] DONE … queued for [merge]` → `[merge] START … enough clips on SSD` → `[merge] DONE`.

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
```

Карта уже вставлена: те же команды без `--wait`.

Прогресс в другом окне:

```bash
./scripts/autopilot_dashboard.sh
```

Во время import дашборд показывает параллельно:
`► [copy] … N/M MB (%)` и `► [merge] … N/M MB (%)`, плюс блок **процессы**.

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
  Q -->|stage_batch_clips, retries| M["Сразу — следующий copy/merge"]
  Q -->|chunk_clips, prefetch_batches| G["Со следующей группы камеры"]
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
