# Skills (mattpocock/skills)

Навыки из https://github.com/mattpocock/skills

**Важно:** Cursor читает skills только из `~/.cursor/skills/` (глобально) и `.cursor/skills/` (проект). Копии в этом каталоге `skills/` — источник/зеркало, сами по себе не активируются.

## Установлено глобально (`~/.cursor/skills/`)

| Skill | Вызов | Назначение |
|-------|--------|------------|
| **grill-me** | `/grill-me` | Relentless interview плана/дизайна |
| **grilling** | (движок) | Вопрос за вопросом; за ним стоят grill-* |
| **grill-with-docs** | `/grill-with-docs` | То же + `CONTEXT.md` / ADR via domain-modeling |
| **domain-modeling** | `/domain-modeling` | Глоссарий и ADR по ходу решений |
| **diagnosing-bugs** | `/diagnosing-bugs` | Дисциплинированный debug loop |
| **tdd** | `/tdd` | Red-green-refactor |
| **handoff** | `/handoff` | Сжать чат в handoff для нового агента |
| **setup-matt-pocock-skills** | `/setup-matt-pocock-skills` | Один раз на репо: tracker, labels, docs layout |

### Рекомендуемый порядок в 70mai

1. Один раз: `/setup-matt-pocock-skills`
2. Перед фичей: `/grill-with-docs` (предпочтительнее голого `/grill-me`)
3. Баги пайплайна: `/diagnosing-bugs`
4. Реализация: `/tdd`
5. Смена чата / context > 40%: `/handoff`

## Файлы в репо

Зеркало тех же skills в `skills/<name>/` (включая `agents/openai.yaml` где есть).
