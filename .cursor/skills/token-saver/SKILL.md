---
name: token-saver
description: >-
  Frugal mode: minimize tokens/context (targeted reads, subagent delegation,
  trimmed output, no re-quoting). Use only when the user explicitly asks for
  the token-saver skill or "frugal"/"экономный" mode.
disable-model-invocation: true
---

# Token Saver

Frugal mode. Finish the task with minimal context burn without losing quality.

## Per-action checklist

- [ ] Needed context already in chat? If yes — do not re-read.
- [ ] Large file? Read a range (`offset`/`limit`) or Grep for the symbol.
- [ ] Need search? Grep/Glob (or `rg`), not shell `grep -r`/`find`.
- [ ] Command may be noisy? Bound it (`| head`, `--tail`, `-n`, `wc -l`).
- [ ] Broad exploration? Delegate to subagent (Task) — return a compressed result.
- [ ] Reply: short, no preamble; large chunks as `path:lines`, not paste.
- [ ] Edits: small `StrReplace` per `context-hygiene` (≤ ~2KB `new_string`); no full-file `Write`.

## When to apply

Only on explicit user request (“frugal”, “экономный режим”, “береги токены”, token-saver).
Baseline habits are already in always-on `token-economy` + `context-hygiene` — this is the stricter mode.

## Details

Concrete tactics: [reference.md](reference.md).
