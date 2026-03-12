#!/usr/bin/env python3
"""
meek_report.py — RF Signal Intelligence Report Generator
Queries rf_adapt_intel.db and produces a self-contained HTML report.

Usage:
    python3 meek_report.py
    python3 meek_report.py --db /path/to/rf_adapt_intel.db
    python3 meek_report.py --days 7 --out /tmp/report.html

Run on Brian:  python3 meek_report.py
Open report:   xdg-open ~/meek_report.html   (or scp to your machine)
"""

import argparse
import json
import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

DEFAULT_DB   = "/home/woger/rf_worker/rf_adapt_intel.db"
DEFAULT_OUT  = Path.home() / "meek_report.html"
DEFAULT_DAYS = 30

BAND_GROUPS = {
    "Aviation": [
        ("ADS-B",        "ADS-B 1090 MHz transponders"),
        ("VDL2",         "VHF Data Link Mode 2"),
        ("ACARS-VHF",    "ACARS aviation data 136.900 MHz"),
        ("ACARS-129",    "ACARS secondary A 129.125 MHz"),
        ("ACARS-130",    "ACARS secondary C 130.025 MHz"),
        ("AIRBAND-VHF",  "VHF airband AM voice 118-136 MHz"),
        ("VOLMET",       "London VOLMET weather broadcast"),
        ("INMARSAT-AERO","Inmarsat Aero L-band"),
    ],
    "Maritime": [
        ("AIS-A",        "AIS channel A 161.975 MHz"),
        ("AIS-B",        "AIS channel B 162.025 MHz"),
        ("MARINE-CH16",  "Marine VHF channel 16"),
        ("MARINE-CH70",  "Marine DSC channel 70"),
    ],
    "Railway": [
        ("GSM-R-876",    "Network Rail GSM-R uplink 876 MHz"),
    ],
    "Weather & Satellite": [
        ("NOAA-APT",     "NOAA weather satellite APT"),
        ("METEOR-LRPT",  "Meteor-M2 LRPT"),
        ("RADIOSONDE",   "Met Office radiosonde balloons"),
        ("GPS-L1",       "GPS L1 C/A"),
        ("IRIDIUM",      "Iridium LEO bursts"),
    ],
    "IoT & Smart Infrastructure": [
        ("SMETS2",       "UK SMETS2 smart meters"),
        ("TPMS-433",     "Tyre pressure sensors"),
        ("ISM-433",      "ISM 433 MHz devices"),
        ("ISM-169",      "ISM 169 MHz sub-GHz IoT"),
        ("LORA-868",     "LoRaWAN 868 MHz"),
        ("ZIGBEE-868",   "ZigBee 868 MHz"),
        ("WMBUS-169",    "Wireless M-Bus 169 MHz"),
        ("SIGFOX-868",   "Sigfox IoT 868 MHz"),
        ("ZWAVE-868",    "Z-Wave 868 MHz"),
    ],
    "Emergency & Public Safety": [
        ("TETRA",        "TETRA digital radio (TVP/SCAS/Berks Fire)"),
        ("ELT-406",      "ELT/EPIRB distress beacons 406 MHz"),
        ("PMR446",       "PMR446 licence-free radio"),
        ("APRS",         "APRS packet radio 144.800 MHz"),
        ("POCSAG-153",   "POCSAG paging 153 MHz"),
        ("FLEX-931",     "FLEX high-speed paging 931 MHz"),
    ],
    "Broadcasting & Paging": [
        ("DAB",          "DAB/DAB+ digital radio"),
        ("DMR",          "DMR digital voice"),
        ("DECT",         "DECT cordless phones"),
        ("CNI-UHF",      "Combat Net Radio UHF"),
    ],
}

# Flat ordered list of all bands for league table
ALL_BANDS = [b for bands in BAND_GROUPS.values() for b, _ in bands]

# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

def open_db(path: str) -> sqlite3.Connection:
    if not os.path.exists(path):
        print(f"[ERROR] DB not found: {path}", file=sys.stderr)
        sys.exit(1)
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA query_only=ON")
    return con


def q(con, sql, params=()):
    return con.execute(sql, params).fetchall()


def band_filter(band: str) -> str:
    return f"%band={band}%"

# ---------------------------------------------------------------------------
# Query functions — return plain Python dicts/lists for templating
# ---------------------------------------------------------------------------

def summary_counts(con, days):
    rows = q(con, """
        SELECT 'signals'  AS t, COUNT(*) AS n FROM signals
        UNION ALL
        SELECT 'examples', COUNT(*) FROM examples
        UNION ALL
        SELECT 'methods',  COUNT(*) FROM methods
    """)
    total_signals = rows[0]["n"]
    total_examples = rows[1]["n"]
    total_methods  = rows[2]["n"]
    recent = q(con, """
        SELECT COUNT(*) AS n FROM signals
        WHERE timestamp >= DATETIME('now', ?)
    """, (f"-{days} days",))[0]["n"]
    oldest = q(con, "SELECT MIN(timestamp) AS t FROM signals")[0]["t"]
    newest = q(con, "SELECT MAX(timestamp) AS t FROM signals")[0]["t"]
    return {
        "total_signals":  total_signals,
        "total_examples": total_examples,
        "total_methods":  total_methods,
        "recent_signals": recent,
        "oldest": oldest or "—",
        "newest": newest or "—",
    }


def league_table(con, days):
    results = []
    for band in ALL_BANDS:
        rows = q(con, """
            SELECT COUNT(*) AS n,
                   ROUND(AVG(e.confidence), 3) AS avg_conf,
                   MAX(s.timestamp) AS last_seen
            FROM signals s
            JOIN examples e ON e.signal_id = s.id
            WHERE s.notes LIKE ?
              AND s.timestamp >= DATETIME('now', ?)
        """, (band_filter(band), f"-{days} days"))
        r = rows[0]
        results.append({
            "band":      band,
            "count":     r["n"] or 0,
            "avg_conf":  r["avg_conf"] or 0.0,
            "last_seen": r["last_seen"] or "—",
        })
    results.sort(key=lambda x: x["count"], reverse=True)
    return results


def daily_series(con, band, days):
    """Returns list of {day, count} for last N days."""
    rows = q(con, """
        SELECT DATE(timestamp) AS day, COUNT(*) AS n
        FROM signals
        WHERE notes LIKE ?
          AND timestamp >= DATE('now', ?)
        GROUP BY day
        ORDER BY day
    """, (band_filter(band), f"-{days} days"))
    return [{"day": r["day"], "count": r["n"]} for r in rows]


def hourly_series(con, band, days):
    rows = q(con, """
        SELECT CAST(STRFTIME('%H', timestamp) AS INTEGER) AS hour,
               COUNT(*) AS n
        FROM signals
        WHERE notes LIKE ?
          AND timestamp >= DATETIME('now', ?)
        GROUP BY hour
        ORDER BY hour
    """, (band_filter(band), f"-{days} days"))
    # Fill missing hours with 0
    hour_map = {r["hour"]: r["n"] for r in rows}
    return [{"hour": h, "count": hour_map.get(h, 0)} for h in range(24)]


def confidence_distribution(con, days):
    rows = q(con, """
        SELECT
            CASE
                WHEN e.confidence >= 0.9 THEN 'Very High (≥0.90)'
                WHEN e.confidence >= 0.7 THEN 'High (0.70–0.89)'
                WHEN e.confidence >= 0.5 THEN 'Medium (0.50–0.69)'
                WHEN e.confidence >= 0.3 THEN 'Low (0.30–0.49)'
                ELSE                         'Noise (<0.30)'
            END AS bucket,
            COUNT(*) AS n
        FROM examples e
        JOIN signals s ON s.id = e.signal_id
        WHERE s.timestamp >= DATETIME('now', ?)
        GROUP BY bucket
    """, (f"-{days} days",))
    return [{"label": r["bucket"], "count": r["n"]} for r in rows]


def group_totals(con, days):
    out = {}
    for group, bands in BAND_GROUPS.items():
        total = 0
        for band, _ in bands:
            n = q(con, """
                SELECT COUNT(*) AS n FROM signals
                WHERE notes LIKE ?
                  AND timestamp >= DATETIME('now', ?)
            """, (band_filter(band), f"-{days} days"))[0]["n"]
            total += n
        out[group] = total
    return out


def recent_detections(con, limit=50):
    rows = q(con, """
        SELECT s.timestamp,
               s.notes,
               ROUND(e.confidence, 3) AS conf
        FROM signals s
        JOIN examples e ON e.signal_id = s.id
        ORDER BY s.timestamp DESC
        LIMIT ?
    """, (limit,))
    results = []
    for r in rows:
        notes = r["notes"] or ""
        band = "unknown"
        for part in notes.split():
            if part.startswith("band="):
                band = part[5:]
                break
        results.append({
            "timestamp": r["timestamp"],
            "band":      band,
            "conf":      r["conf"],
            "notes":     notes[:120],
        })
    return results


def elt_alerts(con):
    rows = q(con, """
        SELECT s.id, s.timestamp, ROUND(e.confidence,3) AS conf, s.notes
        FROM signals s
        JOIN examples e ON e.signal_id = s.id
        WHERE s.notes LIKE '%band=ELT-406%'
        ORDER BY s.timestamp DESC
        LIMIT 20
    """)
    return [dict(r) for r in rows]


def tetra_daily(con, days):
    rows = q(con, """
        SELECT DATE(timestamp) AS day, COUNT(*) AS n
        FROM signals
        WHERE notes LIKE '%band=TETRA%'
          AND timestamp >= DATE('now', ?)
        GROUP BY day ORDER BY day
    """, (f"-{days} days",))
    return [{"day": r["day"], "count": r["n"]} for r in rows]


def ais_weekly(con):
    rows = q(con, """
        SELECT STRFTIME('%Y-W%W', timestamp) AS week, COUNT(*) AS n
        FROM signals
        WHERE (notes LIKE '%band=AIS-A%' OR notes LIKE '%band=AIS-B%')
        GROUP BY week ORDER BY week DESC LIMIT 12
    """)
    return [{"week": r["week"], "count": r["n"]} for r in rows]


def radiosonde_coverage(con, days=14):
    rows = q(con, """
        SELECT DATE(timestamp) AS day,
               SUM(CASE WHEN STRFTIME('%H',timestamp) BETWEEN '21' AND '23'
                        OR   STRFTIME('%H',timestamp) = '00' THEN 1 ELSE 0 END) AS z00,
               SUM(CASE WHEN STRFTIME('%H',timestamp) BETWEEN '11' AND '13' THEN 1 ELSE 0 END) AS z12
        FROM signals
        WHERE notes LIKE '%band=RADIOSONDE%'
          AND timestamp >= DATE('now', ?)
        GROUP BY day ORDER BY day DESC
    """, (f"-{days} days",))
    return [{"day": r["day"], "z00": r["z00"], "z12": r["z12"]} for r in rows]


# ---------------------------------------------------------------------------
# HTML template
# ---------------------------------------------------------------------------

HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>meek · RF Signal Report</title>
<script src="https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.1/chart.umd.min.js"></script>
<style>
  @import url('https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;600&family=IBM+Plex+Sans:wght@300;400;600&display=swap');

  :root {
    --bg:       #0a0f14;
    --bg2:      #111820;
    --bg3:      #1a2530;
    --border:   #1e3040;
    --accent:   #00c8a0;
    --accent2:  #0088ff;
    --warn:     #ffb800;
    --danger:   #ff4444;
    --text:     #c8d8e8;
    --muted:    #5a7080;
    --mono:     'IBM Plex Mono', monospace;
    --sans:     'IBM Plex Sans', sans-serif;
  }

  *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }

  body {
    background: var(--bg);
    color: var(--text);
    font-family: var(--sans);
    font-size: 14px;
    line-height: 1.6;
  }

  /* ── Header ── */
  header {
    background: linear-gradient(135deg, #071018 0%, #0d2035 50%, #071018 100%);
    border-bottom: 1px solid var(--accent);
    padding: 28px 40px 22px;
    display: flex;
    align-items: flex-end;
    justify-content: space-between;
    gap: 20px;
  }
  header .logo { display: flex; align-items: center; gap: 14px; }
  header .logo svg { width: 36px; height: 36px; flex-shrink: 0; }
  header h1 {
    font-family: var(--mono);
    font-size: 22px;
    font-weight: 600;
    color: var(--accent);
    letter-spacing: 0.04em;
  }
  header .subtitle {
    font-family: var(--mono);
    font-size: 11px;
    color: var(--muted);
    letter-spacing: 0.06em;
    text-transform: uppercase;
  }
  header .meta {
    text-align: right;
    font-family: var(--mono);
    font-size: 11px;
    color: var(--muted);
    line-height: 1.8;
  }
  header .meta span { color: var(--text); }

  /* ── Nav ── */
  nav {
    background: var(--bg2);
    border-bottom: 1px solid var(--border);
    padding: 0 40px;
    display: flex;
    gap: 0;
    overflow-x: auto;
  }
  nav a {
    display: block;
    padding: 12px 18px;
    font-family: var(--mono);
    font-size: 11px;
    font-weight: 600;
    letter-spacing: 0.05em;
    text-transform: uppercase;
    color: var(--muted);
    text-decoration: none;
    border-bottom: 2px solid transparent;
    white-space: nowrap;
    transition: color 0.2s, border-color 0.2s;
  }
  nav a:hover { color: var(--accent); border-bottom-color: var(--accent); }

  /* ── Main layout ── */
  main { max-width: 1400px; margin: 0 auto; padding: 32px 40px 60px; }

  section { margin-bottom: 48px; }
  section h2 {
    font-family: var(--mono);
    font-size: 13px;
    font-weight: 600;
    letter-spacing: 0.08em;
    text-transform: uppercase;
    color: var(--accent);
    border-left: 3px solid var(--accent);
    padding-left: 12px;
    margin-bottom: 20px;
  }

  /* ── Stat cards ── */
  .stat-grid {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
    gap: 16px;
  }
  .stat-card {
    background: var(--bg2);
    border: 1px solid var(--border);
    border-radius: 6px;
    padding: 20px;
    position: relative;
    overflow: hidden;
  }
  .stat-card::before {
    content: '';
    position: absolute;
    top: 0; left: 0; right: 0;
    height: 2px;
    background: var(--accent);
  }
  .stat-card.warn::before { background: var(--warn); }
  .stat-card .label {
    font-family: var(--mono);
    font-size: 10px;
    letter-spacing: 0.1em;
    text-transform: uppercase;
    color: var(--muted);
    margin-bottom: 8px;
  }
  .stat-card .value {
    font-family: var(--mono);
    font-size: 28px;
    font-weight: 600;
    color: var(--text);
  }
  .stat-card .note {
    font-size: 11px;
    color: var(--muted);
    margin-top: 4px;
  }

  /* ── Chart cards ── */
  .chart-grid {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(360px, 1fr));
    gap: 20px;
  }
  .chart-card {
    background: var(--bg2);
    border: 1px solid var(--border);
    border-radius: 6px;
    padding: 20px;
  }
  .chart-card h3 {
    font-family: var(--mono);
    font-size: 11px;
    font-weight: 600;
    letter-spacing: 0.06em;
    text-transform: uppercase;
    color: var(--muted);
    margin-bottom: 14px;
  }
  .chart-wrap { position: relative; height: 200px; }
  .chart-wrap.tall { height: 280px; }
  .chart-wrap.short { height: 140px; }

  /* ── League table ── */
  .league-table {
    width: 100%;
    border-collapse: collapse;
    font-family: var(--mono);
    font-size: 12px;
  }
  .league-table thead th {
    padding: 8px 12px;
    text-align: left;
    font-size: 10px;
    letter-spacing: 0.08em;
    text-transform: uppercase;
    color: var(--muted);
    border-bottom: 1px solid var(--border);
  }
  .league-table thead th:not(:first-child) { text-align: right; }
  .league-table tbody tr {
    border-bottom: 1px solid var(--border);
    transition: background 0.1s;
  }
  .league-table tbody tr:hover { background: var(--bg3); }
  .league-table td {
    padding: 9px 12px;
    color: var(--text);
  }
  .league-table td:not(:first-child) { text-align: right; color: var(--muted); }
  .league-table td.count { color: var(--accent); font-weight: 600; }
  .league-table .rank { color: var(--muted); width: 32px; }
  .badge {
    display: inline-block;
    padding: 1px 7px;
    border-radius: 3px;
    font-size: 10px;
    font-weight: 600;
    letter-spacing: 0.04em;
  }
  .badge-aviation  { background: rgba(0,136,255,0.15); color: #60b0ff; }
  .badge-maritime  { background: rgba(0,200,160,0.15); color: var(--accent); }
  .badge-railway   { background: rgba(255,140,0,0.15); color: #ffa040; }
  .badge-weather   { background: rgba(150,100,255,0.15); color: #b090ff; }
  .badge-iot       { background: rgba(255,184,0,0.15);  color: var(--warn); }
  .badge-emergency { background: rgba(255,68,68,0.15);  color: var(--danger); }
  .badge-broadcast { background: rgba(200,200,200,0.15);color: #aaa; }

  /* ── Alert box ── */
  .alert-box {
    background: rgba(255,68,68,0.08);
    border: 1px solid rgba(255,68,68,0.4);
    border-radius: 6px;
    padding: 16px 20px;
    font-family: var(--mono);
    font-size: 12px;
  }
  .alert-box .alert-title {
    color: var(--danger);
    font-weight: 600;
    font-size: 13px;
    letter-spacing: 0.04em;
    margin-bottom: 10px;
  }
  .alert-row { padding: 6px 0; border-bottom: 1px solid rgba(255,68,68,0.15); color: var(--text); }
  .alert-row:last-child { border-bottom: none; }
  .alert-empty { color: var(--muted); font-style: italic; }

  /* ── Recent table ── */
  .recent-table {
    width: 100%;
    border-collapse: collapse;
    font-family: var(--mono);
    font-size: 11px;
  }
  .recent-table thead th {
    padding: 7px 10px;
    text-align: left;
    font-size: 10px;
    letter-spacing: 0.08em;
    text-transform: uppercase;
    color: var(--muted);
    border-bottom: 1px solid var(--border);
  }
  .recent-table tbody tr { border-bottom: 1px solid var(--border); }
  .recent-table tbody tr:hover { background: var(--bg3); }
  .recent-table td { padding: 7px 10px; color: var(--text); }
  .recent-table td.ts { color: var(--muted); white-space: nowrap; }
  .recent-table td.conf { text-align: right; }
  .conf-hi  { color: var(--accent); }
  .conf-med { color: var(--warn); }
  .conf-lo  { color: var(--muted); }

  /* ── Conf bar ── */
  .conf-bar {
    display: flex;
    align-items: center;
    gap: 8px;
  }
  .conf-bar-track {
    flex: 1;
    height: 4px;
    background: var(--bg3);
    border-radius: 2px;
    overflow: hidden;
  }
  .conf-bar-fill {
    height: 100%;
    border-radius: 2px;
    background: var(--accent);
  }

  /* ── Radiosonde table ── */
  .sonde-table {
    width: 100%;
    border-collapse: collapse;
    font-family: var(--mono);
    font-size: 12px;
  }
  .sonde-table th, .sonde-table td {
    padding: 8px 14px;
    border-bottom: 1px solid var(--border);
  }
  .sonde-table th {
    font-size: 10px;
    letter-spacing: 0.08em;
    text-transform: uppercase;
    color: var(--muted);
    text-align: left;
  }
  .sonde-table td.hit { color: var(--accent); }
  .sonde-table td.miss { color: var(--muted); }

  /* ── Footer ── */
  footer {
    text-align: center;
    padding: 24px;
    border-top: 1px solid var(--border);
    font-family: var(--mono);
    font-size: 10px;
    color: var(--muted);
    letter-spacing: 0.06em;
  }

  /* scrollbar */
  ::-webkit-scrollbar { width: 6px; height: 6px; }
  ::-webkit-scrollbar-track { background: var(--bg); }
  ::-webkit-scrollbar-thumb { background: var(--border); border-radius: 3px; }
</style>
</head>
<body>

<header>
  <div class="logo">
    <svg viewBox="0 0 36 36" fill="none" xmlns="http://www.w3.org/2000/svg">
      <circle cx="18" cy="18" r="17" stroke="#00c8a0" stroke-width="1.5" opacity="0.4"/>
      <circle cx="18" cy="18" r="12" stroke="#00c8a0" stroke-width="1.5" opacity="0.6"/>
      <circle cx="18" cy="18" r="7"  stroke="#00c8a0" stroke-width="1.5"/>
      <circle cx="18" cy="18" r="2"  fill="#00c8a0"/>
      <line x1="18" y1="1" x2="18" y2="5"   stroke="#00c8a0" stroke-width="1.5"/>
      <line x1="18" y1="31" x2="18" y2="35"  stroke="#00c8a0" stroke-width="1.5"/>
      <line x1="1"  y1="18" x2="5"  y2="18"  stroke="#00c8a0" stroke-width="1.5"/>
      <line x1="31" y1="18" x2="35" y2="18"  stroke="#00c8a0" stroke-width="1.5"/>
    </svg>
    <div>
      <h1>meek · rf_adapt_intel</h1>
      <div class="subtitle">Signal Intelligence Report — Woodley / Berkshire</div>
    </div>
  </div>
  <div class="meta">
    Generated <span>%%GENERATED%%</span><br>
    DB <span>%%DB_PATH%%</span><br>
    Period <span>Last %%DAYS%% days</span>
  </div>
</header>

<nav>
  <a href="#overview">Overview</a>
  <a href="#league">All Bands</a>
  <a href="#aviation">Aviation</a>
  <a href="#maritime">Maritime</a>
  <a href="#weather">Weather</a>
  <a href="#iot">IoT</a>
  <a href="#emergency">Emergency</a>
  <a href="#satellite">Satellite</a>
  <a href="#recent">Recent</a>
</nav>

<main>

<!-- ═══════════════════════════════════════════════════════════ OVERVIEW -->
<section id="overview">
  <h2>Overview</h2>
  <div class="stat-grid">
    <div class="stat-card">
      <div class="label">Total Signals (all time)</div>
      <div class="value">%%TOTAL_SIGNALS%%</div>
      <div class="note">%%TOTAL_EXAMPLES%% examples · %%TOTAL_METHODS%% methods</div>
    </div>
    <div class="stat-card">
      <div class="label">Signals this period</div>
      <div class="value">%%RECENT_SIGNALS%%</div>
      <div class="note">Last %%DAYS%% days</div>
    </div>
    <div class="stat-card">
      <div class="label">First detection</div>
      <div class="value" style="font-size:16px;padding-top:6px">%%OLDEST%%</div>
    </div>
    <div class="stat-card">
      <div class="label">Latest detection</div>
      <div class="value" style="font-size:16px;padding-top:6px">%%NEWEST%%</div>
    </div>
    <div class="stat-card warn" id="elt-stat">
      <div class="label">ELT-406 Alerts</div>
      <div class="value" id="elt-count" style="color:var(--danger)">%%ELT_COUNT%%</div>
      <div class="note">Distress beacons detected</div>
    </div>
  </div>

  <div class="chart-grid" style="margin-top:20px">
    <div class="chart-card">
      <h3>Detections by group (last %%DAYS%% days)</h3>
      <div class="chart-wrap short"><canvas id="groupPie"></canvas></div>
    </div>
    <div class="chart-card">
      <h3>Confidence distribution (last %%DAYS%% days)</h3>
      <div class="chart-wrap short"><canvas id="confDist"></canvas></div>
    </div>
  </div>
</section>

<!-- ═════════════════════════════════════════════════════════ LEAGUE TABLE -->
<section id="league">
  <h2>All Bands — Detection League Table</h2>
  <table class="league-table">
    <thead>
      <tr>
        <th>#</th>
        <th>Band</th>
        <th>Category</th>
        <th>Detections</th>
        <th>Avg Confidence</th>
        <th>Last Seen</th>
      </tr>
    </thead>
    <tbody id="leagueBody">
%%LEAGUE_ROWS%%
    </tbody>
  </table>
</section>

<!-- ═══════════════════════════════════════════════════════════ AVIATION -->
<section id="aviation">
  <h2>Aviation</h2>
  <div class="chart-grid">
    <div class="chart-card">
      <h3>ADS-B — daily detections</h3>
      <div class="chart-wrap"><canvas id="adsbDaily"></canvas></div>
    </div>
    <div class="chart-card">
      <h3>ADS-B — hourly heatmap (UTC)</h3>
      <div class="chart-wrap"><canvas id="adsbHourly"></canvas></div>
    </div>
    <div class="chart-card">
      <h3>Aviation bands comparison</h3>
      <div class="chart-wrap"><canvas id="aviationComp"></canvas></div>
    </div>
  </div>
</section>

<!-- ═══════════════════════════════════════════════════════════ MARITIME -->
<section id="maritime">
  <h2>Maritime · River Thames</h2>
  <div class="chart-grid">
    <div class="chart-card">
      <h3>AIS — weekly contacts</h3>
      <div class="chart-wrap"><canvas id="aisWeekly"></canvas></div>
    </div>
    <div class="chart-card">
      <h3>AIS channel balance (A vs B)</h3>
      <div class="chart-wrap short"><canvas id="aisBalance"></canvas></div>
    </div>
  </div>
</section>

<!-- ════════════════════════════════════════════════════════════ WEATHER -->
<section id="weather">
  <h2>Weather &amp; Atmospheric</h2>
  <div class="chart-grid">
    <div class="chart-card">
      <h3>Radiosonde launch coverage (00Z vs 12Z)</h3>
      <div style="overflow-x:auto;margin-top:4px">
        <table class="sonde-table">
          <thead><tr><th>Date</th><th>00Z (Larkhill)</th><th>12Z (Larkhill)</th></tr></thead>
          <tbody id="sondeBody">%%SONDE_ROWS%%</tbody>
        </table>
      </div>
    </div>
    <div class="chart-card">
      <h3>Weather / satellite bands</h3>
      <div class="chart-wrap"><canvas id="weatherComp"></canvas></div>
    </div>
  </div>
</section>

<!-- ═══════════════════════════════════════════════════════════════ IoT -->
<section id="iot">
  <h2>IoT &amp; Smart Infrastructure</h2>
  <div class="chart-grid">
    <div class="chart-card">
      <h3>TPMS — hourly traffic indicator</h3>
      <div class="chart-wrap"><canvas id="tpmsHourly"></canvas></div>
    </div>
    <div class="chart-card">
      <h3>SMETS2 smart meter — daily bursts</h3>
      <div class="chart-wrap"><canvas id="smets2Daily"></canvas></div>
    </div>
    <div class="chart-card">
      <h3>IoT band breakdown</h3>
      <div class="chart-wrap"><canvas id="iotComp"></canvas></div>
    </div>
  </div>
</section>

<!-- ══════════════════════════════════════════════════════ EMERGENCY -->
<section id="emergency">
  <h2>Emergency &amp; Public Safety</h2>

  <div class="alert-box" style="margin-bottom:20px">
    <div class="alert-title">⚠ ELT-406 Distress Beacon Log</div>
    <div id="eltBody">%%ELT_ROWS%%</div>
  </div>

  <div class="chart-grid">
    <div class="chart-card">
      <h3>TETRA burst activity — daily (TVP/SCAS/Berks Fire)</h3>
      <div class="chart-wrap"><canvas id="tetraDaily"></canvas></div>
    </div>
    <div class="chart-card">
      <h3>TETRA — hourly pattern</h3>
      <div class="chart-wrap"><canvas id="tetraHourly"></canvas></div>
    </div>
    <div class="chart-card">
      <h3>PMR446 — weekend vs weekday</h3>
      <div class="chart-wrap short"><canvas id="pmrPattern"></canvas></div>
    </div>
  </div>
</section>

<!-- ══════════════════════════════════════════════════════════ SATELLITE -->
<section id="satellite">
  <h2>Satellite</h2>
  <div class="chart-grid">
    <div class="chart-card">
      <h3>Iridium LEO — hourly burst geometry</h3>
      <div class="chart-wrap"><canvas id="iridiumHourly"></canvas></div>
    </div>
    <div class="chart-card">
      <h3>GPS-L1 — daily confidence trend</h3>
      <div class="chart-wrap"><canvas id="gpsConf"></canvas></div>
    </div>
  </div>
</section>

<!-- ══════════════════════════════════════════════════════════ RECENT -->
<section id="recent">
  <h2>Recent Detections</h2>
  <table class="recent-table">
    <thead>
      <tr><th>Timestamp</th><th>Band</th><th style="text-align:right">Confidence</th><th>Notes (truncated)</th></tr>
    </thead>
    <tbody>%%RECENT_ROWS%%</tbody>
  </table>
</section>

</main>

<footer>
  meek · rf_adapt_intel · foxrouter/meek · Report generated %%GENERATED%% · Woodley, Berkshire
</footer>

<script>
const ACCENT  = '#00c8a0';
const ACCENT2 = '#0088ff';
const WARN    = '#ffb800';
const DANGER  = '#ff4444';
const MUTED   = '#2a4050';
const TEXT    = '#c8d8e8';
const PURPLE  = '#9060ff';

Chart.defaults.color = '#5a7080';
Chart.defaults.borderColor = '#1e3040';
Chart.defaults.font.family = "'IBM Plex Mono', monospace";
Chart.defaults.font.size = 11;

function barChart(id, labels, data, color, label) {
  const ctx = document.getElementById(id);
  if (!ctx) return;
  new Chart(ctx, {
    type: 'bar',
    data: {
      labels,
      datasets: [{ label, data, backgroundColor: color + '99', borderColor: color, borderWidth: 1 }]
    },
    options: {
      responsive: true, maintainAspectRatio: false,
      plugins: { legend: { display: false } },
      scales: {
        x: { grid: { color: MUTED + '40' }, ticks: { maxRotation: 45 } },
        y: { grid: { color: MUTED + '40' }, beginAtZero: true }
      }
    }
  });
}

function lineChart(id, labels, data, color, label) {
  const ctx = document.getElementById(id);
  if (!ctx) return;
  new Chart(ctx, {
    type: 'line',
    data: {
      labels,
      datasets: [{
        label, data,
        borderColor: color, backgroundColor: color + '20',
        fill: true, tension: 0.3, pointRadius: 2, pointHoverRadius: 5
      }]
    },
    options: {
      responsive: true, maintainAspectRatio: false,
      plugins: { legend: { display: false } },
      scales: {
        x: { grid: { color: MUTED + '40' }, ticks: { maxRotation: 45 } },
        y: { grid: { color: MUTED + '40' }, beginAtZero: true }
      }
    }
  });
}

function pieChart(id, labels, data, colors) {
  const ctx = document.getElementById(id);
  if (!ctx) return;
  new Chart(ctx, {
    type: 'doughnut',
    data: {
      labels,
      datasets: [{ data, backgroundColor: colors, borderColor: '#0a0f14', borderWidth: 2 }]
    },
    options: {
      responsive: true, maintainAspectRatio: false,
      plugins: {
        legend: { position: 'right', labels: { boxWidth: 12, padding: 10, color: TEXT } }
      }
    }
  });
}

function multiBar(id, labels, datasets) {
  const ctx = document.getElementById(id);
  if (!ctx) return;
  new Chart(ctx, {
    type: 'bar',
    data: { labels, datasets },
    options: {
      responsive: true, maintainAspectRatio: false,
      plugins: { legend: { labels: { color: TEXT, boxWidth: 12 } } },
      scales: {
        x: { grid: { color: MUTED + '40' } },
        y: { grid: { color: MUTED + '40' }, beginAtZero: true }
      }
    }
  });
}

// ── Inject data ──────────────────────────────────────────────────────────────
const DATA = %%JSON_DATA%%;

// Overview: group pie
pieChart('groupPie',
  Object.keys(DATA.group_totals),
  Object.values(DATA.group_totals),
  [ACCENT2, ACCENT, '#ffa040', PURPLE, WARN, DANGER, '#888']
);

// Confidence distribution
const confLabels = DATA.conf_dist.map(r => r.label);
const confCounts = DATA.conf_dist.map(r => r.count);
barChart('confDist', confLabels, confCounts, ACCENT, 'Examples');

// ADS-B
barChart('adsbDaily',
  DATA.adsb_daily.map(r => r.day), DATA.adsb_daily.map(r => r.count),
  ACCENT2, 'ADS-B detections');
barChart('adsbHourly',
  DATA.adsb_hourly.map(r => r.hour + ':00'), DATA.adsb_hourly.map(r => r.count),
  ACCENT2, 'Count');

// Aviation comparison
multiBar('aviationComp',
  ['ADS-B', 'VDL2', 'ACARS-VHF', 'INMARSAT-AERO'],
  [{
    label: 'Detections',
    data: [DATA.band_totals['ADS-B']||0, DATA.band_totals['VDL2']||0,
           DATA.band_totals['ACARS-VHF']||0, DATA.band_totals['INMARSAT-AERO']||0],
    backgroundColor: ACCENT2 + '99', borderColor: ACCENT2, borderWidth: 1
  }]
);

// AIS weekly
barChart('aisWeekly',
  DATA.ais_weekly.map(r => r.week).reverse(),
  DATA.ais_weekly.map(r => r.count).reverse(),
  ACCENT, 'AIS contacts');

// AIS balance
pieChart('aisBalance',
  ['Channel A', 'Channel B'],
  [DATA.band_totals['AIS-A']||0, DATA.band_totals['AIS-B']||0],
  [ACCENT, ACCENT2]);

// Weather
multiBar('weatherComp',
  ['NOAA-APT', 'METEOR-LRPT', 'RADIOSONDE', 'GPS-L1'],
  [{
    label: 'Detections',
    data: ['NOAA-APT','METEOR-LRPT','RADIOSONDE','GPS-L1'].map(b => DATA.band_totals[b]||0),
    backgroundColor: PURPLE + '99', borderColor: PURPLE, borderWidth: 1
  }]
);

// TPMS hourly
barChart('tpmsHourly',
  DATA.tpms_hourly.map(r => r.hour + ':00'),
  DATA.tpms_hourly.map(r => r.count),
  WARN, 'Sensor hits');

// SMETS2 daily
lineChart('smets2Daily',
  DATA.smets2_daily.map(r => r.day),
  DATA.smets2_daily.map(r => r.count),
  WARN, 'Bursts');

// IoT comparison
multiBar('iotComp',
  ['SMETS2', 'TPMS-433', 'ISM-433', 'LORA-868', 'ZIGBEE-868'],
  [{
    label: 'Detections',
    data: ['SMETS2','TPMS-433','ISM-433','LORA-868','ZIGBEE-868'].map(b => DATA.band_totals[b]||0),
    backgroundColor: WARN + '99', borderColor: WARN, borderWidth: 1
  }]
);

// TETRA daily
barChart('tetraDaily',
  DATA.tetra_daily.map(r => r.day),
  DATA.tetra_daily.map(r => r.count),
  DANGER, 'Bursts');

// TETRA hourly
barChart('tetraHourly',
  DATA.tetra_hourly.map(r => r.hour + ':00'),
  DATA.tetra_hourly.map(r => r.count),
  DANGER, 'Bursts');

// PMR446 day pattern
const pmrData = DATA.pmr_pattern;
pieChart('pmrPattern',
  pmrData.map(r => r.label),
  pmrData.map(r => r.count),
  [ACCENT, ACCENT2, PURPLE]);

// Iridium hourly
barChart('iridiumHourly',
  DATA.iridium_hourly.map(r => r.hour + ':00'),
  DATA.iridium_hourly.map(r => r.count),
  '#c060ff', 'Bursts');

// GPS confidence
lineChart('gpsConf',
  DATA.gps_conf.map(r => r.day),
  DATA.gps_conf.map(r => r.avg_conf),
  ACCENT, 'Avg confidence');

</script>
</body>
</html>
"""

# ---------------------------------------------------------------------------
# Badge helper
# ---------------------------------------------------------------------------

BAND_CATEGORY_MAP = {}
CATEGORY_BADGE = {
    "Aviation":                  "aviation",
    "Maritime":                  "maritime",
    "Railway":                   "railway",
    "Weather & Satellite":       "weather",
    "IoT & Smart Infrastructure":"iot",
    "Emergency & Public Safety": "emergency",
    "Broadcasting & Paging":     "broadcast",
}
for cat, bands in BAND_GROUPS.items():
    for band, _ in bands:
        BAND_CATEGORY_MAP[band] = cat


def badge_html(band):
    cat = BAND_CATEGORY_MAP.get(band, "")
    cls = CATEGORY_BADGE.get(cat, "")
    label = cat.split(" &")[0].split(" /")[0]
    return f'<span class="badge badge-{cls}">{label}</span>'


def conf_class(c):
    if c is None: return "conf-lo"
    if c >= 0.7: return "conf-hi"
    if c >= 0.4: return "conf-med"
    return "conf-lo"


# ---------------------------------------------------------------------------
# Build report
# ---------------------------------------------------------------------------

def build(db_path, days, out_path):
    print(f"[meek_report] Opening {db_path}")
    con = open_db(db_path)

    print("[meek_report] Querying…")
    summ       = summary_counts(con, days)
    lt         = league_table(con, days)
    elt        = elt_alerts(con)
    recent     = recent_detections(con, 60)
    sonde      = radiosonde_coverage(con, min(days, 14))
    ais_wk     = ais_weekly(con)

    # Per-band totals for charts
    band_totals = {}
    for entry in lt:
        band_totals[entry["band"]] = entry["count"]

    # Series data
    data = {
        "group_totals":   group_totals(con, days),
        "conf_dist":      confidence_distribution(con, days),
        "adsb_daily":     daily_series(con, "ADS-B", days),
        "adsb_hourly":    hourly_series(con, "ADS-B", days),
        "tpms_hourly":    hourly_series(con, "TPMS-433", days),
        "smets2_daily":   daily_series(con, "SMETS2", days),
        "tetra_daily":    tetra_daily(con, days),
        "tetra_hourly":   hourly_series(con, "TETRA", days),
        "iridium_hourly": hourly_series(con, "IRIDIUM", days),
        "gps_conf":       [],
        "ais_weekly":     ais_wk,
        "pmr_pattern":    [],
        "band_totals":    band_totals,
    }

    # GPS confidence trend
    gps_rows = q(con, """
        SELECT DATE(s.timestamp) AS day,
               ROUND(AVG(e.confidence),4) AS avg_conf
        FROM signals s JOIN examples e ON e.signal_id=s.id
        WHERE s.notes LIKE '%band=GPS-L1%'
          AND s.timestamp >= DATE('now', ?)
        GROUP BY day ORDER BY day DESC LIMIT 30
    """, (f"-{days} days",))
    data["gps_conf"] = [{"day": r["day"], "avg_conf": r["avg_conf"]} for r in gps_rows]

    # PMR446 day pattern
    pmr_rows = q(con, """
        SELECT
            CASE CAST(STRFTIME('%w',timestamp) AS INTEGER)
                WHEN 0 THEN 'Sunday'
                WHEN 6 THEN 'Saturday'
                ELSE 'Weekday'
            END AS lbl,
            COUNT(*) AS n
        FROM signals WHERE notes LIKE '%band=PMR446%'
        GROUP BY lbl
    """)
    data["pmr_pattern"] = [{"label": r["lbl"], "count": r["n"]} for r in pmr_rows]

    con.close()

    # ── Render HTML ──────────────────────────────────────────────────────────
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    # League table rows
    league_rows = []
    for i, entry in enumerate(lt, 1):
        conf_pct = int((entry["avg_conf"] or 0) * 100)
        bar = (f'<div class="conf-bar">'
               f'<div class="conf-bar-track"><div class="conf-bar-fill" style="width:{conf_pct}%"></div></div>'
               f'<span style="font-size:10px;color:var(--muted);min-width:30px;text-align:right">{entry["avg_conf"]:.3f}</span>'
               f'</div>')
        league_rows.append(
            f'<tr>'
            f'<td class="rank">{i}</td>'
            f'<td style="font-weight:600">{entry["band"]}</td>'
            f'<td>{badge_html(entry["band"])}</td>'
            f'<td class="count">{entry["count"]:,}</td>'
            f'<td style="min-width:160px">{bar}</td>'
            f'<td style="color:var(--muted);white-space:nowrap">{entry["last_seen"]}</td>'
            f'</tr>'
        )

    # ELT rows
    if elt:
        elt_rows = "".join(
            f'<div class="alert-row">'
            f'{r["timestamp"]}  conf={r["conf"]}  id={r["id"]}'
            f'</div>'
            for r in elt
        )
    else:
        elt_rows = '<div class="alert-empty">No ELT-406 distress beacons detected in database.</div>'

    # Radiosonde rows
    sonde_rows = "".join(
        f'<tr>'
        f'<td>{r["day"]}</td>'
        f'<td class="{"hit" if r["z00"] else "miss"}">{"✓" if r["z00"] else "✗"}</td>'
        f'<td class="{"hit" if r["z12"] else "miss"}">{"✓" if r["z12"] else "✗"}</td>'
        f'</tr>'
        for r in sonde
    ) or '<tr><td colspan="3" style="color:var(--muted)">No radiosonde data in period</td></tr>'

    # Recent rows
    recent_rows = "".join(
        f'<tr>'
        f'<td class="ts">{r["timestamp"]}</td>'
        f'<td><strong>{r["band"]}</strong></td>'
        f'<td class="conf {conf_class(r["conf"])}">{r["conf"]}</td>'
        f'<td style="color:var(--muted);font-size:10px">{r["notes"]}</td>'
        f'</tr>'
        for r in recent
    )

    html = HTML_TEMPLATE
    html = html.replace("%%GENERATED%%",      now)
    html = html.replace("%%DB_PATH%%",        db_path)
    html = html.replace("%%DAYS%%",           str(days))
    html = html.replace("%%TOTAL_SIGNALS%%",  f"{summ['total_signals']:,}")
    html = html.replace("%%TOTAL_EXAMPLES%%", f"{summ['total_examples']:,}")
    html = html.replace("%%TOTAL_METHODS%%",  f"{summ['total_methods']:,}")
    html = html.replace("%%RECENT_SIGNALS%%", f"{summ['recent_signals']:,}")
    html = html.replace("%%OLDEST%%",         str(summ["oldest"])[:16])
    html = html.replace("%%NEWEST%%",         str(summ["newest"])[:16])
    html = html.replace("%%ELT_COUNT%%",      str(len(elt)))
    html = html.replace("%%LEAGUE_ROWS%%",    "\n".join(league_rows))
    html = html.replace("%%ELT_ROWS%%",       elt_rows)
    html = html.replace("%%SONDE_ROWS%%",     sonde_rows)
    html = html.replace("%%RECENT_ROWS%%",    recent_rows)
    html = html.replace("%%JSON_DATA%%",      json.dumps(data))

    out_path = Path(out_path)
    out_path.write_text(html, encoding="utf-8")
    print(f"[meek_report] Report written → {out_path}  ({out_path.stat().st_size // 1024} KB)")
    return out_path


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    p = argparse.ArgumentParser(description="meek RF signal report generator")
    p.add_argument("--db",   default=DEFAULT_DB,        help="Path to rf_adapt_intel.db")
    p.add_argument("--days", default=DEFAULT_DAYS, type=int, help="Lookback window in days")
    p.add_argument("--out",  default=str(DEFAULT_OUT),  help="Output HTML path")
    args = p.parse_args()
    build(args.db, args.days, args.out)


if __name__ == "__main__":
    main()

