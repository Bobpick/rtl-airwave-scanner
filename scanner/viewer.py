from __future__ import annotations

import argparse
from pathlib import Path

from flask import Flask, abort, jsonify, render_template_string, request, send_file

from scanner.config import Config
from scanner.recorder import TransmissionLog
from scanner.squelch import DEFAULTS, LIMITS, apply_to_config, load_squelch, save_squelch

PAGE = r"""
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1"/>
  <title>Airwave recordings</title>
  <style>
    :root {
      --bg: #0f1419; --panel: #1a2332; --text: #e7ecf3; --muted: #8b9bb4;
      --acc: #3d9cf0; --ok: #3ecf8e; --bad: #f07178; --line: #2a3548;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0; font-family: "Segoe UI", system-ui, sans-serif;
      background: var(--bg); color: var(--text);
    }
    header {
      padding: 1rem 1.25rem; border-bottom: 1px solid var(--line);
      display: flex; flex-wrap: wrap; gap: 1rem; align-items: center;
      background: linear-gradient(180deg, #152033, var(--bg));
      position: sticky; top: 0; z-index: 5;
    }
    h1 { font-size: 1.15rem; margin: 0; font-weight: 600; letter-spacing: .02em; }
    .stats { display: flex; gap: .75rem; flex-wrap: wrap; color: var(--muted); font-size: .9rem; }
    .stats b { color: var(--text); }
    .channels {
      margin: 0 1.25rem .75rem; padding: .65rem 1rem; background: var(--panel);
      border: 1px solid var(--line); border-radius: 10px; font-size: .85rem;
      color: var(--muted); display: flex; flex-wrap: wrap; gap: .5rem 1.25rem;
    }
    .channels strong { color: var(--text); font-weight: 600; }
    .channels .mhz { color: #9fd0ff; font-weight: 600; }
    .squelch {
      margin: 0 1.25rem 1rem; padding: 1rem 1.1rem; background: var(--panel);
      border: 1px solid var(--line); border-radius: 10px;
    }
    .squelch h2 {
      margin: 0 0 .75rem; font-size: .95rem; font-weight: 600; color: var(--text);
      display: flex; align-items: center; gap: .75rem; flex-wrap: wrap;
    }
    .squelch h2 .hint { color: var(--muted); font-weight: 400; font-size: .8rem; }
    .squelch h2 .saved { color: var(--ok); font-size: .8rem; font-weight: 500; opacity: 0; transition: opacity .2s; }
    .squelch h2 .saved.show { opacity: 1; }
    .sliders { display: grid; grid-template-columns: repeat(auto-fit, minmax(240px, 1fr)); gap: .85rem 1.5rem; }
    .slider-row label {
      display: flex; justify-content: space-between; font-size: .82rem; color: var(--muted); margin-bottom: .25rem;
    }
    .slider-row label b { color: var(--text); font-variant-numeric: tabular-nums; }
    .slider-row input[type=range] {
      width: 100%; accent-color: var(--acc); cursor: pointer;
    }
    .slider-row .scale { display: flex; justify-content: space-between; font-size: .7rem; color: var(--muted); margin-top: .1rem; }
    .controls { display: flex; gap: .5rem; flex-wrap: wrap; margin-left: auto; }
    select, button, input {
      background: var(--panel); color: var(--text); border: 1px solid var(--line);
      border-radius: 8px; padding: .4rem .65rem; font-size: .9rem;
    }
    button { cursor: pointer; }
    button:hover { border-color: var(--acc); }
    main { padding: 0 1.25rem 3rem; }
    table { width: 100%; border-collapse: collapse; font-size: .9rem; }
    th, td { padding: .55rem .5rem; border-bottom: 1px solid var(--line); text-align: left; vertical-align: middle; }
    th { color: var(--muted); font-weight: 500; position: sticky; top: 64px; background: var(--bg); }
    tr:hover td { background: #152033; }
    .mhz { font-variant-numeric: tabular-nums; font-weight: 600; color: #9fd0ff; }
    .tag {
      display: inline-block; padding: .12rem .45rem; border-radius: 999px;
      font-size: .75rem; font-weight: 600;
    }
    .tag.ok { background: rgba(62,207,142,.15); color: var(--ok); }
    .tag.bad { background: rgba(240,113,120,.15); color: var(--bad); }
    .muted { color: var(--muted); font-size: .82rem; }
    audio { width: 180px; height: 32px; vertical-align: middle; }
    .empty { color: var(--muted); padding: 2rem; text-align: center; }
    .del { color: var(--bad); border-color: transparent; background: transparent; }
  </style>
</head>
<body>
  <header>
    <h1>Airwave scanner</h1>
    <div class="stats" id="stats">Loading…</div>
    <div class="controls">
      <select id="quality">
        <option value="accepted">Accepted only</option>
        <option value="">All (incl. rejected)</option>
        <option value="rejected">Rejected only</option>
      </select>
      <select id="band"><option value="">All bands</option></select>
      <button id="refresh">Refresh</button>
      <button id="purge" title="Delete all rejected rows">Purge rejected</button>
    </div>
  </header>

  <div class="squelch">
    <h2>
      Band groups
      <span class="hint">Toggle live — ATC off by default so ham/GMRS get airtime</span>
      <span class="saved" id="bg-saved">saved</span>
    </h2>
    <div class="toggles" id="band-toggles" style="display:flex;flex-wrap:wrap;gap:.75rem 1.25rem;margin-bottom:1rem;">
      <label style="display:flex;align-items:center;gap:.4rem;cursor:pointer;font-size:.9rem;">
        <input type="checkbox" id="enable_atc"/> <strong>ATC</strong> <span class="muted">airband voice</span>
      </label>
      <label style="display:flex;align-items:center;gap:.4rem;cursor:pointer;font-size:.9rem;">
        <input type="checkbox" id="enable_ham" checked/> <strong>Ham</strong> <span class="muted">2m / 70cm / repeater</span>
      </label>
      <label style="display:flex;align-items:center;gap:.4rem;cursor:pointer;font-size:.9rem;">
        <input type="checkbox" id="enable_gmrs" checked/> <strong>GMRS/FRS</strong>
      </label>
      <label style="display:flex;align-items:center;gap:.4rem;cursor:pointer;font-size:.9rem;">
        <input type="checkbox" id="enable_murs" checked/> <strong>MURS</strong>
      </label>
      <label style="display:flex;align-items:center;gap:.4rem;cursor:pointer;font-size:.9rem;">
        <input type="checkbox" id="enable_marine" checked/> <strong>Marine</strong>
      </label>
    </div>
    <h2>
      Squelch
      <span class="hint">Higher = less static, fewer clips · applies live to running scanner</span>
      <span class="saved" id="sq-saved">saved</span>
    </h2>
    <div class="sliders">
      <div class="slider-row">
        <label>RF SNR threshold <b id="v-snr">12.0</b> dB</label>
        <input type="range" id="snr_threshold_db" min="4" max="35" step="0.5" value="12"/>
        <div class="scale"><span>open 4</span><span>tight 35</span></div>
      </div>
      <div class="slider-row">
        <label>Min voice score <b id="v-voice">0.25</b> <span class="muted">(≤0.30 = loose / save more)</span></label>
        <input type="range" id="min_voice_score" min="0.10" max="0.95" step="0.01" value="0.25"/>
        <div class="scale"><span>open 0.10</span><span>tight 0.95</span></div>
      </div>
      <div class="slider-row">
        <label>Min activity <b id="v-act">4</b>%</label>
        <input type="range" id="min_activity_ratio" min="0.01" max="0.50" step="0.01" value="0.04"/>
        <div class="scale"><span>open 1%</span><span>tight 50%</span></div>
      </div>
      <div class="slider-row">
        <label>Min dynamic range <b id="v-dyn">4.0</b> dB</label>
        <input type="range" id="min_dynamic_range_db" min="1" max="20" step="0.5" value="4"/>
        <div class="scale"><span>open 1</span><span>tight 20</span></div>
      </div>
    </div>
  </div>

  <div class="channels" id="channels">Loading channels…</div>
  <main>
    <table>
      <thead>
        <tr>
          <th>When (UTC)</th>
          <th>Freq / channel</th>
          <th>Band</th>
          <th>Mod</th>
          <th>Dur</th>
          <th>SNR</th>
          <th>Voice</th>
          <th>Quality</th>
          <th>Play</th>
          <th></th>
        </tr>
      </thead>
      <tbody id="rows"><tr><td colspan="10" class="empty">Loading…</td></tr></tbody>
    </table>
  </main>
  <script>
    const $ = (id) => document.getElementById(id);
    let saveTimer = null;

    function fmtTime(iso) {
      if (!iso) return '—';
      const d = new Date(iso);
      return d.toISOString().replace('T', ' ').replace(/\.\d+Z$/, 'Z');
    }
    function fmtDur(s) {
      s = Number(s) || 0;
      if (s < 60) return s.toFixed(1) + 's';
      return Math.floor(s/60) + 'm ' + (s%60).toFixed(0) + 's';
    }

    function updateLabels() {
      $('v-snr').textContent = Number($('snr_threshold_db').value).toFixed(1);
      $('v-voice').textContent = Number($('min_voice_score').value).toFixed(2);
      $('v-act').textContent = Math.round(Number($('min_activity_ratio').value) * 100);
      $('v-dyn').textContent = Number($('min_dynamic_range_db').value).toFixed(1);
    }

    async function loadSquelch() {
      const s = await (await fetch('/api/squelch')).json();
      $('snr_threshold_db').value = s.snr_threshold_db;
      $('min_voice_score').value = s.min_voice_score;
      $('min_activity_ratio').value = s.min_activity_ratio;
      $('min_dynamic_range_db').value = s.min_dynamic_range_db;
      $('enable_atc').checked = !!s.enable_atc;
      $('enable_ham').checked = s.enable_ham !== false;
      $('enable_gmrs').checked = s.enable_gmrs !== false;
      $('enable_murs').checked = s.enable_murs !== false;
      $('enable_marine').checked = s.enable_marine !== false;
      updateLabels();
    }

    async function saveSquelch(fromBands) {
      updateLabels();
      const body = {
        snr_threshold_db: Number($('snr_threshold_db').value),
        min_voice_score: Number($('min_voice_score').value),
        min_activity_ratio: Number($('min_activity_ratio').value),
        min_dynamic_range_db: Number($('min_dynamic_range_db').value),
        enable_atc: $('enable_atc').checked,
        enable_ham: $('enable_ham').checked,
        enable_gmrs: $('enable_gmrs').checked,
        enable_murs: $('enable_murs').checked,
        enable_marine: $('enable_marine').checked,
      };
      await fetch('/api/squelch', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      });
      const el = $(fromBands ? 'bg-saved' : 'sq-saved');
      el.classList.add('show');
      setTimeout(() => el.classList.remove('show'), 1200);
    }

    function scheduleSave() {
      updateLabels();
      clearTimeout(saveTimer);
      saveTimer = setTimeout(() => saveSquelch(false), 200);
    }

    ['snr_threshold_db','min_voice_score','min_activity_ratio','min_dynamic_range_db'].forEach(id => {
      $(id).addEventListener('input', scheduleSave);
    });
    ['enable_atc','enable_ham','enable_gmrs','enable_murs','enable_marine'].forEach(id => {
      $(id).addEventListener('change', () => saveSquelch(true));
    });

    async function loadStats() {
      const s = await (await fetch('/api/stats')).json();
      $('stats').innerHTML = `
        <span>Total <b>${s.total}</b></span>
        <span>Accepted <b style="color:var(--ok)">${s.accepted}</b></span>
        <span>Rejected <b style="color:var(--bad)">${s.rejected}</b></span>
      `;
      const bandSel = $('band');
      const cur = bandSel.value;
      bandSel.innerHTML = '<option value="">All bands</option>';
      (s.bands || []).forEach(b => {
        if (!b.name) return;
        const o = document.createElement('option');
        o.value = b.name; o.textContent = `${b.name} (${b.count})`;
        bandSel.appendChild(o);
      });
      bandSel.value = cur;
    }

    async function loadChannels() {
      const ch = await (await fetch('/api/channels')).json();
      const el = $('channels');
      if (!ch.length) { el.textContent = 'Ham / GMRS / MURS labels from config.'; return; }
      el.innerHTML = ch.map(c =>
        `<span><span class="mhz">${c.frequency_mhz.toFixed(3)}</span> <strong>${c.name}</strong></span>`
      ).join('');
    }

    async function loadRows() {
      const q = $('quality').value;
      const band = $('band').value;
      const params = new URLSearchParams({ limit: '300' });
      if (q) params.set('quality', q);
      if (band) params.set('band', band);
      const rows = await (await fetch('/api/transmissions?' + params)).json();
      const tbody = $('rows');
      if (!rows.length) {
        tbody.innerHTML = '<tr><td colspan="10" class="empty">No transmissions yet. Run <code>./run.sh</code>.</td></tr>';
        return;
      }
      tbody.innerHTML = rows.map(r => {
        const qtag = (r.quality === 'rejected')
          ? `<span class="tag bad">rejected</span><div class="muted">${r.quality_reason || ''}</div>`
          : `<span class="tag ok">accepted</span>`;
        const audio = r.audio_file
          ? `<audio controls preload="none" src="/audio/${r.id}"></audio>`
          : '<span class="muted">—</span>';
        const channel = r.notes
          ? `<div class="muted" style="max-width:16rem">${r.notes}</div>`
          : '';
        return `<tr>
          <td>
            <div>${fmtTime(r.start_utc)}</div>
            <div class="muted">→ ${fmtTime(r.end_utc)}</div>
          </td>
          <td>
            <div class="mhz">${(r.frequency_mhz || 0).toFixed(4)} <span class="muted">MHz</span></div>
            ${channel}
          </td>
          <td>${r.band_name || '—'}</td>
          <td>${(r.modulation || '').toUpperCase()}</td>
          <td>${fmtDur(r.duration_seconds)}</td>
          <td>
            <div>${(r.peak_snr_db ?? 0).toFixed(1)} dB</div>
            <div class="muted">avg ${(r.mean_snr_db ?? r.peak_snr_db ?? 0).toFixed(1)}</div>
          </td>
          <td>
            <div>${(r.voice_score ?? 0).toFixed(2)}</div>
            <div class="muted">act ${((r.activity_ratio ?? 0)*100).toFixed(0)}% · dyn ${(r.dynamic_range_db ?? 0).toFixed(1)} dB</div>
          </td>
          <td>${qtag}</td>
          <td>${audio}</td>
          <td><button class="del" data-id="${r.id}" title="Delete">✕</button></td>
        </tr>`;
      }).join('');

      tbody.querySelectorAll('.del').forEach(btn => {
        btn.onclick = async () => {
          if (!confirm('Delete this entry and its audio file?')) return;
          await fetch('/api/transmissions/' + btn.dataset.id, { method: 'DELETE' });
          loadStats(); loadRows();
        };
      });
    }

    async function refresh() { await loadStats(); await loadRows(); }
    $('refresh').onclick = refresh;
    $('quality').onchange = loadRows;
    $('band').onchange = loadRows;
    $('purge').onclick = async () => {
      if (!confirm('Delete all rejected transmissions (and their audio)?')) return;
      await fetch('/api/purge_rejected', { method: 'POST' });
      refresh();
    };
    loadSquelch();
    loadChannels();
    refresh();
    setInterval(refresh, 15000);
  </script>
</body>
</html>
"""


def create_app(cfg: Config) -> Flask:
    log = TransmissionLog(cfg.database, cfg.csv_path, cfg.output_dir)
    app = Flask(__name__)
    root = Path.cwd()
    squelch_path = Path(cfg.squelch_file)

    # Seed squelch file from config defaults if missing
    if not squelch_path.is_file():
        seed = dict(DEFAULTS)
        seed["snr_threshold_db"] = cfg.snr_threshold_db
        seed["min_voice_score"] = cfg.min_voice_score
        seed["min_activity_ratio"] = cfg.min_activity_ratio
        seed["min_dynamic_range_db"] = cfg.min_dynamic_range_db
        seed["min_speech_band_ratio"] = cfg.min_speech_band_ratio
        seed["min_audio_rms"] = cfg.min_audio_rms
        save_squelch(seed, squelch_path)

    @app.get("/")
    def index():
        return render_template_string(PAGE)

    @app.get("/api/squelch")
    def api_squelch_get():
        return jsonify(load_squelch(squelch_path))

    @app.post("/api/squelch")
    def api_squelch_set():
        from scanner.squelch import BOOL_DEFAULTS

        body = request.get_json(force=True, silent=True) or {}
        current = load_squelch(squelch_path)
        current.update({k: body[k] for k in DEFAULTS if k in body})
        current.update({k: body[k] for k in BOOL_DEFAULTS if k in body})
        saved = save_squelch(current, squelch_path)
        apply_to_config(cfg, saved)
        return jsonify(saved)

    @app.get("/api/squelch/limits")
    def api_squelch_limits():
        return jsonify({k: {"min": v[0], "max": v[1]} for k, v in LIMITS.items()})

    @app.get("/api/channels")
    def api_channels():
        return jsonify(
            [
                {
                    "name": ch.name,
                    "frequency_mhz": ch.frequency_hz / 1e6,
                    "kind": ch.kind,
                    "notes": ch.notes,
                }
                for ch in cfg.known_channels
            ]
        )

    @app.get("/api/stats")
    def api_stats():
        return jsonify(log.stats())

    @app.get("/api/transmissions")
    def api_list():
        quality = request.args.get("quality") or None
        band = request.args.get("band") or None
        limit = min(int(request.args.get("limit", 300)), 1000)
        offset = int(request.args.get("offset", 0))
        return jsonify(
            log.list_transmissions(quality=quality, band=band, limit=limit, offset=offset)
        )

    @app.get("/api/transmissions/<int:tx_id>")
    def api_one(tx_id: int):
        row = log.get(tx_id)
        if not row:
            abort(404)
        return jsonify(row)

    @app.delete("/api/transmissions/<int:tx_id>")
    def api_delete(tx_id: int):
        if not log.delete(tx_id, remove_file=True):
            abort(404)
        return jsonify({"ok": True})

    @app.post("/api/purge_rejected")
    def api_purge():
        rows = log.list_transmissions(quality="rejected", limit=5000)
        n = 0
        for r in rows:
            if log.delete(int(r["id"]), remove_file=True):
                n += 1
        return jsonify({"deleted": n})

    @app.get("/audio/<int:tx_id>")
    def audio(tx_id: int):
        row = log.get(tx_id)
        if not row or not row.get("audio_file"):
            abort(404)
        raw = Path(row["audio_file"])
        candidates = []
        if raw.is_absolute():
            candidates.append(raw)
        else:
            candidates.append((root / raw).resolve())
            candidates.append((Path.cwd() / raw).resolve())
            candidates.append(raw.resolve())
        path = next((p for p in candidates if p.is_file()), None)
        if path is None:
            abort(404)
        return send_file(
            path,
            mimetype="audio/wav",
            as_attachment=False,
            download_name=path.name,
            conditional=True,
            max_age=0,
        )

    return app


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Browse recorded transmissions in a web UI")
    parser.add_argument("-c", "--config", default="config.yaml")
    parser.add_argument("--host", default=None)
    parser.add_argument("--port", type=int, default=None)
    args = parser.parse_args(argv)

    cfg = Config.from_yaml(args.config)
    host = args.host or cfg.viewer_host
    port = args.port or cfg.viewer_port
    app = create_app(cfg)
    print(f"Open http://{host}:{port}/")
    app.run(host=host, port=port, debug=False, threaded=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
