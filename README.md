# 70mai — от флешки до YouTube

Проект берёт запись с регистратора **70mai**, склеивает ролики и заливает на YouTube.

Кратко для запуска. Все флаги, OAuth, профили и отладка — в [детальное_описание.md](детальное_описание.md).

---

## Что происходит по шагам

1. **Копируем исходники с флешки**  
   Клипы с SD (`Normal` / `Event` / `Parking`, камеры Front и Back) читаются с карты (обычно `/Volumes/Untitled`).

2. **Склеиваем в куски по ~10 минут**  
   Короткие клипы (~1 мин) сначала копируются с SD на локальный диск пачками (`stage_batch_clips`), склеиваются (`chunk_clips`), затем минутные копии удаляются. Параметры в [`70mai_runtime.json`](70mai_runtime.json) — можно менять на ходу (import перечитывает перед каждым merge).

3. **Собираем вертикальное видео Front + Back**  
   Две камеры в одном кадре: сверху передняя, снизу задняя.

4. **Загружаем на YouTube**  
   Ролики уходят на канал (по умолчанию — private).

5. **Удаляем локальные файлы**  
   После успешной загрузки временные склейки на Mac освобождают место.

6. **Пишем информацию на флешку**  
   На карте в папке `.70mai/` сохраняются статус загрузок, ссылки на YouTube и краткий отчёт — можно продолжить на другом Mac.

---

## Как запустить

Нужны: Mac, Python 3.10+, ffmpeg, вставленная SD-карта 70mai.

Первый раз в папке проекта:

```bash
scripts/setup-venv.sh
```

Обычный запуск (ждёт флешку, делает все шаги выше):

```bash
./scripts/publish_all_70mai.sh --wait
```

С **watchdog** — то же самое, но при сбое перезапускает автопилот сам:

```bash
./scripts/watch_publish_all_70mai.sh --wait
```

Карта уже вставлена (без ожидания):

```bash
./scripts/publish_all_70mai.sh
./scripts/watch_publish_all_70mai.sh
```

Прогресс в другом окне терминала:

```bash
./scripts/autopilot_dashboard.sh
```

---

## Первый запуск YouTube

Один раз: положите OAuth-файл Google в `~/.config/70mai/youtube_credentials.json` и при первом upload откройте браузер для входа.

Автопилот сам создаст на флешке `.70mai/` (токен и статусы) — подробности в [детальное_описание.md](детальное_описание.md#youtube-oauth-one-time).

---

## Полезное

| Действие | Команда |
|----------|---------|
| Посмотреть, что на карте | `python3 import_70mai.py --scan` |
| Только план, без записи | `./scripts/publish_all_70mai.sh --dry-run` |
| Запуск с watchdog | `./scripts/watch_publish_all_70mai.sh --wait` |
| Лог автопилота | `tail -f video/Output/.publish_tmp/publish_all.log` |
| Лог watchdog | `tail -f video/Output/.publish_tmp/publish_all_watchdog.log` |
| Отчёт по карте (MD/CSV) | `./scripts/generate_card_reports.sh` |
| Тюнинг import на ходу | Править [`70mai_runtime.json`](70mai_runtime.json) (`chunk_clips`, `stage_batch_clips`, …) |

Цели проекта: [GOALS.md](GOALS.md). Технические детали: [детальное_описание.md](детальное_описание.md).
