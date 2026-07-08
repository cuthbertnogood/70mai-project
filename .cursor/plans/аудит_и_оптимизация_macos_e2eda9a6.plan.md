---
name: Аудит и оптимизация macOS
overview: "Аудит выявил 3 узких места: диск заполнен на 94% (главное), нехватка RAM со свопом, слабый двухъядерный CPU под фоновой нагрузкой. План: освободить ~100+ GB удалением merged-источников загруженных поездок, исключить видео из Spotlight, убрать Google-агенты, снизить фоновую нагрузку."
todos:
  - id: cleanup-script
    content: Создать scripts/cleanup_uploaded_sources.py (dry-run/apply, маппинг uploaded-поездок на merged-файлы)
    status: completed
  - id: run-cleanup
    content: Запустить dry-run, показать список, после подтверждения --apply; проверить df
    status: completed
  - id: spotlight-exclude
    content: Исключить video/ из Spotlight-индексации
    status: completed
  - id: google-agents
    content: Отключить и удалить Google Keystone/Updater launch-агенты
    status: completed
  - id: ui-memory-tweaks
    content: Reduce Motion/Transparency, Chrome Memory Saver (инструкции + что можно через defaults)
    status: completed
  - id: hw-encode-check
    content: Проверить HW-профиль VideoToolbox в compose/автопилоте, сделать дефолтом при необходимости
    status: completed
  - id: verify
    content: "Финальная верификация: диск, swap, load, launchctl; обновить README"
    status: completed
isProject: false
---

# Аудит и оптимизация macOS (MacBook Air 2018)

## Результаты аудита

**Железо:** MacBook Air 2018 (MacBookAir8,1), i5-8210Y 2 ядра 1.6 GHz (TDP 7W), 8 GB RAM (распаяна), SSD 250 GB, чип T2. macOS Sonoma 14.8.7 — оптимальная версия для этого железа, обновлять на Sequoia не стоит.

### Узкие места (по критичности)

- **Диск 94% занят — 13 GB свободно из 233.** Критично: APFS деградирует при <10% свободного места, swap-файлам некуда расти, compose-пайплайну нужно до 7.7 GB на chunk. Источник: `video/Output` = 169 GB (Normal 137 + Event 33).
- **RAM 8 GB — постоянный swap.** Swap 1.2/2 GB занят, 317k pageouts. Chrome (18 процессов) + Cursor (16 процессов) конкурируют; Cursor Renderer ест 60% CPU и 1.1 GB.
- **CPU перегружен.** Load average 15-мин достигал 179 при 2 ядрах — ffmpeg compose + Spotlight-индексация видео + Chrome/Cursor одновременно.
- **Spotlight индексирует video/Output** — каждый новый merged MP4 (гигабайты) гоняет mdworker.
- **Google Keystone/Updater** — 3 launch-агента в автозагрузке (решено убрать).
- Фон: `photoanalysisd`, Happ VPN tunnel (7.5% CPU), WindowServer 18% (прозрачность UI на слабом iGPU).

**Здоровое:** SMART Verified, троттлинга сейчас нет, TM-снапшотов нет, Spotlight на SD уже отключён, powernap выключен.

## Шаг 1 — Освободить диск (цель: 60+ GB свободно)

Merged-источники именуются по wall-clock диапазону (`NO_20260427-085015_085315_F.mp4`), поездки в state на SD (`/Volumes/Untitled/.70mai/publish/publish_Normal.state.json`) имеют `uploaded: true` и диапазоны в [`publish_plan.md`](video/Output/publish_plan.md).

Новый скрипт [`scripts/cleanup_uploaded_sources.py`](scripts/cleanup_uploaded_sources.py):

- Читает state с SD + trip-диапазоны (переиспользовать группировку из `plan_estimate.py`)
- Сопоставляет merged-файлы `video/Output/Normal/{Front,Back}` с загруженными поездками по временному диапазону
- `--dry-run` по умолчанию (список + объём), удаление только с `--apply`
- **Не трогает** источники pending-поездок — пайплайн сейчас работает (файлы пишутся, автопилот активен)

Дополнительно вручную (по желанию, покажу список): `~/Downloads` 8.8 GB, старый бэкап `70mai_project_backup_2026-07-08_0109` (167 MB).

## Шаг 2 — Исключить видео из Spotlight

Добавить `~/work_local/70mai_project/video` в исключения: System Settings → Siri & Spotlight → Spotlight Privacy (ручной шаг), либо через `sudo` в plist исключений тома. Это уберёт mdworker-нагрузку при каждом compose/import.

## Шаг 3 — Убрать Google-агенты

```bash
launchctl bootout gui/$UID/com.google.keystone.agent 2>/dev/null
launchctl bootout gui/$UID/com.google.keystone.xpcservice 2>/dev/null
launchctl bootout gui/$UID/com.google.GoogleUpdater.wake 2>/dev/null
rm ~/Library/LaunchAgents/com.google.*.plist
```

Заметка: Chrome может пересоздать их при ручном обновлении — это нормально, можно повторить.

## Шаг 4 — Снизить фоновую нагрузку (best practices для 8 GB / Intel)

- **Reduce Motion + Reduce Transparency** (System Settings → Accessibility → Display) — разгружает WindowServer на iGPU
- **Chrome Memory Saver** (chrome://settings/performance) — усыпляет фоновые вкладки
- Рекомендация по режиму работы: во время compose-запусков не держать Chrome с десятками вкладок; периодически перезапускать Cursor (Renderer копит память в долгих сессиях)
- `photoanalysisd` доработает индекс один раз и затихнет — не трогаем

## Шаг 5 — Пайплайн: подтвердить HW-кодирование

У Air 2018 есть чип T2 → VideoToolbox HW encode. Проверить, что compose использует `--profile` с `h264_videotoolbox` (по README уже поддерживается), и что автопилот запускается с HW-профилем. Если нет — сделать HW-профиль дефолтом на этой машине. Это главный рычаг скорости compose и защита от троттлинга.

## Шаг 6 — Верификация

- `df -h` → ≥60 GB свободно
- `memory_pressure`, `sysctl vm.swapusage` → снижение swap после перезапуска приложений
- `uptime` load average при работающем пайплайне
- `launchctl list | grep google` → пусто
- README: краткая секция про `cleanup_uploaded_sources.py` (auto-documentation)

## Что сознательно не трогаем

- FileVault выключен — включение замедлит старый Intel SSD-стек (решение за пользователем, это безопасность, не производительность)
- Amphetamine и Happ — пользовательские инструменты (Amphetamine нужен пайплайну для предотвращения сна)
- ripgrep, Homebrew (35 формул — умеренно)
- Обновление macOS до Sequoia — не рекомендуется на 8 GB Intel
