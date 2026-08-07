# Backlog: YouTube Upload Exit via FI VPS

**GitHub:** [#1](https://github.com/cuthbertnogood/70mai-project/issues/1)

Handoff from grilling: local Mac cannot reach YouTube; use a Finland FirstByte VPS as **Upload Exit** instead of routing all YouTube traffic through a general VPN.

Glossary: [`CONTEXT.md`](../../CONTEXT.md) — **Upload Exit**, **Proxy exit**, **Relay**, **Pipeline host**.

## Constraint

- Local host → YouTube is blocked/restricted.
- Infra: 2× VPS RU + 2× VPS FI (FirstByte). Prefer **FI** as exit to Google; RU is a poor Google exit.
- Today: Mac → YouTube Data API only (`lib/youtube_upload.py`); `trust_env=False` (system proxy ignored). No VPS hop in code.

## Decisions locked

1. Need EU/FI exit — local → Google unavailable.
2. Strategy **C**: **Proxy exit** first; **Relay** if proxy fails or compose∥upload overlap is needed.
3. First transport: **SSH SOCKS** (`ssh -D` on FI VPS).

No ADR yet — write one after Proxy exit is proven or Relay is chosen deliberately.

## Phases

### Phase 1 — Experiment (manual)

On a host that can SSH to FI FirstByte:

1. Pick one FI VPS; note IP, SSH user, disk/traffic limits.
2. Start SOCKS: `ssh -N -D 1080 user@FI_VPS_IP`
3. Disable Happ/VPN or split so it does not intercept the SOCKS path.
4. Probe: HTTPS to `www.googleapis.com` / `upload.googleapis.com` via SOCKS.
5. One private test upload (small MP4) via SOCKS; record MB/s.
6. Baselines to comment on [#1](https://github.com/cuthbertnogood/70mai-project/issues/1):
   - Mac → FI (`scp`/`rsync`) MB/s
   - Proxy exit → YouTube MB/s
   - Old VPN → YouTube MB/s (if still measurable)

### Phase 2 — Code (if Phase 1 OK)

- Explicit `--upload-socks` / `--upload-proxy` in `lib/youtube_upload.py` — **do not** enable global `trust_env=True`.
- Diagnostics + README; autopilot passes the flag.
- Keep OAuth on Mac (Proxy exit).

### Phase 3 — Relay fallback

- `rsync`/`scp` composed MP4 → FI.
- Run `youtube_upload` on VPS with OAuth token on VPS.
- Optional overlap: Mac compose next while FI uploads previous.

## Fog (open)

- Which of two FI VPS (IP, SSH user, disk/traffic).
- WireGuard after SOCKS succeeds?
- OAuth on Mac (Proxy) vs on VPS (Relay).

## Out of scope for this handoff commit

- Implementing Proxy exit / Relay in Python.
- SSH keys / VPS setup on the machine that wrote this doc.
- Autopilot changes until Phase 2.
