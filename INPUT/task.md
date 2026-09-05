# Autopilot — техническое задание

## Запуск

```bash
cd /Users/cuthbert/work/cursor/70mai_project

./scripts/setup-venv.sh                    # один раз

./scripts/autopilot.sh                     # веб-Dashboard http://127.0.0.1:8787/
./scripts/autopilot.sh --no-browser
./scripts/autopilot.sh -- --skip-import

./scripts/publish_all_70mai.sh --wait      # CLI без веба
tail -f video/Output/.publish_tmp/publish_all.log
```

Полный список команд — в [README.md](../README.md) (раздел **Запуск**).

## Цель

**Autopilot** — приложение-оркестратор: вставил SD-карту 70mai → дождался полного прогона (import → compose → upload на YouTube) → закрыл сессию. Пайплайн не переписывается: движок — существующие `publish_all_70mai.py` / `import_70mai.py` / `publish_70mai.py`.

Исходные требования:

1. Найти флеш-диск с видеозаписями 70mai A810.
2. Скачать (import с SD на SSD), объединить (merge), закодировать (compose 2-cam), загрузить на YouTube.
3. Ролики на YouTube — **~2 часа** (Chunk из одного или нескольких коротких Trip).
4. Показывать **весь прогон** в **Dashboard** (веб), не только upload.

## Жизненный цикл (one-shot)

| Этап | Поведение |
|------|-----------|
| Старт | `./scripts/autopilot.sh` — HTTP на `127.0.0.1`, открытие браузера, URL в терминале |
| Нет карты | Ждать 70mai SD без таймаута (как `--wait`); Dashboard: «ожидание карты» |
| Прогон | Spawn `publish_all_70mai.py --wait --no-dashboard`; при crash — рестарт (как watchdog), **кроме Stop** |
| Успех / ошибка / Stop | Dashboard остаётся до **Quit** |
| Quit | Процесс Autopilot завершается |

## Dashboard

- **Сеть:** только `127.0.0.1`, без логина.
- **Данные:** тот же `autopilot_status.json` и план, что TTY-дашборд (`autopilot_dashboard.sh` остаётся для отладки).
- **Экран:** карта, фаза (Import / Compose / Upload / OAuth), Chunk/Trip, диск, YouTube URL, сбои.

### Команды

| Кнопка | Действие |
|--------|----------|
| **Stop** | Конец **этого** прогона: kill пайплайна, рестарт запрещён. State на SD не трогаем. Следующий прогон — новый запуск Autopilot. |
| **Skip** | Отложить **текущий Chunk** (Deferred Chunk): не compose/upload в этом прогоне, **не** `mark-uploaded`. Следующий Autopilot снова возьмёт Chunk. |
| **Repair** | Существующий `--repair auto` по текущему сбою; если сбоя нет — no-op. |
| **Quit** | Выход Autopilot (после ожидания карты / готово / stop / ошибка). |

## Наследовано из пайплайна (без изменений)

- Типы по умолчанию: **Normal**, **Event**, **Parking**.
- Chunk ~**120 мин** из Trip; Event/Parking — один Chunk на тип.
- OAuth и publish state на SD (`/.70mai/`).
- Privacy **unlisted**, profile **youtube** из `70mai_runtime.json`.
- Resume: state на SD + `sessions/*.upload.json`.

## Вне скоупа

- Автозапуск по USB / `launchd`.
- Доступ к Dashboard из LAN.
- Замена CLI `publish_all_70mai.sh` (остаётся для отладки и скриптов).

## Запуск

```bash
./scripts/autopilot.sh              # открыть браузер
./scripts/autopilot.sh --no-browser # только URL в терминале
```

Лог пайплайна: `video/Output/.publish_tmp/publish_all.log`.
