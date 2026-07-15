# Token Saver — reference

Apply selectively. Do not paste this file into replies.

## Reading files

- Size first: `wc -l path` (or IDE metadata), then decide.
- Large file → `offset`/`limit`, not whole-file Read.
- Definition/usage → Grep the symbol instead of reading the file.
- Do not re-read the same file; keep already-seen lines in mind.

## Search

- Path/name → Glob (`**/*.py`).
- Content → Grep tool or terminal `rg`.
- Avoid shell `grep -r`/`find` for tree walks — noisy and expensive.
- Narrow: `rg -l`, `rg -c`, `rg --max-columns 200`.

## Shell output

- Logs/lists: `--tail 50`, `tail -n 50`, `ls` without `-R`; trees → `tree -L 2`.
- Estimate size first: `... | wc -l` before dumping everything.
- No `cat` on large files — Read with a range.
- Compose/import dry-runs and ffmpeg logs: prefer last N lines or failure slices.

## Delegation

- Broad questions (“how does X work”, “where is Y used”) → Task/`explore`.
- Ask the subagent for a compressed return format (paths + line refs + short conclusion).

## Replies

- No preamble or task restatement.
- Large code/logs → `path:lines`, not verbatim paste.
- Do not repeat what you already showed.

## Secrets

- Do not dump `.env` or credential files wholesale.
- Load only what you need; never echo secret values into the chat.

## Edits (see also `context-hygiene`)

- Small `StrReplace` (~500 tokens / ~2KB `new_string` max preferred).
- Never `Write` the full body of an existing file.
- Batch README/GOALS once at end of a feature (auto-documentation rule).

## Custom Mode “Frugal” (optional UI)

Cursor: Settings → Chat/Modes → New custom mode.

- Name: `Frugal`.
- Tools: keep Read, Grep/Glob/Search, Edit; limit free terminal if desired.
- Model: cheaper/faster for simple tasks.
- System instruction: follow `token-economy` strictly — targeted reads, subagent for heavy exploration, trimmed output, short answers.
