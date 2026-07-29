# Skills (mattpocock/skills)

Установленные навыки из https://github.com/mattpocock/skills

## grill-me + grilling

**grill-me** — relentless interview для sharpen плана или дизайна.
Запускает сессию grilling.

**grilling** — базовый движок: задаёт вопросы один за другим, разбирает ветви решений, ждёт подтверждения перед действиями.

### Как использовать
- Скажи: `Запусти grill-me` + описание задачи/плана
- Или: `/grill-me` + контекст
- Идеально для:
  - Планирования новых фич в 70mai
  - Рефакторинга пайплайна
  - Принятия архитектурных решений
  - Разбора сложных багов перед фиксом

### Файлы
- `grill-me/SKILL.md`
- `grilling/SKILL.md`
- `.../agents/openai.yaml` (метаданные)

Эту файлы взяты напрямую из репозитория и скопированы в проект (как рекомендует автор для tinkerers).

## diagnosing-bugs

**diagnosing-bugs** — Disciplined diagnosis loop для hard bugs и performance regressions.

Фазы:
1. Build a tight feedback loop (самая важная!)
2. Reproduce + minimise
3. Hypothesise (3–5 ranked гипотез)
4. Instrument
5. Fix + regression test
6. Cleanup + post-mortem

### Когда использовать
- Скажи: `diagnose this`, `debug this`, `/diagnosing-bugs`
- Или агент сам подхватит при сообщении "сломалось", "падает", "медленно", "ошибка"

Идеально для:
- Отладки пайплайна 70mai (import, publish, ffmpeg, staging)
- Performance regressions
- Flaky bugs
- Сложных багов, где нет очевидного виновника

Есть шаблон: `scripts/hitl-loop.template.sh` (для human-in-the-loop случаев).

Рекомендуется также рассмотреть `grill-with-docs` и `setup-matt-pocock-skills` позже, если нужно больше инструментов.