-- =============================================================================
-- meek DB query reference
-- DB path: <db_path>              -- e.g. /opt/rf_worker/rf_adapt_intel.db
-- Open:    sqlite3 <db_path>
-- Schema:  signals(id, timestamp, source, notes)
--          methods(id, name, params)
--          examples(id, signal_id, method_id, confidence, notes, created_at)
-- notes / decision_trace contains:  band=<BAND>  snr=<X>dB  -> <mod>@<conf>
-- Band names are defined in include/meek/band_profiles.hpp (kUkBands array).
-- =============================================================================

-- ─────────────────────────────────────────────────────────────────────────────
-- 0.  HOUSEKEEPING / SQLITE SETTINGS
-- ─────────────────────────────────────────────────────────────────────────────

-- Band mapping view — must be created before PRAGMA query_only = ON because
-- CREATE TEMP VIEW writes to the temp schema.  Reused in sections 2, 5, and 9.
-- Update this list in sync with include/meek/band_profiles.hpp (kUkBands).
-- DROP first so re-reading this file in the same session always picks up the
-- latest mapping rather than leaving a stale definition in place.
DROP VIEW IF EXISTS v_band;
CREATE TEMP VIEW v_band AS
SELECT id AS signal_id,
    CASE
        WHEN notes LIKE '%band=ADS-B%'         THEN 'ADS-B'
        WHEN notes LIKE '%band=VDL2%'          THEN 'VDL2'
        WHEN notes LIKE '%band=ACARS-VHF%'     THEN 'ACARS-VHF'
        WHEN notes LIKE '%band=ACARS%'         THEN 'ACARS'
        WHEN notes LIKE '%band=INMARSAT-AERO%' THEN 'INMARSAT-AERO'
        WHEN notes LIKE '%band=AIS-A%'         THEN 'AIS-A'
        WHEN notes LIKE '%band=AIS-B%'         THEN 'AIS-B'
        WHEN notes LIKE '%band=MARINE-CH16%'   THEN 'MARINE-CH16'
        WHEN notes LIKE '%band=MARINE-CH70%'   THEN 'MARINE-CH70'
        WHEN notes LIKE '%band=NOAA-APT%'      THEN 'NOAA-APT'
        WHEN notes LIKE '%band=METEOR-LRPT%'   THEN 'METEOR-LRPT'
        WHEN notes LIKE '%band=RADIOSONDE%'    THEN 'RADIOSONDE'
        WHEN notes LIKE '%band=GPS-L1%'        THEN 'GPS-L1'
        WHEN notes LIKE '%band=IRIDIUM%'       THEN 'IRIDIUM'
        WHEN notes LIKE '%band=SMETS2%'        THEN 'SMETS2'
        WHEN notes LIKE '%band=TPMS-433%'      THEN 'TPMS-433'
        WHEN notes LIKE '%band=ISM-433%'       THEN 'ISM-433'
        WHEN notes LIKE '%band=ISM-169%'       THEN 'ISM-169'
        WHEN notes LIKE '%band=LORA-868%'      THEN 'LORA-868'
        WHEN notes LIKE '%band=ZIGBEE-868%'    THEN 'ZIGBEE-868'
        WHEN notes LIKE '%band=WMBUS-169%'     THEN 'WMBUS-169'
        WHEN notes LIKE '%band=SIGFOX-868%'    THEN 'SIGFOX-868'
        WHEN notes LIKE '%band=ZWAVE-868%'     THEN 'ZWAVE-868'
        WHEN notes LIKE '%band=TETRA%'         THEN 'TETRA'
        WHEN notes LIKE '%band=ELT-406%'       THEN 'ELT-406'
        WHEN notes LIKE '%band=PMR446%'        THEN 'PMR446'
        WHEN notes LIKE '%band=APRS%'          THEN 'APRS'
        WHEN notes LIKE '%band=DAB%'           THEN 'DAB'
        WHEN notes LIKE '%band=POCSAG-153%'    THEN 'POCSAG-153'
        WHEN notes LIKE '%band=FLEX-931%'      THEN 'FLEX-931'
        WHEN notes LIKE '%band=DMR%'           THEN 'DMR'
        WHEN notes LIKE '%band=DECT%'          THEN 'DECT'
        WHEN notes LIKE '%band=CNI-UHF%'       THEN 'CNI-UHF'
        ELSE                                       'unmatched'
    END AS band
FROM signals;

PRAGMA query_only = ON;           -- read-only session (safety net)
PRAGMA journal_mode;              -- inspect current journal mode (capture enables WAL)
.headers on
.mode column
.nullvalue NULL

-- ─────────────────────────────────────────────────────────────────────────────
-- 1.  OVERVIEW — row counts and date range
-- ─────────────────────────────────────────────────────────────────────────────

SELECT 'signals'  AS tbl, COUNT(*) AS total_rows FROM signals
UNION ALL
SELECT 'examples', COUNT(*) FROM examples
UNION ALL
SELECT 'methods',  COUNT(*) FROM methods;

SELECT MIN(timestamp) AS oldest,
       MAX(timestamp) AS newest
FROM signals;

-- Signals captured in the last 24 h / 7 days / 30 days
SELECT
    SUM(CASE WHEN timestamp >= DATETIME('now', '-1 day')   THEN 1 ELSE 0 END) AS last_24h,
    SUM(CASE WHEN timestamp >= DATETIME('now', '-7 days')  THEN 1 ELSE 0 END) AS last_7d,
    SUM(CASE WHEN timestamp >= DATETIME('now', '-30 days') THEN 1 ELSE 0 END) AS last_30d,
    COUNT(*)                                                                   AS all_time
FROM signals;

-- ─────────────────────────────────────────────────────────────────────────────
-- 2.  ACTIVITY BY BAND  (uses LIKE match on decision_trace stored in notes)
--     Replace '-30 days' with '-7 days' or '-1 day' as needed.
-- ─────────────────────────────────────────────────────────────────────────────

-- Count per known band, sorted most-active first
-- Band mapping is centralised in the v_band temp view (see section 0).
SELECT
    bm.band,
    COUNT(*)                     AS detections,
    ROUND(AVG(e.confidence), 3)  AS avg_confidence,
    MAX(s.timestamp)             AS last_seen
FROM signals s
JOIN examples e ON e.signal_id = s.id
JOIN v_band bm  ON bm.signal_id = s.id
WHERE s.timestamp >= DATETIME('now', '-30 days')
GROUP BY bm.band
ORDER BY detections DESC;

-- ─────────────────────────────────────────────────────────────────────────────
-- 3.  MODULATION-CLASS BREAKDOWN
--     The decision_trace encodes the winner as "-> <mod>@<conf>".
--     SQLite LIKE is used to bucket by modulation class.
-- ─────────────────────────────────────────────────────────────────────────────

SELECT
    CASE
        WHEN s.notes LIKE '%-> fsk_like@%'     THEN 'FSK'
        WHEN s.notes LIKE '%-> psk_qam_like@%' THEN 'PSK/QAM'
        WHEN s.notes LIKE '%-> ook_am_like@%'  THEN 'OOK/AM'
        WHEN s.notes LIKE '%-> cw_like@%'      THEN 'CW'
        WHEN s.notes LIKE '%-> unknown@%'      THEN 'unknown'
        ELSE                                        'other'
    END                          AS mod_class,
    COUNT(*)                     AS detections,
    ROUND(AVG(e.confidence), 3)  AS avg_confidence,
    ROUND(MIN(e.confidence), 3)  AS min_confidence,
    ROUND(MAX(e.confidence), 3)  AS max_confidence
FROM signals s
JOIN examples e ON e.signal_id = s.id
WHERE s.timestamp >= DATETIME('now', '-30 days')
GROUP BY mod_class
ORDER BY detections DESC;

-- ─────────────────────────────────────────────────────────────────────────────
-- 4.  CONFIDENCE DISTRIBUTION (bucketed)
-- ─────────────────────────────────────────────────────────────────────────────

SELECT
    CASE
        WHEN e.confidence >= 0.90 THEN 'Very High (>=0.90)'
        WHEN e.confidence >= 0.70 THEN 'High      (0.70-0.89)'
        WHEN e.confidence >= 0.50 THEN 'Medium    (0.50-0.69)'
        WHEN e.confidence >= 0.30 THEN 'Low       (0.30-0.49)'
        ELSE                           'Noise     (<0.30)'
    END           AS confidence_bucket,
    COUNT(*)      AS count,
    ROUND(100.0 * COUNT(*) / SUM(COUNT(*)) OVER (), 1) AS pct
FROM examples e
JOIN signals s ON s.id = e.signal_id
WHERE s.timestamp >= DATETIME('now', '-30 days')
GROUP BY confidence_bucket
ORDER BY MIN(e.confidence) DESC;

-- ─────────────────────────────────────────────────────────────────────────────
-- 5.  SNR PER BAND  (extracted from decision_trace "snr=X.XXXdB")
--     INSTR/SUBSTR are used because SQLite lacks regex by default.
--     Band mapping is centralised in the v_band temp view (see section 0).
-- ─────────────────────────────────────────────────────────────────────────────

-- Average SNR per band over last 7 days, sorted strongest first
SELECT
    bm.band,
    COUNT(*)                                                     AS detections,
    -- Extract numeric SNR: find 'snr=', then locate the 'dB' within that suffix
    ROUND(AVG(
        CAST(SUBSTR(
            s.notes,
            INSTR(s.notes, 'snr=') + 4,
            INSTR(SUBSTR(s.notes, INSTR(s.notes, 'snr=') + 4), 'dB') - 1
        ) AS REAL)
    ), 2)                                                        AS avg_snr_db
FROM signals s
JOIN v_band bm ON bm.signal_id = s.id
WHERE s.timestamp >= DATETIME('now', '-7 days')
  AND s.notes LIKE '%snr=%'
GROUP BY bm.band
ORDER BY avg_snr_db DESC;

-- ─────────────────────────────────────────────────────────────────────────────
-- 6.  REJECTION / GATE ANALYSIS
--     NOTE: Rejected frames are NOT inserted into the SQLite DB.
--     The worker (output_loop in src/main.cpp) only persists detections that
--     pass the confidence threshold AND the SNR gate, so [REJECT:…] tokens
--     never appear in signals.notes.
--
--     To analyse gate behaviour in production use:
--       - Prometheus metrics:  rf_frames_rejected, rf_frames_total
--                              (textfile at /var/lib/rf-adapt-intel/metrics.prom)
--
--     Implementation note:
--       write_json_log() is only called for frames that pass the gates, so
--       rejected frames and any [REJECT:…] decision_trace tokens do NOT appear
--       in worker.log either.  If per-frame reject traces are required, add a
--       separate logging path in the worker for gated-out frames.
-- ─────────────────────────────────────────────────────────────────────────────

-- ─────────────────────────────────────────────────────────────────────────────
-- 7.  DAILY TREND — detections per day over last 30 days
-- ─────────────────────────────────────────────────────────────────────────────

SELECT DATE(timestamp) AS day,
       COUNT(*)        AS detections
FROM signals
WHERE timestamp >= DATE('now', '-30 days')
GROUP BY day
ORDER BY day DESC;

-- ─────────────────────────────────────────────────────────────────────────────
-- 8.  HOURLY PATTERN — hour-of-day distribution (UTC) last 7 days
-- ─────────────────────────────────────────────────────────────────────────────

SELECT CAST(STRFTIME('%H', timestamp) AS INTEGER) AS hour_utc,
       COUNT(*)                                    AS detections
FROM signals
WHERE timestamp >= DATETIME('now', '-7 days')
GROUP BY hour_utc
ORDER BY hour_utc;

-- ─────────────────────────────────────────────────────────────────────────────
-- 9.  BAND × MOD CROSS-TAB  (last 30 days)
--     Band mapping is centralised in the v_band temp view (see section 0).
-- ─────────────────────────────────────────────────────────────────────────────

SELECT
    bm.band,
    CASE
        WHEN s.notes LIKE '%-> fsk_like@%'     THEN 'FSK'
        WHEN s.notes LIKE '%-> psk_qam_like@%' THEN 'PSK/QAM'
        WHEN s.notes LIKE '%-> ook_am_like@%'  THEN 'OOK/AM'
        WHEN s.notes LIKE '%-> cw_like@%'      THEN 'CW'
        ELSE                                        'other'
    END                                      AS mod_class,
    COUNT(*)                                 AS detections,
    ROUND(AVG(e.confidence), 3)              AS avg_confidence
FROM signals s
JOIN examples e ON e.signal_id = s.id
JOIN v_band bm  ON bm.signal_id = s.id
WHERE s.timestamp >= DATETIME('now', '-30 days')
GROUP BY bm.band, mod_class
ORDER BY bm.band, detections DESC;

-- ─────────────────────────────────────────────────────────────────────────────
-- 10. ELT / DISTRESS BEACON ALERTS  (safety-critical — always check)
-- ─────────────────────────────────────────────────────────────────────────────

SELECT s.id,
       s.timestamp,
       ROUND(e.confidence, 3)          AS confidence,
       SUBSTR(s.notes, 1, 200)         AS trace_excerpt
FROM signals s
JOIN examples e ON e.signal_id = s.id
WHERE s.notes LIKE '%band=ELT-406%'
ORDER BY s.timestamp DESC
LIMIT 50;

-- ─────────────────────────────────────────────────────────────────────────────
-- 11. HIGH-CONFIDENCE RECENT DETECTIONS  (conf >= 0.85, last 24 h)
-- ─────────────────────────────────────────────────────────────────────────────

SELECT s.timestamp,
       ROUND(e.confidence, 3)          AS confidence,
       SUBSTR(s.notes, 1, 200)         AS trace_excerpt
FROM signals s
JOIN examples e ON e.signal_id = s.id
WHERE e.confidence >= 0.85
  AND s.timestamp >= DATETIME('now', '-1 day')
ORDER BY e.confidence DESC, s.timestamp DESC
LIMIT 100;

-- ─────────────────────────────────────────────────────────────────────────────
-- 12. LOW-CONFIDENCE / NOISY OBSERVATIONS  (conf < 0.30, last 7 days)
--     Useful for diagnosing antenna/SDR noise floor issues.
-- ─────────────────────────────────────────────────────────────────────────────

SELECT s.timestamp,
       ROUND(e.confidence, 3)          AS confidence,
       SUBSTR(s.notes, 1, 200)         AS trace_excerpt
FROM signals s
JOIN examples e ON e.signal_id = s.id
WHERE e.confidence < 0.30
  AND s.timestamp >= DATETIME('now', '-7 days')
ORDER BY s.timestamp DESC
LIMIT 50;

-- ─────────────────────────────────────────────────────────────────────────────
-- 13. METHODS TABLE — registered classifiers and their parameters
-- ─────────────────────────────────────────────────────────────────────────────

SELECT id, name, params FROM methods ORDER BY id;

-- ─────────────────────────────────────────────────────────────────────────────
-- 14. BAND GROUP TOTALS (last 30 days)
-- ─────────────────────────────────────────────────────────────────────────────

SELECT 'Aviation'              AS band_group,
       SUM(CASE WHEN notes LIKE '%band=ADS-B%'
                  OR notes LIKE '%band=VDL2%'
                  OR notes LIKE '%band=ACARS-VHF%'
                  OR notes LIKE '%band=ACARS%'
                  OR notes LIKE '%band=INMARSAT-AERO%'
               THEN 1 ELSE 0 END) AS detections
FROM signals WHERE timestamp >= DATETIME('now', '-30 days')
UNION ALL
SELECT 'Maritime',
       SUM(CASE WHEN notes LIKE '%band=AIS-A%'
                  OR notes LIKE '%band=AIS-B%'
                  OR notes LIKE '%band=MARINE-CH16%'
                  OR notes LIKE '%band=MARINE-CH70%'
               THEN 1 ELSE 0 END)
FROM signals WHERE timestamp >= DATETIME('now', '-30 days')
UNION ALL
SELECT 'Weather & Satellite',
       SUM(CASE WHEN notes LIKE '%band=NOAA-APT%'
                  OR notes LIKE '%band=METEOR-LRPT%'
                  OR notes LIKE '%band=RADIOSONDE%'
                  OR notes LIKE '%band=GPS-L1%'
                  OR notes LIKE '%band=IRIDIUM%'
               THEN 1 ELSE 0 END)
FROM signals WHERE timestamp >= DATETIME('now', '-30 days')
UNION ALL
SELECT 'IoT & Smart Infrastructure',
       SUM(CASE WHEN notes LIKE '%band=SMETS2%'
                  OR notes LIKE '%band=TPMS-433%'
                  OR notes LIKE '%band=ISM-433%'
                  OR notes LIKE '%band=ISM-169%'
                  OR notes LIKE '%band=LORA-868%'
                  OR notes LIKE '%band=ZIGBEE-868%'
                  OR notes LIKE '%band=WMBUS-169%'
                  OR notes LIKE '%band=SIGFOX-868%'
                  OR notes LIKE '%band=ZWAVE-868%'
               THEN 1 ELSE 0 END)
FROM signals WHERE timestamp >= DATETIME('now', '-30 days')
UNION ALL
SELECT 'Emergency & Public Safety',
       SUM(CASE WHEN notes LIKE '%band=TETRA%'
                  OR notes LIKE '%band=ELT-406%'
                  OR notes LIKE '%band=PMR446%'
                  OR notes LIKE '%band=APRS%'
               THEN 1 ELSE 0 END)
FROM signals WHERE timestamp >= DATETIME('now', '-30 days')
UNION ALL
SELECT 'Broadcasting & Paging',
       SUM(CASE WHEN notes LIKE '%band=DAB%'
                  OR notes LIKE '%band=POCSAG-153%'
                  OR notes LIKE '%band=FLEX-931%'
                  OR notes LIKE '%band=DMR%'
                  OR notes LIKE '%band=DECT%'
               THEN 1 ELSE 0 END)
FROM signals WHERE timestamp >= DATETIME('now', '-30 days')
UNION ALL
SELECT 'Military / Tactical',
       SUM(CASE WHEN notes LIKE '%band=CNI-UHF%'
               THEN 1 ELSE 0 END)
FROM signals WHERE timestamp >= DATETIME('now', '-30 days')
ORDER BY detections DESC;

-- ─────────────────────────────────────────────────────────────────────────────
-- 15. SIGNAL RATE — per-hour burst rate (last 24 h, for spotting RFI spikes)
-- ─────────────────────────────────────────────────────────────────────────────

SELECT STRFTIME('%Y-%m-%d %H:00', timestamp) AS hour_slot,
       COUNT(*)                               AS detections
FROM signals
WHERE timestamp >= DATETIME('now', '-1 day')
GROUP BY hour_slot
ORDER BY hour_slot DESC;

-- ─────────────────────────────────────────────────────────────────────────────
-- 16. RAW TAIL — most recent 20 signal rows (for quick live inspection)
-- ─────────────────────────────────────────────────────────────────────────────

SELECT s.id,
       s.timestamp,
       s.source,
       ROUND(e.confidence, 3) AS confidence,
       SUBSTR(s.notes, 1, 160) AS trace_excerpt
FROM signals s
JOIN examples e ON e.signal_id = s.id
ORDER BY s.id DESC
LIMIT 20;
