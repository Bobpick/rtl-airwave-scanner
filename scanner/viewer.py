from __future__ import annotations

import argparse
import os
import signal
import threading
import time
from pathlib import Path

from flask import Flask, Response, abort, jsonify, render_template_string, request, send_file

from scanner.config import Config
from scanner.live_state import read_live_state
from scanner.recorder import TransmissionLog
from scanner.squelch import BOOL_DEFAULTS, DEFAULTS, apply_to_config, load_squelch, save_squelch

PAGE = r"""
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1"/>
  <title>RTL Airwave Scanner</title>
  <style>
    :root {
      --bg: #050b13;
      --panel: #0d1725;
      --panel: #0d1725;
      --panel-deep: #09121e;
      --panel-top: #122033;
      --text: #dfe8f3;
      --muted: #91a1b5;
      --faint: #52657d;
      --line: #26384b;
      --line-soft: rgba(93, 126, 158, .20);
      --blue: #258ce0;
      --blue-bright: #43b7ff;
      --green: #38c876;
      --yellow: #e6b735;
      --purple: #9878e8;
      --pink: #e36bb5;
      --ok: #45d16a;
    }
    * { box-sizing: border-box; }
    html { background: #03070d; }
    body {
      margin: 0;
      min-height: 100vh;
      color: var(--text);
      font-family: "Segoe UI", system-ui, -apple-system, sans-serif;
      font-size: 13px;
      background:
        radial-gradient(900px 500px at 0% 0%, rgba(30, 72, 107, .35), transparent 62%),
        radial-gradient(700px 460px at 100% 0%, rgba(14, 49, 77, .28), transparent 65%),
        repeating-linear-gradient(115deg, rgba(255,255,255,.018) 0 1px, transparent 1px 90px),
        #050a11;
    }
    .shell {
      width: min(1400px, calc(100vw - 34px));
      margin: 14px auto 28px;
    }
    .card {
      position: relative;
      overflow: hidden;
      border: 1px solid #31465b;
      border-radius: 17px;
      background: linear-gradient(180deg, rgba(17, 31, 47, .98), rgba(8, 17, 28, .99));
      box-shadow:
        0 0 0 3px rgba(3, 9, 15, .78),
        0 0 0 5px rgba(34, 53, 69, .42),
        0 24px 70px rgba(0,0,0,.62),
        inset 0 1px rgba(255,255,255,.08);
    }
    .topbar {
      min-height: 48px;
      display: flex;
      align-items: center;
      gap: 1rem;
      padding: .65rem 1.25rem;
      border-bottom: 1px solid #2a3e52;
      background: linear-gradient(180deg, rgba(27, 44, 61, .72), rgba(8, 17, 28, .75));
      box-shadow: inset 0 -1px rgba(0,0,0,.55);
    }
    .brand {
      color: #eef5fb;
      font-size: 1.03rem;
      font-weight: 700;
      letter-spacing: .045em;
      white-space: nowrap;
      text-shadow: 0 1px 2px #000;
    }
    .meta {
      display: flex;
      flex: 1;
      justify-content: flex-end;
      align-items: center;
      flex-wrap: wrap;
      gap: .7rem 1.65rem;
      color: #aab7c6;
      font-size: .72rem;
      letter-spacing: .025em;
      white-space: nowrap;
    }
    .meta b { color: #e7edf4; font-weight: 600; }
    .meta .mode { color: #71d492; }
    .dot {
      width: 9px; height: 9px; display: inline-block;
      margin-left: .42rem; vertical-align: -1px;
      border-radius: 50%; background: var(--ok);
      box-shadow: 0 0 9px rgba(69, 209, 106, .9);
    }
    .dot.off { background: #ec5d69; box-shadow: 0 0 9px #ec5d69; }

    .body {
      display: grid;
      grid-template-columns: 218px minmax(0, 1fr);
      min-height: 580px;
    }
    @media (max-width: 900px) {
      .shell { width: calc(100vw - 16px); margin-top: 8px; }
      .body { grid-template-columns: 1fr; }
      .sidebar { border-right: 0 !important; border-bottom: 1px solid var(--line); }
      .meta { justify-content: flex-start; }
    }

    .sidebar {
      padding: .9rem .85rem 1rem;
      border-right: 1px solid #2a3c50;
      background: linear-gradient(180deg, rgba(8, 18, 29, .96), rgba(5, 13, 22, .99));
    }
    .sidebar h3 {
      margin: 0 0 .64rem;
      color: #dce6ef;
      font-size: .68rem;
      font-weight: 600;
      letter-spacing: .10em;
    }
    .sq-row { margin-bottom: .68rem; }
    .sq-row label {
      display: flex;
      align-items: baseline;
      justify-content: space-between;
      gap: .4rem;
      margin-bottom: .23rem;
      color: #d4dee8;
      font-size: .68rem;
      line-height: 1.1;
      white-space: nowrap;
    }
    .sq-row label span.range { color: #bcc8d4; }
    .sq-row label b {
      color: #edf4fb;
      font-size: .67rem;
      font-weight: 500;
      font-variant-numeric: tabular-nums;
    }
    .sq-row input[type=range] {
      width: 100%;
      height: 4px;
      margin: 0;
      cursor: pointer;
      accent-color: var(--blue);
    }
    .sq-row input[type=range]::-webkit-slider-runnable-track {
      height: 3px; border-radius: 4px; background: #405365;
    }
    .sq-row input[type=range]::-webkit-slider-thumb {
      width: 11px; height: 11px; margin-top: -4px;
      border: 0; border-radius: 50%; background: var(--blue-bright);
      box-shadow: 0 0 5px rgba(67,183,255,.65);
      appearance: none;
    }
    .sq-row.ham input { accent-color: var(--green); }
    .sq-row.gmrs input { accent-color: var(--yellow); }
    .sq-row.marine input { accent-color: var(--purple); }
    .sq-row.murs input { accent-color: var(--pink); }
    .saved {
      height: .8rem;
      color: #55da7c;
      font-size: .65rem;
      opacity: 0;
      transition: opacity .2s;
    }
    .saved.show { opacity: 1; }
    .groups {
      margin-top: .42rem;
      padding-top: .72rem;
      border-top: 1px solid var(--line-soft);
    }
    .groups label {
      display: flex;
      align-items: center;
      gap: .5rem;
      margin: .32rem 0;
      color: #d6e0e9;
      font-size: .74rem;
      cursor: pointer;
    }
    .groups input {
      width: 14px; height: 14px; margin: 0;
      accent-color: var(--blue);
    }
    .groups label.visual-only {
      opacity: .72;
      cursor: default;
    }
    .adv {
      margin-top: .8rem;
      padding-top: .65rem;
      border-top: 1px solid var(--line-soft);
      color: var(--muted);
      font-size: .68rem;
    }
    .adv summary { cursor: pointer; color: #aab9c8; }
    .adv select { width: 100%; }

    .main {
      position: relative;
      min-width: 0;
      padding: .75rem .78rem .85rem;
      background: linear-gradient(180deg, rgba(9, 20, 32, .72), rgba(5, 13, 22, .45));
    }
    .legend {
      display: flex;
      flex-wrap: wrap;
      align-items: center;
      gap: .4rem;
      margin: 0 0 .25rem;
      position: static;
      pointer-events: auto;
    }
    .main {
      min-width: 0; /* allow flex children to shrink without overflow */
    }
    .pill {
      display: inline-flex;
      align-items: center;
      justify-content: center;
      min-width: 84px;
      height: 20px;
      padding: .12rem .6rem;
      border: 1px solid rgba(255,255,255,.08);
      border-radius: 3px;
      color: #dce8f0;
      background: rgba(36, 82, 119, .9);
      box-shadow: inset 0 1px rgba(255,255,255,.12), 0 1px 2px #000;
      font-size: .67rem;
      letter-spacing: .025em;
    }
    .pill i { display: none; }
    .pill.atc { background: rgba(30, 104, 162, .86); }
    .pill.ham { background: rgba(25, 132, 73, .86); }
    .pill.gmrs { background: rgba(155, 119, 18, .90); }
    .pill.marine { background: rgba(92, 52, 140, .88); }
    .pill.murs { background: rgba(130, 50, 104, .88); }
    .pill.public { background: rgba(155, 72, 48, .88); }
    .status-row {
      display: flex;
      flex-wrap: wrap;
      gap: .35rem .85rem;
      align-items: baseline;
      padding: .15rem 0 .35rem;
      font-size: .72rem;
      color: #8092a6;
      line-height: 1.35;
    }
    .status-row b { color: #d5e0ec; font-weight: 600; }
    .status-row .sep { color: #3a4d63; }
    #plan-chips {
      display: flex;
      flex-wrap: wrap;
      gap: .3rem;
      max-height: 4.5rem;
      overflow-y: auto;
      padding: .15rem 0 .4rem;
      margin-bottom: .15rem;
    }
    #plan-chips .chip {
      display: inline-flex;
      align-items: center;
      border: 1px solid #2a3d52;
      border-radius: 999px;
      padding: .12rem .45rem;
      font-size: .65rem;
      color: #9aabc0;
      background: rgba(0,0,0,.2);
      white-space: nowrap;
      line-height: 1.2;
    }
    #plan-chips .chip.active {
      color: #eef5ff;
      font-weight: 700;
      box-shadow: 0 0 0 1px rgba(61,156,240,.55);
      background: rgba(61,156,240,.12);
    }

    .viz {
      position: relative;
      overflow: hidden;
      border: 1px solid #263b50;
      border-radius: 4px;
      background: #050c15;
      box-shadow: inset 0 0 22px rgba(0,0,0,.45), 0 2px 5px rgba(0,0,0,.35);
    }
    .spectrum-wrap {
      position: relative;
      height: 164px;
      border-bottom: 1px solid #263b50;
      background: linear-gradient(180deg, rgba(9,23,37,.96), rgba(3,10,18,.98));
    }
    #spectrum { width: 100%; height: 100%; display: block; }
    .axis-y {
      position: absolute;
      z-index: 2;
      left: 5px; top: 29px; bottom: 22px;
      display: flex;
      flex-direction: column;
      justify-content: space-between;
      color: #9baabc;
      font-size: .62rem;
      pointer-events: none;
    }
    .waterfall-wrap {
      position: relative;
      height: 103px;
      background: #04101b;
    }
    #waterfall {
      width: 100%; height: 100%; display: block;
      image-rendering: pixelated;
    }
    .axis-x {
      position: absolute;
      z-index: 3;
      left: 44px; right: 12px; bottom: 2px;
      display: flex;
      justify-content: space-between;
      padding: 0;
      color: #aeb9c5;
      font-size: .61rem;
      border: 0;
      pointer-events: none;
      text-shadow: 0 1px 2px #000;
    }
    .axis-x span { white-space: nowrap; }

    .table-wrap {
      max-height: 320px;
      margin-top: .62rem;
      overflow: auto;
      border: 1px solid #263b50;
      border-radius: 4px;
      background: rgba(3, 10, 17, .73);
    }
    table { width: 100%; border-collapse: collapse; font-size: .69rem; }
    th, td {
      padding: .48rem .55rem;
      text-align: left;
      white-space: nowrap;
      border-bottom: 1px solid rgba(45, 66, 85, .72);
    }
    th {
      position: sticky; top: 0; z-index: 2;
      color: #aebccc;
      background: #0b1725;
      font-size: .62rem;
      font-weight: 600;
      letter-spacing: .055em;
    }
    td { color: #c4d0dc; }
    tr:hover td { background: rgba(40, 139, 219, .10); }
    .tag {
      display: inline-block;
      min-width: 3.2rem;
      padding: .12rem .4rem;
      border-radius: 3px;
      text-align: center;
      font-size: .62rem;
      font-weight: 700;
      letter-spacing: .04em;
    }
    .tag.atc { color: #4db9ff; background: rgba(37,140,224,.13); }
    .tag.ham { color: #55d784; background: rgba(56,200,118,.13); }
    .tag.gmrs { color: #f1ca54; background: rgba(230,183,53,.13); }
    .tag.marine { color: #b69bff; background: rgba(152,120,232,.13); }
    .tag.murs { color: #ee83c3; background: rgba(227,107,181,.13); }
    .tag.public { color: #ff9f7c; background: rgba(227,107,95,.13); }
    .tag.other { color: #a9b7c5; background: rgba(148,163,184,.12); }
    .freq { color: #9bcfff; font-weight: 600; font-variant-numeric: tabular-nums; }
    .file a { color: #3b90d8; text-decoration: none; }
    .file a:hover { color: #75c1ff; text-decoration: underline; }
    .actions { display: flex; align-items: center; justify-content: flex-end; gap: .36rem; }
    .icon-btn {
      display: inline-flex;
      width: 23px; height: 23px;
      align-items: center; justify-content: center;
      padding: 0;
      border: 1px solid transparent;
      border-radius: 3px;
      color: #c5d0dc;
      background: transparent;
      cursor: pointer;
      font-size: .82rem;
      line-height: 1;
    }
    .icon-btn:hover { color: #fff; border-color: #3c86b9; background: rgba(48,131,190,.16); }
    .icon-btn.del { color: #798898; }
    .icon-btn.del:hover { color: #ff9a9a; border-color: #8f4f5b; }
    .icon-btn.download { color: #d2dbe4; text-decoration: none; }
    .icon-btn.play.active { color: #5ddf93; }
    .empty { color: var(--muted); text-align: center; padding: 1.5rem; }
    code { color: #86c7fa; }
    .shutdown-btn {
      margin-left: .75rem;
      padding: .45rem 1rem;
      border-radius: 8px;
      border: 1px solid #e07080;
      background: #b83a48;
      color: #fff;
      font-size: .82rem;
      font-weight: 700;
      cursor: pointer;
      letter-spacing: .04em;
      text-transform: uppercase;
      box-shadow: 0 2px 8px rgba(180,40,50,.4);
      flex-shrink: 0;
    }
    .shutdown-btn:hover {
      background: #d04555;
      color: #fff;
      border-color: #ff90a0;
    }
    .ham-sub {
      margin: .25rem 0 .55rem .85rem;
      padding-left: .65rem;
      border-left: 2px solid #2a4a6a;
      display: flex;
      flex-direction: column;
      gap: .22rem;
    }
    .ham-sub label { font-size: .8rem !important; }
    .shutdown-msg {
      min-height: 60vh; display: flex; align-items: center; justify-content: center;
      flex-direction: column; gap: .75rem; color: var(--muted); text-align: center;
      padding: 2rem;
    }
    .shutdown-msg h1 { color: var(--text); font-size: 1.25rem; margin: 0; }
  </style>
</head>
<body>
  <div class="shell">
    <div class="card">
      <div class="topbar">
        <div class="brand">RTL Airwave Scanner</div>
        <div class="meta">
          <span>OP: <b id="operator">—</b></span>
          <span>SITE: <b id="site">—</b></span>
          <span>MODE: <b class="mode" id="mode">—</b></span>
          <span>GAIN: <b id="gain">—</b></span>
          <span>UTC: <b id="utc">—</b><span class="dot" id="live-dot" title="live feed"></span></span>
          <button type="button" class="shutdown-btn" id="shutdown" title="Stop scanner and dashboard">Shutdown</button>
        </div>
      </div>

      <div class="body">
        <aside class="sidebar">
          <h3>WHICH BANDS TO SCAN</h3>
          <p style="margin:0 0 .75rem;font-size:.72rem;color:var(--muted);line-height:1.35;">
            One RTL-SDR = one ~2&nbsp;MHz window at a time. Checked bands are
            <b style="color:var(--text)">hopped in rotation</b>.
          </p>
          <div class="groups" style="margin-top:0;">
            <label><input type="checkbox" id="enable_atc"/> ATC <span class="muted">(118–137 AM)</span></label>
            <label><input type="checkbox" id="enable_ham" checked/> <strong>HAM</strong> <span class="muted">(all meters)</span></label>
            <div class="ham-sub" id="ham-meters">
              <label><input type="checkbox" id="enable_ham_10m" checked/> 10 m <span class="muted">(28–29.7)</span></label>
              <label><input type="checkbox" id="enable_ham_6m" checked/> 6 m <span class="muted">(50–54)</span></label>
              <label><input type="checkbox" id="enable_ham_2m" checked/> 2 m <span class="muted">(144–148)</span></label>
              <label><input type="checkbox" id="enable_ham_1p25m" checked/> 1.25 m <span class="muted">(222–225)</span></label>
              <label><input type="checkbox" id="enable_ham_70cm" checked/> 70 cm <span class="muted">(420–450)</span></label>
              <label><input type="checkbox" id="enable_ham_33cm" checked/> 33 cm <span class="muted">(902–928)</span></label>
              <label><input type="checkbox" id="enable_ham_23cm" checked/> 23 cm <span class="muted">(1240–1300)</span></label>
            </div>
            <label><input type="checkbox" id="enable_gmrs" checked/> GMRS/FRS <span class="muted">(462 / 467)</span></label>
            <label><input type="checkbox" id="enable_marine" checked/> MARINE <span class="muted">(156–162)</span></label>
            <label><input type="checkbox" id="enable_murs" checked/> MURS <span class="muted">(151–155)</span></label>
          </div>
          <div class="saved" id="bg-saved">saved</div>

          <h3 style="margin-top:1.25rem;">RECORDING THRESHOLDS</h3>
          <p style="margin:0 0 .65rem;font-size:.72rem;color:var(--muted);line-height:1.35;">
            Global for all bands. Higher = fewer clips / less static.
          </p>
          <div class="sq-row atc">
            <label><span class="range">RF SNR (min dB above noise)</span><b id="v-snr">12.0</b></label>
            <input type="range" id="snr_threshold_db" min="4" max="35" step="0.5" value="12"/>
          </div>
          <div class="sq-row ham">
            <label><span class="range">Voice score (≤0.30 = looser)</span><b id="v-voice">0.25</b></label>
            <input type="range" id="min_voice_score" min="0.10" max="0.95" step="0.01" value="0.25"/>
          </div>
          <div class="sq-row gmrs">
            <label><span class="range">Min activity %</span><b id="v-act">4</b>%</label>
            <input type="range" id="min_activity_ratio" min="0.01" max="0.50" step="0.01" value="0.04"/>
          </div>
          <div class="sq-row marine">
            <label><span class="range">Min dynamic range dB</span><b id="v-dyn">4.0</b></label>
            <input type="range" id="min_dynamic_range_db" min="1" max="20" step="0.5" value="4"/>
          </div>
          <div class="saved" id="sq-saved">saved</div>

          <details class="adv">
            <summary>Advanced / filters</summary>
            <div style="margin-top:.5rem;display:flex;flex-direction:column;gap:.4rem;">
              <select id="quality" style="background:var(--panel);color:var(--text);border:1px solid var(--line);border-radius:6px;padding:.35rem;">
                <option value="accepted">Accepted only</option>
                <option value="">All</option>
                <option value="rejected">Rejected only</option>
              </select>
              <button id="purge" class="icon-btn" style="width:auto;padding:0 .6rem;height:30px;">Purge rejected</button>
            </div>
          </details>
        </aside>

        <section class="main">
          <div class="legend">
            <span class="pill atc">ATC</span>
            <span class="pill ham">HAM</span>
            <span class="pill gmrs">GMRS</span>
            <span class="pill marine">MARINE</span>
            <span class="pill murs">MURS</span>
          </div>
          <div class="status-row">
            <span>Now: <b id="band-line">—</b></span>
            <span class="sep">·</span>
            <span>Plan: <b id="plan-count">—</b></span>
          </div>
          <div id="plan-chips"></div>

          <div class="viz">
            <div class="spectrum-wrap">
              <div class="axis-y"><span>0</span><span>-20</span><span>-40</span><span>-60</span><span>-80</span><span>-100</span></div>
              <canvas id="spectrum"></canvas>
              <div class="axis-x" id="axis-x"><span>0 MHz</span><span>500 MHz</span><span>1.0 GHz</span></div>
            </div>
            <div class="waterfall-wrap">
              <canvas id="waterfall"></canvas>
            </div>
          </div>

          <div class="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>TIME (UTC)</th>
                  <th>FREQUENCY</th>
                  <th>BAND</th>
                  <th>SNR (dB)</th>
                  <th>DURATION</th>
                  <th>FILE</th>
                  <th></th>
                </tr>
              </thead>
              <tbody id="rows">
                <tr><td colspan="7" class="empty">Loading…</td></tr>
              </tbody>
            </table>
          </div>
        </section>
      </div>
    </div>
  </div>

  <script>
    const $ = (id) => document.getElementById(id);
    let saveTimer = null;
    const wfRows = 80;
    let wf = null; // ImageData buffer rows

    const GROUP_COLOR = {
      atc: '#2e9ce7', ham: '#39c97a', gmrs: '#e5b733',
      marine: '#9b79e7', murs: '#df6caf', public: '#e27a59',
      other: '#8ea1b5'
    };

    let activeAudio = null;
    let activePlayButton = null;

    function fmtTime(iso) {
      if (!iso) return '—';
      try {
        return new Date(iso).toISOString().replace('T',' ').replace(/\.\d+Z$/,'Z');
      } catch { return iso; }
    }
    function fmtDur(s) {
      s = Number(s) || 0;
      const m = Math.floor(s/60), sec = Math.round(s%60);
      return String(m).padStart(2,'0') + ':' + String(sec).padStart(2,'0');
    }
    function groupOf(bandName, notes) {
      const n = ((bandName || '') + ' ' + (notes || '')).toLowerCase();
      if (n.includes('public') || n.includes('safety') || n.includes('police') || n.includes('fire')) return 'public';
      if (n.startsWith('atc') || n.includes('air') || n.includes('aviation')) return 'atc';
      if (n.startsWith('gmrs') || n.startsWith('frs')) return 'gmrs';
      if (n.startsWith('marine') || n.includes('vhf marine')) return 'marine';
      if (n.startsWith('murs')) return 'murs';
      if (n.startsWith('2m') || n.startsWith('70cm') || n.startsWith('1.25') || n.includes('kd6') || n.includes('ham')) return 'ham';
      return 'other';
    }

    function groupLabel(group) {
      return group === 'public' ? 'PUBLIC SAFETY' : group.toUpperCase();
    }

    function updateLabels() {
      $('v-snr').textContent = Number($('snr_threshold_db').value).toFixed(1);
      $('v-voice').textContent = Number($('min_voice_score').value).toFixed(2);
      $('v-act').textContent = Math.round(Number($('min_activity_ratio').value)*100);
      $('v-dyn').textContent = Number($('min_dynamic_range_db').value).toFixed(1);
    }

    async function loadSquelch() {
      const s = await (await fetch('/api/squelch')).json();
      $('snr_threshold_db').value = s.snr_threshold_db;
      $('min_voice_score').value = s.min_voice_score;
      $('min_activity_ratio').value = s.min_activity_ratio;
      $('min_dynamic_range_db').value = s.min_dynamic_range_db;
      const meters = ['enable_ham_10m','enable_ham_6m','enable_ham_2m','enable_ham_1p25m','enable_ham_70cm','enable_ham_33cm','enable_ham_23cm'];
      $('enable_atc').checked = !!s.enable_atc;
      meters.forEach(id => { if ($(id)) $(id).checked = s[id] !== false; });
      $('enable_ham').checked = s.enable_ham !== false && meters.some(id => $(id) && $(id).checked);
      $('enable_gmrs').checked = s.enable_gmrs !== false;
      $('enable_murs').checked = s.enable_murs !== false;
      $('enable_marine').checked = s.enable_marine !== false;
      updateLabels();
    }

    function meterIds() {
      return ['enable_ham_10m','enable_ham_6m','enable_ham_2m','enable_ham_1p25m','enable_ham_70cm','enable_ham_33cm','enable_ham_23cm'];
    }

    async function saveSquelch(fromBands) {
      updateLabels();
      const meters = meterIds();
      // Master HAM toggles all meters
      if (fromBands === 'ham-master') {
        const on = $('enable_ham').checked;
        meters.forEach(id => { if ($(id)) $(id).checked = on; });
      } else if (fromBands === 'ham-meter') {
        $('enable_ham').checked = meters.some(id => $(id) && $(id).checked);
      }
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
      meters.forEach(id => { body[id] = !!( $(id) && $(id).checked ); });
      await fetch('/api/squelch', {
        method: 'POST',
        headers: {'Content-Type':'application/json'},
        body: JSON.stringify(body),
      });
      const el = $(fromBands ? 'bg-saved' : 'sq-saved');
      el.classList.add('show');
      setTimeout(() => el.classList.remove('show'), 1000);
    }
    function scheduleSave() {
      updateLabels();
      clearTimeout(saveTimer);
      saveTimer = setTimeout(() => saveSquelch(false), 180);
    }
    ['snr_threshold_db','min_voice_score','min_activity_ratio','min_dynamic_range_db'].forEach(id => {
      $(id).addEventListener('input', scheduleSave);
    });
    $('enable_atc').addEventListener('change', () => saveSquelch(true));
    $('enable_gmrs').addEventListener('change', () => saveSquelch(true));
    $('enable_murs').addEventListener('change', () => saveSquelch(true));
    $('enable_marine').addEventListener('change', () => saveSquelch(true));
    $('enable_ham').addEventListener('change', () => saveSquelch('ham-master'));
    meterIds().forEach(id => {
      if ($(id)) $(id).addEventListener('change', () => saveSquelch('ham-meter'));
    });

    function drawSpectrum(state) {
      const canvas = $('spectrum');
      const dpr = window.devicePixelRatio || 1;
      const w = canvas.clientWidth, h = canvas.clientHeight;
      if (!w || !h) return;
      if (canvas.width !== Math.floor(w*dpr) || canvas.height !== Math.floor(h*dpr)) {
        canvas.width = Math.floor(w*dpr); canvas.height = Math.floor(h*dpr);
      }
      const ctx = canvas.getContext('2d');
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      ctx.clearRect(0, 0, w, h);

      const left = 30, right = 8, top = 25, bottom = 22;
      const plotW = Math.max(1, w - left - right);
      const plotH = Math.max(1, h - top - bottom);

      // Fine grid matching the instrument-panel look in the reference image.
      ctx.strokeStyle = 'rgba(128, 163, 191, .16)';
      ctx.lineWidth = 1;
      for (let i = 0; i <= 5; i++) {
        const y = top + plotH * i / 5 + .5;
        ctx.beginPath(); ctx.moveTo(left, y); ctx.lineTo(w - right, y); ctx.stroke();
      }
      const freqs = state && state.freqs_mhz ? state.freqs_mhz : [];
      for (let i = 0; i <= 10; i++) {
        const x = left + plotW * i / 10 + .5;
        ctx.strokeStyle = 'rgba(128, 163, 191, .10)';
        ctx.beginPath(); ctx.moveTo(x, top); ctx.lineTo(x, h - bottom); ctx.stroke();
      }

      const power = state && Array.isArray(state.power_db) ? state.power_db : [];
      const n = power.length;
      if (!n) {
        ctx.fillStyle = 'rgba(139,155,180,.58)';
        ctx.font = '12px system-ui';
        ctx.fillText('Waiting for scanner spectrum… start ./run.sh', left + 10, top + plotH / 2);
        return;
      }

      const mapY = (db) => {
        const t = Math.max(-100, Math.min(0, Number(db) || -100));
        return top + (1 - (t + 100) / 100) * plotH;
      };
      const mapX = (i) => left + (n < 2 ? .5 : i / (n - 1)) * plotW;
      const col = GROUP_COLOR[(state.group || 'other').toLowerCase()] || GROUP_COLOR.other;

      // Filled blue noise floor and luminous trace.
      ctx.beginPath();
      for (let i = 0; i < n; i++) {
        const x = mapX(i), y = mapY(power[i]);
        if (i === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
      }
      ctx.lineTo(w - right, h - bottom); ctx.lineTo(left, h - bottom); ctx.closePath();
      const fill = ctx.createLinearGradient(0, top, 0, h - bottom);
      fill.addColorStop(0, 'rgba(27, 143, 235, .42)');
      fill.addColorStop(.7, 'rgba(13, 82, 150, .16)');
      fill.addColorStop(1, 'rgba(0, 20, 42, 0)');
      ctx.fillStyle = fill; ctx.fill();

      ctx.beginPath();
      for (let i = 0; i < n; i++) {
        const x = mapX(i), y = mapY(power[i]);
        if (i === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
      }
      ctx.strokeStyle = '#1993ee';
      ctx.lineWidth = 1.25;
      ctx.shadowColor = 'rgba(22, 141, 241, .75)';
      ctx.shadowBlur = 3;
      ctx.stroke();
      ctx.shadowBlur = 0;

      // Mark detected carriers with a colored vertical beam.
      (state.peaks || []).forEach((p, index) => {
        if (freqs.length < 2 || p.mhz < freqs[0] || p.mhz > freqs[freqs.length - 1]) return;
        const x = left + ((p.mhz - freqs[0]) / (freqs[freqs.length - 1] - freqs[0])) * plotW;
        ctx.strokeStyle = index % 3 === 0 ? col : 'rgba(32, 145, 232, .60)';
        ctx.lineWidth = index % 3 === 0 ? 1.2 : .7;
        ctx.shadowColor = ctx.strokeStyle;
        ctx.shadowBlur = 5;
        ctx.beginPath(); ctx.moveTo(x, top); ctx.lineTo(x, h - bottom); ctx.stroke();
        ctx.shadowBlur = 0;
      });
    }

    function pushWaterfall(state) {
      const canvas = $('waterfall');
      const dpr = window.devicePixelRatio || 1;
      const w = canvas.clientWidth, h = canvas.clientHeight;
      if (!w || !h) return;
      if (canvas.width !== Math.floor(w * dpr) || canvas.height !== Math.floor(h * dpr)) {
        canvas.width = Math.floor(w * dpr);
        canvas.height = Math.floor(h * dpr);
        wf = null;
      }
      const ctx = canvas.getContext('2d');
      const cw = canvas.width, ch = canvas.height;
      ctx.imageSmoothingEnabled = false;
      if (!wf) wf = ctx.createImageData(cw, ch);

      // Move the previous waterfall down one physical pixel row.
      wf.data.copyWithin(cw * 4, 0);
      const power = state && Array.isArray(state.power_db) ? state.power_db : [];
      const row = new Uint8ClampedArray(cw * 4);
      for (let x = 0; x < cw; x++) {
        const i = power.length ? Math.floor(x / cw * Math.max(power.length - 1, 0)) : 0;
        const db = power.length ? Number(power[i]) : -100;
        const t = Math.max(0, Math.min(1, (db + 100) / 100));
        // Blue-to-cyan-to-yellow heat map, like a real SDR waterfall.
        let r, g, b;
        if (t < .25) {
          const q = t / .25; r = 3; g = 12 + 40 * q; b = 38 + 100 * q;
        } else if (t < .55) {
          const q = (t - .25) / .30; r = 4 + 20 * q; g = 52 + 112 * q; b = 138 + 55 * q;
        } else if (t < .78) {
          const q = (t - .55) / .23; r = 24 + 190 * q; g = 164 + 50 * q; b = 185 - 125 * q;
        } else {
          const q = (t - .78) / .22; r = 214 + 41 * q; g = 214 - 120 * q; b = 60 - 45 * q;
        }
        const k = x * 4;
        row[k] = Math.round(r); row[k + 1] = Math.round(g);
        row[k + 2] = Math.round(b); row[k + 3] = 255;
      }
      wf.data.set(row, 0);
      ctx.putImageData(wf, 0, 0);
    }

    function updateAxis(state) {
      const el = $('axis-x');
      if (!state || !state.freqs_mhz || state.freqs_mhz.length < 2) {
        el.innerHTML = '<span>0 MHz</span><span>500 MHz</span><span>1.0 GHz</span>';
        return;
      }
      const f0 = Number(state.freqs_mhz[0]);
      const f1 = Number(state.freqs_mhz[state.freqs_mhz.length - 1]);
      const mid = (f0 + f1) / 2;
      const format = (f) => f >= 1000 ? (f / 1000).toFixed(1) + ' GHz' : Math.round(f) + ' MHz';
      el.innerHTML = `<span>${format(f0)}</span><span>${format(mid)}</span><span>${format(f1)}</span>`;
    }

    async function loadLive() {
      try {
        const r = await fetch('/api/live', {cache: 'no-store'});
        if (!r.ok || r.status === 204) {
          $('live-dot').classList.add('off');
          drawSpectrum(null);
          return;
        }
        const s = await r.json();
        if (!s || !s.ts) {
          $('live-dot').classList.add('off');
          drawSpectrum(null);
          return;
        }
        const age = Date.now() / 1000 - Number(s.ts);
        $('live-dot').classList.toggle('off', age > 8);
        $('site').textContent = s.site || 'LOCAL';
        $('mode').textContent = s.mode || '—';
        $('gain').textContent = (s.gain_db != null ? Number(s.gain_db).toFixed(1) + ' dB' : '—');
        $('utc').textContent = s.utc || '—';
        const span = Number(s.span_mhz || 2);
        const g = String(s.group || '').toUpperCase();
        const radioTag = s.radio ? ` · radio ${s.radio}` : '';
        $('band-line').textContent =
          `${s.band || '—'}  (${g || '—'})  ` +
          `${Number(s.center_mhz || 0).toFixed(3)} MHz · plot ±${(span/2).toFixed(2)} MHz${radioTag}`;
        const plan = s.plan || [];
        const radios = s.radios || [];
        const pc = $('plan-count');
        if (pc) {
          const multi = radios.length > 1 ? ` · ${radios.length} dongles` : '';
          pc.textContent = plan.length
            ? `${plan.length} windows (hopping; plot = current only)${multi}`
            : (radios.length > 1 ? `${radios.length} dongles` : '—');
        }
        const chips = $('plan-chips');
        if (chips) {
          chips.innerHTML = plan.map(p => {
            const col = GROUP_COLOR[(p.group||'').toLowerCase()] || '#94a3b8';
            const cls = p.active ? 'chip active' : 'chip';
            const label = `${p.name}`;
            return `<span class="${cls}" style="border-left:3px solid ${col}" title="${Number(p.start_mhz).toFixed(1)}–${Number(p.stop_mhz).toFixed(1)} MHz">${label}</span>`;
          }).join('');
        }
        drawSpectrum(s);
        pushWaterfall(s);
        updateAxis(s);
      } catch (err) {
        $('live-dot').classList.add('off');
        drawSpectrum(null);
      }
    }

    async function loadRows() {
      try {
        const q = $('quality').value;
        const params = new URLSearchParams({limit: '200'});
        if (q) params.set('quality', q);
        const response = await fetch('/api/transmissions?' + params, {cache: 'no-store'});
        const rows = await response.json();
        const tbody = $('rows');
        if (!rows.length) {
          tbody.innerHTML = '<tr><td colspan="7" class="empty">No recordings yet — run <code>./run.sh</code> and enable band groups.</td></tr>';
          return;
        }
        tbody.innerHTML = rows.map(r => {
          const g = groupOf(r.band_name, r.notes);
          const fname = (r.audio_file || '').split('/').pop() || '—';
          const play = r.audio_file
            ? `<button class="icon-btn play" data-src="/audio/${r.id}" title="Play recording">▶</button>
               <a class="icon-btn download" title="Download" href="/audio/${r.id}" download="${fname}">⇩</a>`
            : '—';
          return `<tr>
            <td>${fmtTime(r.start_utc)}</td>
            <td class="freq">${Number(r.frequency_mhz || 0).toFixed(4)} MHz</td>
            <td><span class="tag ${g}">${groupLabel(g)}</span></td>
            <td>${Number(r.peak_snr_db ?? 0).toFixed(1)}</td>
            <td>${fmtDur(r.duration_seconds)}</td>
            <td class="file">${r.audio_file ? `<a href="/audio/${r.id}" download="${fname}">${fname}</a>` : '—'}</td>
            <td class="actions">${play}
              <button class="icon-btn del" data-id="${r.id}" title="Delete">✕</button>
            </td>
          </tr>`;
        }).join('');

        tbody.querySelectorAll('.play').forEach(btn => {
          btn.onclick = () => {
            if (activeAudio && activePlayButton === btn) {
              activeAudio.pause();
              activeAudio.currentTime = 0;
              btn.textContent = '▶';
              btn.classList.remove('active');
              activeAudio = null;
              activePlayButton = null;
              return;
            }
            if (activeAudio) activeAudio.pause();
            if (activePlayButton) {
              activePlayButton.textContent = '▶';
              activePlayButton.classList.remove('active');
            }
            activeAudio = new Audio(btn.dataset.src);
            activePlayButton = btn;
            btn.textContent = '■';
            btn.classList.add('active');
            activeAudio.onended = () => {
              btn.textContent = '▶';
              btn.classList.remove('active');
              activeAudio = null;
              activePlayButton = null;
            };
            activeAudio.play().catch(() => {
              btn.textContent = '▶';
              btn.classList.remove('active');
            });
          };
        });
        tbody.querySelectorAll('.del').forEach(btn => {
          btn.onclick = async () => {
            if (!confirm('Delete this recording?')) return;
            await fetch('/api/transmissions/' + btn.dataset.id, {method: 'DELETE'});
            loadRows();
          };
        });
      } catch (err) {
        // Keep the existing table visible during a short database/API restart.
      }
    }

    $('quality').onchange = loadRows;
    $('purge').onclick = async () => {
      if (!confirm('Delete all rejected transmissions?')) return;
      await fetch('/api/purge_rejected', {method:'POST'});
      loadRows();
    };
    async function doShutdown() {
      if (!confirm('Stop the scanner and close the dashboard?\n\n(The USB dongle will be released.)')) return;
      try {
        await fetch('/api/shutdown', {method: 'POST'});
      } catch (e) { /* server may die before response */ }
      document.body.innerHTML =
        '<div class="shutdown-msg">' +
        '<h1>RTL Airwave Scanner stopped</h1>' +
        '<p>Scanner and dashboard are shut down. You can close this tab.</p>' +
        '<p style="font-size:.85rem">Start again from the app menu or <code>./start-background.sh</code></p>' +
        '</div>';
    }
    $('shutdown').onclick = doShutdown;

    // Operator callsign (from site.yaml via /api/status)
    fetch('/api/status').then(r => r.json()).then(s => {
      const op = [s.operator_callsign, s.operator_class].filter(Boolean).join(' · ');
      if (op && $('operator')) $('operator').textContent = op;
    }).catch(() => {});

    loadSquelch();
    loadLive();
    loadRows();
    setInterval(loadLive, 400);
    setInterval(loadRows, 8000);
    window.addEventListener('resize', () => loadLive());
  </script>
</body>
</html>
"""


def create_app(cfg: Config) -> Flask:
    log = TransmissionLog(cfg.database, cfg.csv_path, cfg.output_dir)
    app = Flask(__name__)
    root = Path.cwd()
    squelch_path = Path(cfg.squelch_file)
    live_path = Path(cfg.output_dir) / "live_state.json"

    if not squelch_path.is_file():
        seed = dict(DEFAULTS)
        seed.update(BOOL_DEFAULTS)
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

    @app.get("/api/live")
    def api_live():
        state = read_live_state(live_path)
        if not state:
            return jsonify({}), 204
        return jsonify(state)

    @app.get("/api/status")
    def api_status():
        return jsonify(
            {
                "operator_callsign": getattr(cfg, "operator_callsign", "") or "",
                "operator_class": getattr(cfg, "operator_class", "") or "",
                "viewer_pid": os.getpid(),
            }
        )

    @app.get("/api/squelch")
    def api_squelch_get():
        return jsonify(load_squelch(squelch_path))

    @app.post("/api/squelch")
    def api_squelch_set():
        body = request.get_json(force=True, silent=True) or {}
        current = load_squelch(squelch_path)
        current.update({k: body[k] for k in DEFAULTS if k in body})
        current.update({k: body[k] for k in BOOL_DEFAULTS if k in body})
        saved = save_squelch(current, squelch_path)
        apply_to_config(cfg, saved)
        return jsonify(saved)

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

    @app.post("/api/shutdown")
    def api_shutdown():
        """Stop scanner process(es) and then this viewer (releases USB dongle)."""
        project = str(root.resolve())
        viewer_pid = os.getpid()

        def _shutdown():
            time.sleep(0.35)
            # Stop scanner workers first (not this viewer)
            try:
                import subprocess

                out = subprocess.check_output(["ps", "-eo", "pid=,args="], text=True)
                for line in out.splitlines():
                    line = line.strip()
                    if not line:
                        continue
                    parts = line.split(None, 1)
                    if len(parts) < 2:
                        continue
                    pid_s, args = parts[0], parts[1]
                    try:
                        pid = int(pid_s)
                    except ValueError:
                        continue
                    if pid == viewer_pid:
                        continue
                    if "python" not in args or "-m scanner" not in args:
                        continue
                    if "scanner.viewer" in args:
                        continue
                    # Prefer processes for this project path when visible
                    if project not in args and "rtl-airwave-scanner" not in args and "-m scanner" not in args:
                        continue
                    try:
                        os.kill(pid, signal.SIGTERM)
                    except ProcessLookupError:
                        pass
                time.sleep(0.5)
                # Force-kill stubborn scanners
                out = subprocess.check_output(["ps", "-eo", "pid=,args="], text=True)
                for line in out.splitlines():
                    line = line.strip()
                    parts = line.split(None, 1)
                    if len(parts) < 2:
                        continue
                    try:
                        pid = int(parts[0])
                    except ValueError:
                        continue
                    args = parts[1]
                    if pid == viewer_pid:
                        continue
                    if "python" in args and "-m scanner" in args and "scanner.viewer" not in args:
                        try:
                            os.kill(pid, signal.SIGKILL)
                        except ProcessLookupError:
                            pass
            except Exception:
                pass
            # Clear pid files if present
            for name in ("scanner.pid", "viewer.pid"):
                try:
                    (root / "logs" / name).unlink(missing_ok=True)
                except Exception:
                    pass
            # Stop this Flask process
            try:
                os.kill(viewer_pid, signal.SIGTERM)
            except Exception:
                os._exit(0)

        threading.Thread(target=_shutdown, daemon=True).start()
        return jsonify({"ok": True, "message": "Shutting down scanner and viewer"})

    @app.get("/audio/<int:tx_id>")
    def audio(tx_id: int):
        from scanner.retention import open_audio_bytes

        row = log.get(tx_id)
        if not row or not row.get("audio_file"):
            abort(404)
        raw = Path(row["audio_file"])
        candidates: list[Path] = []
        if raw.is_absolute():
            candidates.append(raw)
        else:
            candidates.append((root / raw).resolve())
            candidates.append((Path.cwd() / raw).resolve())
            candidates.append(raw.resolve())
        # Also try archive/<name>.wav.zip if loose WAV was archived
        extra: list[Path] = []
        for c in list(candidates):
            if c.suffix.lower() == ".wav":
                extra.append(c.parent / "archive" / f"{c.name}.zip")
                extra.append(cfg.output_dir / "archive" / f"{c.name}.zip")
            if str(c).endswith(".wav.zip") and not c.is_file():
                # bare path under recordings/
                extra.append(cfg.output_dir / "archive" / c.name)
        candidates.extend(extra)
        path = next((p for p in candidates if p.is_file()), None)
        if path is None:
            abort(404)
        if path.suffix.lower() == ".zip" or path.name.endswith(".wav.zip"):
            opened = open_audio_bytes(path)
            if opened is None:
                abort(404)
            data, dl_name = opened
            return Response(
                data,
                mimetype="audio/wav",
                headers={
                    "Content-Disposition": f'inline; filename="{dl_name}"',
                    "Cache-Control": "no-cache",
                },
            )
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

