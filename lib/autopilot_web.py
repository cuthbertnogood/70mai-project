#!/usr/bin/env python3
"""Localhost web Dashboard for Autopilot."""

from __future__ import annotations

import json
import sys
import threading
from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

from autopilot_control import (
    peek_control,
    read_run_state,
    write_run_state,
)

DEFAULT_PORT = 8787
DEFAULT_HOST = "127.0.0.1"


def _row_to_dict(row: Any) -> dict[str, Any]:
    data = asdict(row)
    for key in ("trip_start", "trip_end"):
        val = data.get(key)
        if val is not None and hasattr(val, "isoformat"):
            data[key] = val.isoformat(sep=" ", timespec="seconds")
    return data


def build_status_payload(
    *,
    temp_dir: Path,
    video_dir: Path,
    types: list[str],
    source: Path | None,
    min_free_gb: float,
) -> dict[str, Any]:
    from autopilot_dashboard import (
        chunk_summary_counts,
        free_disk_gb,
        list_pipeline_processes,
        resolve_live_status,
    )

    run_state = read_run_state(temp_dir)
    sd = None
    usage_total = 0

    def _format_gb(n: float) -> str:
        return f"{n / 1e9:.1f} GB"

    try:
        from publish_all_70mai import autopilot_disk_usage, find_sd_card, format_gb

        sd = find_sd_card()
        _format_gb = format_gb
        usage = autopilot_disk_usage(video_dir, temp_dir, types=types)
        usage_total = float(usage.get("total", 0))
    except Exception:
        pass

    if sd and not run_state.get("sd_path"):
        run_state = {**run_state, "sd_path": str(sd)}

    rows: list[Any] = []
    chunks_done = chunk_total = trips_done = 0
    live = resolve_live_status(temp_dir, rows=rows)
    failures: list[str] = []

    try:
        from autopilot_dashboard import Dashboard

        dash = Dashboard(
            temp_dir=temp_dir,
            video_dir=video_dir,
            check_disk=Path("."),
            min_free_gb=min_free_gb,
            source=source or sd,
            types=types,
            enabled=False,
        )
        dash.reload_plan_if_changed()
        if dash.rows:
            rows = dash.rows
            try:
                dash._refresh_from_publish_state()
                dash._refresh_from_status()
            except Exception:
                pass
            live = resolve_live_status(temp_dir, rows=rows)
            chunks_done, chunk_total, trips_done = chunk_summary_counts(rows)
        try:
            from autopilot_dashboard import collect_failure_lines

            failures = collect_failure_lines(temp_dir, source=source or sd)
        except Exception:
            failures = []
    except Exception:
        pass

    sd_card = {"present": False, "trips": []}
    try:
        from autopilot_sd_table import build_sd_card_payload

        sd_card = build_sd_card_payload(source or sd, types)
    except Exception:
        pass

    return {
        "run": run_state,
        "diagnostics": _read_diagnostics(temp_dir),
        "sd_present": sd is not None,
        "sd_path": str(sd) if sd else None,
        "sd_card": sd_card,
        "live": live,
        "summary": {
            "chunks_done": chunks_done,
            "chunk_total": chunk_total,
            "trips_done": trips_done,
            "trip_total": len(rows),
        },
        "disk": {
            "free_gb": round(free_disk_gb(Path(".")), 1),
            "video_total_gb": _format_gb(usage_total),
        },
        "rows": [_row_to_dict(r) for r in rows],
        "failures": failures,
        "processes": [
            {
                "pid": p.pid,
                "role": p.role,
                "uptime_sec": p.etime_sec,
                "tip": p.tip,
            }
            for p in list_pipeline_processes(temp_dir=temp_dir)
        ],
        "pending_control": peek_control(temp_dir),
    }


def _dashboard_html_bytes() -> bytes:
    return _DASHBOARD_HTML.encode("utf-8")


def _reload_dashboard_module():
    """Pick up HTML/API changes without restarting the Autopilot process."""
    import importlib

    return importlib.reload(sys.modules[__name__])


def _read_diagnostics(temp_dir: Path) -> dict[str, Any]:
    path = temp_dir / "autopilot_diagnostics.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


_DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="ru">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Autopilot</title>
  <style>
    :root { font-family: system-ui, sans-serif; background: #0f1419; color: #e7ecf3; }
    body { margin: 0; padding: 1rem 1.25rem 2rem; max-width: 1680px; }
    h1 { margin: 0 0 .25rem; font-size: 1.4rem; }
    h2 { margin: 0 0 .5rem; font-size: 1rem; color: #8b9bb4; font-weight: 600; }
    .sub { color: #8b9bb4; margin-bottom: 1rem; }
    .layout { display: grid; grid-template-columns: 1fr minmax(320px, 420px); gap: 1.25rem; align-items: start; }
    @media (max-width: 1100px) { .layout { grid-template-columns: 1fr; } }
    .sd-panel { background: #1a2332; border-radius: 8px; padding: .75rem 1rem; position: sticky; top: .5rem; max-height: calc(100vh - 2rem); overflow: auto; }
    .sd-meta { font-size: .78rem; color: #8b9bb4; margin-bottom: .6rem; line-height: 1.45; }
    .sd-table { font-size: .78rem; }
    .sd-table th, .sd-table td { padding: .3rem .35rem; }
    .bar { display: flex; flex-wrap: wrap; gap: .5rem; margin-bottom: 1rem; }
    button { border: 0; border-radius: 6px; padding: .45rem .9rem; cursor: pointer; font-weight: 600; }
    button.stop { background: #c0392b; color: #fff; }
    button.skip { background: #d68910; color: #111; }
    button.repair { background: #2874a6; color: #fff; }
    button.profile { background: #8e44ad; color: #fff; }
    button.quit { background: #566573; color: #fff; }
    button:disabled { opacity: .45; cursor: not-allowed; }
    .cards { display: grid; grid-template-columns: repeat(auto-fit,minmax(180px,1fr)); gap: .75rem; margin-bottom: 1rem; }
    .card { background: #1a2332; border-radius: 8px; padding: .75rem 1rem; }
    .card .k { color: #8b9bb4; font-size: .8rem; }
    .card .v { font-size: 1.1rem; font-weight: 600; }
    table { width: 100%; border-collapse: collapse; font-size: .85rem; }
    th, td { text-align: left; padding: .35rem .5rem; border-bottom: 1px solid #243044; }
    th { color: #8b9bb4; font-weight: 600; }
    .phase-compose { color: #5dade2; }
    .phase-upload { color: #58d68d; }
    .phase-import { color: #f5b041; }
    .phase-done { color: #58d68d; }
    .phase-stopped, .phase-error { color: #ec7063; }
    .failures { margin-top: 1rem; background: #1a2332; border-radius: 8px; padding: .75rem 1rem; font-size: .82rem; color: #f5b7b1; }
    a { color: #5dade2; }
    #msg { min-height: 1.2rem; color: #f8c471; margin-bottom: .5rem; }
  </style>
</head>
<body>
  <h1>Autopilot</h1>
  <div class="sub" id="subtitle">Загрузка…</div>
  <div id="msg"></div>
  <div class="bar">
    <button class="stop" id="btn-stop">Stop</button>
    <button class="skip" id="btn-skip">Skip chunk</button>
    <button class="repair" id="btn-repair">Repair</button>
    <button class="profile" id="btn-profile">Профилировать хост</button>
    <button class="quit" id="btn-quit">Quit</button>
  </div>
  <div class="cards" id="cards"></div>
  <div class="layout">
    <div class="main-col">
  <table>
    <thead><tr><th>Ролик</th><th>Тип</th><th>Trip</th><th>Статус</th><th>Прогресс</th><th>YouTube</th></tr></thead>
    <tbody id="rows"></tbody>
  </table>
  <div class="failures" id="failures" hidden></div>
    </div>
    <aside class="sd-panel">
      <h2>Флешка (SD)</h2>
      <div class="sd-meta" id="sd-meta">—</div>
      <table class="sd-table">
        <thead><tr><th>Тип</th><th>Поездка</th><th>Длит.</th><th>Место</th><th>Клипы</th></tr></thead>
        <tbody id="sd-trips"></tbody>
      </table>
    </aside>
  </div>
  <script>
    const msg = document.getElementById('msg');
    const phaseLabels = {
      waiting_card: 'Ожидание SD-карты',
      running: 'Прогон',
      restarting: 'Рестарт после сбоя',
      done: 'Готово',
      stopped: 'Остановлено',
      error: 'Ошибка',
      quitting: 'Выход…',
    };
    async function send(action) {
      msg.textContent = '…';
      const r = await fetch('/api/control', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({action}),
      });
      const j = await r.json();
      msg.textContent = j.message || (r.ok ? 'OK' : 'Ошибка');
    }
    document.getElementById('btn-stop').onclick = () => send('stop');
    document.getElementById('btn-skip').onclick = () => send('skip');
    document.getElementById('btn-repair').onclick = () => send('repair');
    document.getElementById('btn-profile').onclick = () => send('profile');
    document.getElementById('btn-quit').onclick = () => send('quit');
    function esc(s) { const d = document.createElement('div'); d.textContent = s; return d.innerHTML; }
    function render(data) {
      const run = data.run || {};
      const phase = run.phase || 'running';
      document.getElementById('subtitle').textContent =
        (phaseLabels[phase] || phase) + (run.message ? ' — ' + run.message : '');
      const canQuit = ['waiting_card','done','stopped','error'].includes(phase);
      const canControl = ['running','restarting','waiting_card'].includes(phase);
      document.getElementById('btn-quit').disabled = !canQuit;
      document.getElementById('btn-stop').disabled = !canControl || phase === 'waiting_card';
      document.getElementById('btn-skip').disabled = !canControl || phase === 'waiting_card';
      document.getElementById('btn-repair').disabled = !canControl || phase === 'waiting_card';
      const diagnostics = data.diagnostics || {};
      const profileButton = document.getElementById('btn-profile');
      profileButton.disabled = ['running','restarting','quitting'].includes(phase) ||
        diagnostics.status === 'running';
      const trace = diagnostics.trace || [];
      const lastTrace = trace.length ? trace[trace.length - 1] : null;
      const live = data.live || {};
      const cards = [
        ['SD', data.sd_present ? (data.sd_path || 'да') : 'нет'],
        ['Фаза', live.phase || '—'],
        ['Ролики', `${data.summary.chunks_done}/${data.summary.chunk_total}`],
        ['Trips', `${data.summary.trips_done}/${data.summary.trip_total}`],
        ['Диск свободно', `${data.disk.free_gb} GB`],
        ['Деталь', live.detail || '—'],
        ['Диагностика', diagnostics.message || diagnostics.status || '—'],
        ['Последний этап', lastTrace ? `${lastTrace.stage} ${lastTrace.elapsed_sec || ''}s` : '—'],
      ];
      document.getElementById('cards').innerHTML = cards.map(([k,v]) =>
        `<div class="card"><div class="k">${esc(k)}</div><div class="v">${esc(String(v))}</div></div>`
      ).join('');
      const tbody = document.getElementById('rows');
      tbody.innerHTML = (data.rows || []).map(r => {
        const yt = r.youtube_url ? `<a href="${esc(r.youtube_url)}" target="_blank">watch</a>` : '—';
        const roll = r.chunk_display_index ? `р${r.chunk_display_index}/${r.chunk_total}` : '?';
        return `<tr>
          <td>${esc(roll)}</td>
          <td>${esc(r.record_type)}</td>
          <td>${esc(r.label)}</td>
          <td class="phase-${esc(r.status)}">${esc(r.status)}</td>
          <td>${esc(r.progress || '—')}</td>
          <td>${yt}</td>
        </tr>`;
      }).join('');
      const fail = document.getElementById('failures');
      if (data.failures && data.failures.length) {
        fail.hidden = false;
        fail.innerHTML = '<strong>Сбои</strong><br>' + data.failures.map(esc).join('<br>');
      } else {
        fail.hidden = true;
      }
      const sd = data.sd_card || {};
      const sdMeta = document.getElementById('sd-meta');
      if (!sd.present) {
        sdMeta.textContent = 'Карта не подключена';
        document.getElementById('sd-trips').innerHTML = '';
      } else {
        const d = sd.disk || {};
        sdMeta.innerHTML = [
          esc(sd.path || ''),
          d.total ? `свободно ${esc(d.free || '—')} / ${esc(d.total)}` : '',
          sd.video_total && sd.video_total !== '—' ? `видео на карте: ${esc(sd.video_total)}` : '',
          sd.updated_at ? `инвентарь: ${esc(sd.updated_at)}` : '',
        ].filter(Boolean).join('<br>');
        document.getElementById('sd-trips').innerHTML = (sd.trips || []).map(t =>
          `<tr>
            <td>${esc(t.record_type)}</td>
            <td title="${esc(t.start || '')} → ${esc(t.end || '')}">${esc(t.label)}</td>
            <td>${esc(t.duration)}</td>
            <td>${esc(t.size)}</td>
            <td>${esc(String(t.clip_count))}</td>
          </tr>`
        ).join('') || '<tr><td colspan="5">нет данных</td></tr>';
      }
    }
    async function tick() {
      try {
        const r = await fetch('/api/status');
        render(await r.json());
      } catch (e) {
        document.getElementById('subtitle').textContent = 'Нет связи с Autopilot';
      }
    }
    setInterval(tick, 1000);
    tick();
  </script>
</body>
</html>
"""


class AutopilotWebServer:
    """Threaded HTTP server bound to loopback only."""

    def __init__(
        self,
        *,
        host: str,
        port: int,
        temp_dir: Path,
        video_dir: Path,
        types: list[str],
        min_free_gb: float,
        on_control: Callable[[str, dict[str, Any]], str],
        quit_event: threading.Event,
        manage_run_state: bool = True,
    ) -> None:
        self._host = host
        self._port = port
        self._temp_dir = temp_dir
        self._video_dir = video_dir
        self._types = types
        self._min_free_gb = min_free_gb
        self._on_control = on_control
        self._quit_event = quit_event
        self._manage_run_state = manage_run_state
        self._httpd: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self._source: Path | None = None

    @property
    def url(self) -> str:
        return f"http://{self._host}:{self._port}/"

    def set_source(self, source: Path | None) -> None:
        self._source = source

    def start(self) -> None:
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, fmt: str, *args: Any) -> None:
                return

            def _json(self, code: int, payload: dict[str, Any]) -> None:
                body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self) -> None:
                aw = _reload_dashboard_module()
                path = urlparse(self.path).path
                if path in ("/", "/index.html"):
                    body = aw._dashboard_html_bytes()
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.send_header("Cache-Control", "no-store")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                if path == "/api/status":
                    payload = aw.build_status_payload(
                        temp_dir=outer._temp_dir,
                        video_dir=outer._video_dir,
                        types=outer._types,
                        source=outer._source,
                        min_free_gb=outer._min_free_gb,
                    )
                    self._json(200, payload)
                    return
                self.send_error(404)

            def do_POST(self) -> None:
                path = urlparse(self.path).path
                if path != "/api/control":
                    self.send_error(404)
                    return
                length = int(self.headers.get("Content-Length", "0") or 0)
                raw = self.rfile.read(length) if length else b"{}"
                try:
                    data = json.loads(raw.decode("utf-8"))
                except ValueError:
                    self._json(400, {"ok": False, "message": "invalid JSON"})
                    return
                action = str(data.get("action") or "")
                message = outer._on_control(action, data)
                self._json(200, {"ok": True, "message": message})

        self._httpd = ThreadingHTTPServer((self._host, self._port), Handler)
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self._thread.start()
        if self._manage_run_state:
            write_run_state(
                self._temp_dir,
                phase="waiting_card",
                message="ожидание 70mai SD",
                dashboard_url=self.url,
            )

    def stop(self) -> None:
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd.server_close()
            self._httpd = None
        if self._thread is not None:
            self._thread.join(timeout=3)
            self._thread = None
