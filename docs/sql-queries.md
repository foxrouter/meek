<!-- meek DB query reference — docs/sql-queries.md
     DB path: set DB_PATH to your rf_adapt_intel.db location before running queries
     Open:    sqlite3 "$DB_PATH"
     Schema:  signals(id, timestamp, source, notes)
              methods(id, name, params)
              examples(id, signal_id, method_id, confidence, notes, created_at)
     notes / decision_trace contains:  band=  mod=  snr=  ...
-->

# meek DB query reference

**DB path:** Set `DB_PATH` to your database location, e.g.:
```bash
export DB_PATH=/var/lib/rf_adapt_intel/rf_adapt_intel.db
```
**Open:** `sqlite3 "$DB_PATH"`

**Schema:**
- `signals(id, timestamp, source, notes)`
- `methods(id, name, params)`
- `examples(id, signal_id, method_id, confidence, notes, created_at)`

The `notes` / decision trace field contains key=value pairs: `band=` `mod=` `snr=` etc.

---

## HOUSEKEEPING — always run these first on a new session

```sql
PRAGMA journal_mode;          -- confirm WAL
PRAGMA integrity_check;       -- quick health check
PRAGMA foreign_key_check;     -- referential integrity

-- Row counts at a glance
SELECT 'signals'  AS tbl, COUNT(*) AS rows FROM signals
UNION ALL
SELECT 'methods',          COUNT(*)         FROM methods
UNION ALL
SELECT 'examples',         COUNT(*)         FROM examples;
```

---

## SECTION 1 — AVIATION

```sql
-- ADS-B: all detections, newest first
SELECT s.id, s.timestamp, e.confidence, s.notes
FROM   signals s
JOIN   examples e ON e.signal_id = s.id
WHERE  s.notes LIKE '%band=ADS-B%'
ORDER  BY s.timestamp DESC
LIMIT  50;

-- ADS-B: daily detection count (last 30 days)
SELECT DATE(timestamp) AS day, COUNT(*) AS detections
FROM   signals
WHERE  notes LIKE '%band=ADS-B%'
  AND  timestamp >= DATE('now', '-30 days')
GROUP  BY day
ORDER  BY day;

-- ADS-B: hourly heatmap — which hours see most traffic (Heathrow approach)
SELECT CAST(STRFTIME('%H', timestamp) AS INTEGER) AS hour_utc,
       COUNT(*) AS detections
FROM   signals
WHERE  notes LIKE '%band=ADS-B%'
GROUP  BY hour_utc
ORDER  BY hour_utc;

-- ACARS + VDL2: aviation data messages
SELECT s.timestamp, e.confidence,
       SUBSTR(s.notes, INSTR(s.notes,'band='), 20) AS band_hint
FROM   signals s
JOIN   examples e ON e.signal_id = s.id
WHERE  ((s.notes LIKE '%band=ACARS-VHF %' OR s.notes LIKE '%band=ACARS %')
        OR s.notes LIKE '%band=VDL2 %')
ORDER  BY s.timestamp DESC
LIMIT  40;

-- ACARS vs VDL2 — comparative volume
SELECT
    SUM(CASE WHEN notes LIKE '%band=ACARS-VHF %' OR notes LIKE '%band=ACARS %' THEN 1 ELSE 0 END) AS acars,
    SUM(CASE WHEN notes LIKE '%band=VDL2 %'      THEN 1 ELSE 0 END) AS vdl2
FROM signals;

-- Inmarsat Aero: satellite aviation messages
SELECT s.id, s.timestamp, e.confidence, s.notes
FROM   signals s
JOIN   examples e ON e.signal_id = s.id
WHERE  s.notes LIKE '%band=INMARSAT-AERO%'
ORDER  BY s.timestamp DESC
LIMIT  30;
```

---

## SECTION 2 — MARITIME / RIVER THAMES

```sql
-- AIS both channels, newest first
SELECT s.id, s.timestamp, e.confidence, s.notes
FROM   signals s
JOIN   examples e ON e.signal_id = s.id
WHERE  (s.notes LIKE '%band=AIS-A%' OR s.notes LIKE '%band=AIS-B%')
ORDER  BY s.timestamp DESC
LIMIT  50;

-- AIS: channel A vs B balance (should be roughly equal)
SELECT
    SUM(CASE WHEN notes LIKE '%band=AIS-A%' THEN 1 ELSE 0 END) AS ch_a,
    SUM(CASE WHEN notes LIKE '%band=AIS-B%' THEN 1 ELSE 0 END) AS ch_b
FROM signals;

-- AIS: weekly traffic on the Thames (Berkshire / Reading reach)
SELECT STRFTIME('%Y-W%W', timestamp) AS week, COUNT(*) AS contacts
FROM   signals
WHERE  (notes LIKE '%band=AIS-A%' OR notes LIKE '%band=AIS-B%')
GROUP  BY week
ORDER  BY week DESC
LIMIT  12;
```

---

## SECTION 3 — WEATHER

```sql
-- NOAA APT passes received
SELECT s.id, s.timestamp, e.confidence, s.notes
FROM   signals s
JOIN   examples e ON e.signal_id = s.id
WHERE  s.notes LIKE '%band=NOAA-APT%'
ORDER  BY s.timestamp DESC;

-- NOAA APT: best-quality passes (highest confidence = clearest image)
SELECT s.timestamp, e.confidence
FROM   signals s
JOIN   examples e ON e.signal_id = s.id
WHERE  s.notes LIKE '%band=NOAA-APT%'
ORDER  BY e.confidence DESC
LIMIT  10;

-- Meteor-M2 LRPT passes
SELECT s.timestamp, e.confidence, s.notes
FROM   signals s
JOIN   examples e ON e.signal_id = s.id
WHERE  s.notes LIKE '%band=METEOR-LRPT%'
ORDER  BY s.timestamp DESC
LIMIT  20;

-- Radiosonde balloon telemetry (Larkhill / Herstmonceux launches)
SELECT s.timestamp, e.confidence, s.notes
FROM   signals s
JOIN   examples e ON e.signal_id = s.id
WHERE  s.notes LIKE '%band=RADIOSONDE%'
ORDER  BY s.timestamp DESC
LIMIT  30;

-- Radiosonde: twice-daily launches — are both 00Z and 12Z being caught?
SELECT DATE(timestamp) AS day,
       SUM(
           CASE
               WHEN (CAST(STRFTIME('%H', timestamp) AS INTEGER) >= 22
                     OR CAST(STRFTIME('%H', timestamp) AS INTEGER) <= 2)
               THEN 1 ELSE 0
           END
       ) AS launch_00z,
       SUM(
           CASE
               WHEN CAST(STRFTIME('%H', timestamp) AS INTEGER) BETWEEN 10 AND 14
               THEN 1 ELSE 0
           END
       ) AS launch_12z
FROM   signals
WHERE  notes LIKE '%band=RADIOSONDE%'
GROUP  BY day
ORDER  BY day DESC
LIMIT  14;
```

---

## SECTION 4 — IoT / SMART INFRASTRUCTURE

```sql
-- SMETS2 smart meters
SELECT s.timestamp, e.confidence, s.notes
FROM   signals s
JOIN   examples e ON e.signal_id = s.id
WHERE  s.notes LIKE '%band=SMETS2%'
ORDER  BY s.timestamp DESC
LIMIT  30;

-- SMETS2: transmission rate — are meters chattering regularly?
SELECT DATE(timestamp) AS day, COUNT(*) AS bursts
FROM   signals
WHERE  notes LIKE '%band=SMETS2%'
GROUP  BY day
ORDER  BY day DESC
LIMIT  14;

-- TPMS tyre pressure sensors (vehicles on your road)
SELECT s.timestamp, e.confidence, s.notes
FROM   signals s
JOIN   examples e ON e.signal_id = s.id
WHERE  s.notes LIKE '%band=TPMS-433%'
ORDER  BY s.timestamp DESC
LIMIT  50;

-- TPMS: busiest hours (rush-hour traffic indicator)
SELECT CAST(STRFTIME('%H', timestamp) AS INTEGER) AS hour,
       COUNT(*) AS sensor_hits
FROM   signals
WHERE  notes LIKE '%band=TPMS-433%'
GROUP  BY hour
ORDER  BY sensor_hits DESC;

-- ISM-433 (weather stations, doorbells, garden sensors)
SELECT s.timestamp, e.confidence, s.notes
FROM   signals s
JOIN   examples e ON e.signal_id = s.id
WHERE  s.notes LIKE '%band=ISM-433%'
ORDER  BY s.timestamp DESC
LIMIT  40;

-- LoRa 868 / IoT network
SELECT s.timestamp, e.confidence, s.notes
FROM   signals s
JOIN   examples e ON e.signal_id = s.id
WHERE  s.notes LIKE '%band=LORA-868%'
ORDER  BY s.timestamp DESC
LIMIT  30;

-- ISM-433 + LoRa-868 + ZigBee combined IoT overview
SELECT
    SUBSTR(s.notes, INSTR(s.notes,'band='), 20) AS band_tag,
    COUNT(*)                                     AS count,
    ROUND(AVG(e.confidence), 3)                  AS avg_conf
FROM   signals s
JOIN   examples e ON e.signal_id = s.id
WHERE  (s.notes LIKE '%band=ISM-433%'
     OR s.notes LIKE '%band=LORA-868%'
     OR s.notes LIKE '%band=ZIGBEE-868%'
     OR s.notes LIKE '%band=WMBUS-169%')
GROUP  BY band_tag
ORDER  BY count DESC;
```

---

## SECTION 5 — EMERGENCY & PUBLIC SAFETY

```sql
-- TETRA (Thames Valley Police / SCAS / Berks Fire — burst activity)
SELECT s.timestamp, e.confidence, s.notes
FROM   signals s
JOIN   examples e ON e.signal_id = s.id
WHERE  s.notes LIKE '%band=TETRA%'
ORDER  BY s.timestamp DESC
LIMIT  50;

-- TETRA: hourly burst volume — incident activity proxy
SELECT CAST(STRFTIME('%H', timestamp) AS INTEGER) AS hour,
       COUNT(*) AS bursts
FROM   signals
WHERE  notes LIKE '%band=TETRA%'
GROUP  BY hour
ORDER  BY hour;

-- TETRA: rolling 7-day daily totals — spot unusual busy days
SELECT DATE(timestamp) AS day, COUNT(*) AS bursts
FROM   signals
WHERE  notes LIKE '%band=TETRA%'
  AND  timestamp >= DATE('now', '-14 days')
GROUP  BY day
ORDER  BY day;

-- ELT-406 distress beacons
-- Any result here is significant — export immediately for verification
SELECT s.id, s.timestamp, e.confidence, s.notes
FROM   signals s
JOIN   examples e ON e.signal_id = s.id
WHERE  s.notes LIKE '%band=ELT-406%'
ORDER  BY s.timestamp DESC;

-- PMR446 licence-free radio (events, building sites, schools)
SELECT s.timestamp, e.confidence, s.notes
FROM   signals s
JOIN   examples e ON e.signal_id = s.id
WHERE  s.notes LIKE '%band=PMR446%'
ORDER  BY s.timestamp DESC
LIMIT  30;

-- PMR446: weekend vs weekday pattern (events vs construction)
SELECT
    CASE CAST(STRFTIME('%w', timestamp) AS INTEGER)
        WHEN 0 THEN 'Sunday'
        WHEN 6 THEN 'Saturday'
        ELSE 'Weekday'
    END AS day_type,
    COUNT(*) AS detections
FROM signals
WHERE notes LIKE '%band=PMR446%'
GROUP BY day_type;
```

---

## SECTION 6 — SATELLITE

```sql
-- Iridium LEO bursts
SELECT s.timestamp, e.confidence, s.notes
FROM   signals s
JOIN   examples e ON e.signal_id = s.id
WHERE  s.notes LIKE '%band=IRIDIUM%'
ORDER  BY s.timestamp DESC
LIMIT  50;

-- Iridium: passes per hour — confirm constellation geometry
SELECT CAST(STRFTIME('%H', timestamp) AS INTEGER) AS hour,
       COUNT(*) AS bursts
FROM   signals
WHERE  notes LIKE '%band=IRIDIUM%'
GROUP  BY hour
ORDER  BY hour;

-- GPS-L1: signal quality trend (useful for spoofing/jamming detection)
SELECT DATE(timestamp) AS day,
       ROUND(AVG(e.confidence), 4) AS avg_conf,
       COUNT(*) AS samples
FROM   signals s
JOIN   examples e ON e.signal_id = s.id
WHERE  s.notes LIKE '%band=GPS-L1%'
GROUP  BY day
ORDER  BY day DESC
LIMIT  30;
```

---

## SECTION 7 — CROSS-BAND DIAGNOSTICS & REPORTING

```sql
-- All bands: total detections ranked
SELECT
    CASE
        WHEN s.notes LIKE '%band=ADS-B%'          THEN 'ADS-B'
        WHEN s.notes LIKE '%band=VDL2%'           THEN 'VDL2'
        WHEN s.notes LIKE '%band=ACARS-VHF%'      THEN 'ACARS-VHF'
        WHEN s.notes LIKE '%band=ACARS-129%'      THEN 'ACARS-129'
        WHEN s.notes LIKE '%band=ACARS-130%'      THEN 'ACARS-130'
        WHEN s.notes LIKE '%band=ACARS%'          THEN 'ACARS'
        WHEN s.notes LIKE '%band=AIRBAND-VHF%'    THEN 'AIRBAND-VHF'
        WHEN s.notes LIKE '%band=VOLMET%'         THEN 'VOLMET'
        WHEN s.notes LIKE '%band=AIS-A%'          THEN 'AIS-A'
        WHEN s.notes LIKE '%band=AIS-B%'          THEN 'AIS-B'
        WHEN s.notes LIKE '%band=NOAA-APT%'       THEN 'NOAA-APT'
        WHEN s.notes LIKE '%band=METEOR-LRPT%'    THEN 'METEOR-LRPT'
        WHEN s.notes LIKE '%band=RADIOSONDE%'     THEN 'RADIOSONDE'
        WHEN s.notes LIKE '%band=SMETS2%'         THEN 'SMETS2'
        WHEN s.notes LIKE '%band=TPMS-433%'       THEN 'TPMS-433'
        WHEN s.notes LIKE '%band=ISM-433%'        THEN 'ISM-433'
        WHEN s.notes LIKE '%band=ISM-169%'        THEN 'ISM-169'
        WHEN s.notes LIKE '%band=LORA-868%'       THEN 'LORA-868'
        WHEN s.notes LIKE '%band=TETRA%'          THEN 'TETRA'
        WHEN s.notes LIKE '%band=ELT-406%'        THEN 'ELT-406'
        WHEN s.notes LIKE '%band=PMR446%'         THEN 'PMR446'
        WHEN s.notes LIKE '%band=IRIDIUM%'        THEN 'IRIDIUM'
        WHEN s.notes LIKE '%band=INMARSAT-AERO%'  THEN 'INMARSAT-AERO'
        WHEN s.notes LIKE '%band=GPS-L1%'         THEN 'GPS-L1'
        WHEN s.notes LIKE '%band=APRS%'           THEN 'APRS'
        WHEN s.notes LIKE '%band=MARINE-CH16%'    THEN 'MARINE-CH16'
        WHEN s.notes LIKE '%band=MARINE-CH70%'    THEN 'MARINE-CH70'
        WHEN s.notes LIKE '%band=DMR%'            THEN 'DMR'
        WHEN s.notes LIKE '%band=DAB%'            THEN 'DAB'
        WHEN s.notes LIKE '%band=POCSAG-153%'     THEN 'POCSAG-153'
        WHEN s.notes LIKE '%band=FLEX-931%'       THEN 'FLEX-931'
        WHEN s.notes LIKE '%band=DECT%'           THEN 'DECT'
        WHEN s.notes LIKE '%band=ZIGBEE-868%'     THEN 'ZIGBEE-868'
        WHEN s.notes LIKE '%band=WMBUS-169%'      THEN 'WMBUS-169'
        WHEN s.notes LIKE '%band=SIGFOX-868%'     THEN 'SIGFOX-868'
        WHEN s.notes LIKE '%band=ZWAVE-868%'      THEN 'ZWAVE-868'
        WHEN s.notes LIKE '%band=CNI-UHF%'        THEN 'CNI-UHF'
        WHEN s.notes LIKE '%band=GSM-R-876%'      THEN 'GSM-R-876'
        ELSE '(no band / unknown)'
    END AS band,
    COUNT(*)                        AS detections,
    ROUND(AVG(e.confidence), 3)     AS avg_confidence,
    MAX(s.timestamp)                AS last_seen
FROM   signals s
JOIN   examples e ON e.signal_id = s.id
GROUP  BY band
ORDER  BY detections DESC;

-- Last 24 hours — what has meek seen today?
SELECT s.timestamp,
       CASE
           WHEN INSTR(s.notes, 'band=') > 0 THEN
               SUBSTR(
                   s.notes,
                   INSTR(s.notes, 'band='),
                   INSTR(SUBSTR(s.notes || ' ', INSTR(s.notes, 'band=')), ' ') - 1
               )
           ELSE '(no band)'
       END AS band_hint,
       ROUND(e.confidence, 3) AS conf
FROM   signals s
JOIN   examples e ON e.signal_id = s.id
WHERE  s.timestamp >= DATETIME('now', '-1 day')
ORDER  BY s.timestamp DESC;

-- Confidence distribution — tune your SNR gate thresholds
SELECT
    CASE
        WHEN e.confidence >= 0.9 THEN '0.90-1.00 (very high)'
        WHEN e.confidence >= 0.7 THEN '0.70-0.89 (high)'
        WHEN e.confidence >= 0.5 THEN '0.50-0.69 (medium)'
        WHEN e.confidence >= 0.3 THEN '0.30-0.49 (low)'
        ELSE                          '0.00-0.29 (noise?)'
    END AS confidence_band,
    COUNT(*) AS examples
FROM   examples e
GROUP  BY confidence_band
ORDER  BY confidence_band DESC;

-- Methods registered (classifier variants in use)
SELECT id, name, params FROM methods ORDER BY id;

-- Signals with no examples (orphaned — should be 0)
SELECT COUNT(*) AS orphaned_signals
FROM   signals s
WHERE  NOT EXISTS (SELECT 1 FROM examples e WHERE e.signal_id = s.id);

-- DB size and oldest record
SELECT
    (SELECT COUNT(*) FROM signals)       AS total_signals,
    (SELECT MIN(timestamp) FROM signals) AS oldest,
    (SELECT MAX(timestamp) FROM signals) AS newest;
```

---

## SECTION 8 — SHELL ONE-LINERS

Set `DB_PATH` first, then paste individual commands:

```bash
export DB_PATH=/var/lib/rf_adapt_intel/rf_adapt_intel.db

# ADS-B — last 7 days daily count
sqlite3 "$DB_PATH" \
  "SELECT DATE(timestamp) AS day, COUNT(*) AS n FROM signals WHERE notes LIKE '%band=ADS-B%' GROUP BY day ORDER BY day DESC LIMIT 7;"

# ELT-406 — all distress beacon detections
sqlite3 "$DB_PATH" \
  "SELECT timestamp, confidence FROM examples JOIN signals ON signals.id=examples.signal_id WHERE signals.notes LIKE '%band=ELT-406%' ORDER BY timestamp DESC;"

# Signal count in the last hour
sqlite3 "$DB_PATH" \
  "SELECT COUNT(*) FROM signals WHERE timestamp >= DATETIME('now','-1 hour');"

# GSM-R-876 — last 14 days daily burst count (train timetable correlation)
sqlite3 "$DB_PATH" \
  "SELECT DATE(timestamp) AS day, COUNT(*) AS bursts FROM signals WHERE notes LIKE '%band=GSM-R-876%' AND timestamp >= DATE('now','-14 days') GROUP BY day ORDER BY day;"

# ACARS three-frequency combined hourly volume (Heathrow 131.725 / 129.125 / 130.025)
sqlite3 "$DB_PATH" \
  "SELECT CAST(STRFTIME('%H', timestamp) AS INTEGER) AS hour_utc, COUNT(*) AS messages FROM signals WHERE (notes LIKE '%band=ACARS %' OR notes LIKE '%band=ACARS-129 %' OR notes LIKE '%band=ACARS-130 %') GROUP BY hour_utc ORDER BY hour_utc;"

# Export last 7 days to CSV
sqlite3 -csv -header "$DB_PATH" \
  "SELECT s.timestamp, s.notes, e.confidence FROM signals s JOIN examples e ON e.signal_id=s.id WHERE s.timestamp >= DATE('now','-7 days') ORDER BY s.timestamp;" \
  > ~/meek_week.csv
```

---

## SECTION 9 — RAILWAY (GSM-R)

```sql
-- GSM-R: all detections (GWR / Elizabeth Line through Reading)
SELECT s.timestamp, e.confidence, s.notes
FROM signals s JOIN examples e ON e.signal_id = s.id
WHERE s.notes LIKE '%band=GSM-R-876%'
ORDER BY s.timestamp DESC LIMIT 50;

-- GSM-R: hourly burst pattern — correlates with train timetable
SELECT CAST(STRFTIME('%H', timestamp) AS INTEGER) AS hour,
       COUNT(*) AS bursts
FROM signals WHERE notes LIKE '%band=GSM-R-876%'
GROUP BY hour ORDER BY hour;

-- GSM-R: daily totals — spot engineering possession nights (drop to near zero)
SELECT DATE(timestamp) AS day, COUNT(*) AS bursts
FROM signals
WHERE notes LIKE '%band=GSM-R-876%'
AND timestamp >= DATE('now', '-14 days')
GROUP BY day ORDER BY day;
```

---

## SECTION 10 — AIRBAND (White Waltham / Heathrow TMA)

```sql
-- Airband voice: all detections
SELECT s.timestamp, e.confidence, s.notes
FROM signals s JOIN examples e ON e.signal_id = s.id
WHERE (s.notes LIKE '%band=AIRBAND-VHF%'
    OR s.notes LIKE '%band=VOLMET%')
ORDER BY s.timestamp DESC LIMIT 50;

-- Airband: busiest hours (Heathrow peak push 06-09Z and 16-20Z)
SELECT CAST(STRFTIME('%H', timestamp) AS INTEGER) AS hour_utc,
       COUNT(*) AS contacts
FROM signals
WHERE notes LIKE '%band=AIRBAND-VHF%'
GROUP BY hour_utc ORDER BY hour_utc;

-- VOLMET: confidence trend — sustained drop = aviation band interference
SELECT DATE(timestamp) AS day,
       ROUND(AVG(e.confidence), 4) AS avg_conf,
       COUNT(*) AS samples
FROM signals s JOIN examples e ON e.signal_id = s.id
WHERE s.notes LIKE '%band=VOLMET%'
GROUP BY day ORDER BY day DESC LIMIT 30;

-- ACARS all three Heathrow frequencies combined
SELECT
  SUM(CASE WHEN notes LIKE '%band=ACARS %'    THEN 1 ELSE 0 END) AS acars_131,
  SUM(CASE WHEN notes LIKE '%band=ACARS-129%' THEN 1 ELSE 0 END) AS acars_129,
  SUM(CASE WHEN notes LIKE '%band=ACARS-130%' THEN 1 ELSE 0 END) AS acars_130
FROM signals;

-- ACARS: busiest hours across all three channels
SELECT CAST(STRFTIME('%H', timestamp) AS INTEGER) AS hour_utc,
       COUNT(*) AS messages
FROM signals
WHERE (notes LIKE '%band=ACARS %'
    OR notes LIKE '%band=ACARS-129 %'
    OR notes LIKE '%band=ACARS-130 %')
GROUP BY hour_utc ORDER BY hour_utc;
```

---

## SECTION 11 — RIVER THAMES (marine — Berkshire reach)

```sql
-- CH16 voice + CH70 DSC combined
SELECT s.timestamp, e.confidence,
       SUBSTR(s.notes, INSTR(s.notes,'band='), 15) AS band_hint
FROM signals s JOIN examples e ON e.signal_id = s.id
WHERE (s.notes LIKE '%band=MARINE-CH16%'
    OR s.notes LIKE '%band=MARINE-CH70%')
ORDER BY s.timestamp DESC LIMIT 50;

-- CH16 vs CH70 volume balance
SELECT
  SUM(CASE WHEN notes LIKE '%band=MARINE-CH16%' THEN 1 ELSE 0 END) AS ch16_voice,
  SUM(CASE WHEN notes LIKE '%band=MARINE-CH70%' THEN 1 ELSE 0 END) AS ch70_dsc
FROM signals;

-- DSC distress events only — any CH70 detection warrants review
SELECT s.id, s.timestamp, e.confidence, s.notes
FROM signals s JOIN examples e ON e.signal_id = s.id
WHERE s.notes LIKE '%band=MARINE-CH70%'
ORDER BY s.timestamp DESC;

-- Marine: weekly traffic (Thames navigation season April–October peaks)
SELECT STRFTIME('%Y-W%W', timestamp) AS week, COUNT(*) AS contacts
FROM signals
WHERE (notes LIKE '%band=MARINE-CH16%' OR notes LIKE '%band=MARINE-CH70%')
GROUP BY week ORDER BY week DESC LIMIT 26;
```

---

## SECTION 12 — AMATEUR / APRS

```sql
-- APRS: all packets (Reading digi nodes on 144.800 MHz)
SELECT s.timestamp, e.confidence, s.notes
FROM signals s JOIN examples e ON e.signal_id = s.id
WHERE s.notes LIKE '%band=APRS%'
ORDER BY s.timestamp DESC LIMIT 50;

-- APRS: daily packet count
SELECT DATE(timestamp) AS day, COUNT(*) AS packets
FROM signals
WHERE notes LIKE '%band=APRS%'
GROUP BY day ORDER BY day DESC LIMIT 14;
```

---

## SECTION 13 — PUBLIC SAFETY PAGING (Royal Berkshire Hospital)

```sql
-- POCSAG + FLEX combined (RBH NHS paging)
SELECT s.timestamp, e.confidence,
       SUBSTR(s.notes, INSTR(s.notes,'band='), 15) AS band_hint
FROM signals s JOIN examples e ON e.signal_id = s.id
WHERE (s.notes LIKE '%band=POCSAG-153%'
    OR s.notes LIKE '%band=FLEX-931%')
ORDER BY s.timestamp DESC LIMIT 50;

-- POCSAG vs FLEX volume (FLEX is higher speed, should be lower count)
SELECT
  SUM(CASE WHEN notes LIKE '%band=POCSAG-153%' THEN 1 ELSE 0 END) AS pocsag,
  SUM(CASE WHEN notes LIKE '%band=FLEX-931%'   THEN 1 ELSE 0 END) AS flex
FROM signals;

-- Paging: hourly pattern (hospital shift changes 07:00, 13:00, 19:00)
SELECT CAST(STRFTIME('%H', timestamp) AS INTEGER) AS hour,
       COUNT(*) AS pages
FROM signals
WHERE (notes LIKE '%band=POCSAG-153%' OR notes LIKE '%band=FLEX-931%')
GROUP BY hour ORDER BY hour;
```

---

## SECTION 14 — IoT ADDITIONS (DMR, DECT, Wireless M-Bus, ZigBee, Z-Wave)

```sql
-- DMR digital voice (construction sites, events, Reading FC)
SELECT s.timestamp, e.confidence, s.notes
FROM signals s JOIN examples e ON e.signal_id = s.id
WHERE s.notes LIKE '%band=DMR%'
ORDER BY s.timestamp DESC LIMIT 30;

-- DMR: weekend vs weekday (events vs construction)
SELECT
  CASE CAST(STRFTIME('%w', timestamp) AS INTEGER)
    WHEN 0 THEN 'Sunday'
    WHEN 6 THEN 'Saturday'
    ELSE 'Weekday'
  END AS day_type,
  COUNT(*) AS bursts
FROM signals WHERE notes LIKE '%band=DMR%'
GROUP BY day_type;

-- DECT baseline: detections per day (residential density indicator)
SELECT DATE(timestamp) AS day, COUNT(*) AS detections
FROM signals
WHERE notes LIKE '%band=DECT%'
GROUP BY day ORDER BY day DESC LIMIT 14;

-- Wireless M-Bus / ZigBee / Z-Wave combined smart infrastructure
SELECT
  SUBSTR(notes, INSTR(notes,'band='), 20) AS band_tag,
  COUNT(*) AS count,
  ROUND(AVG(e.confidence), 3) AS avg_conf
FROM signals s JOIN examples e ON e.signal_id = s.id
WHERE (notes LIKE '%band=WMBUS-169%'
    OR notes LIKE '%band=ISM-169%'
    OR notes LIKE '%band=ZIGBEE-868%'
    OR notes LIKE '%band=ZWAVE-868%')
GROUP BY band_tag ORDER BY count DESC;
```
