from flask import Flask, render_template, jsonify, request
import sqlite3
import json
from pathlib import Path
import requests
import random

import psycopg2
from psycopg2.extras import RealDictCursor

app = Flask(__name__)

# -------------------- LOCAL SQLITE (temperature charts etc.) --------------------


def get_db_connection():
    conn = sqlite3.connect("iot_data.db")
    conn.row_factory = sqlite3.Row
    return conn


# -------------------- SHARED CONFIG --------------------

CONFIG_PATH = Path(__file__).with_name("config.json")
cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))

AIO_USERNAME = cfg["ADAFRUIT_IO_USERNAME"]
AIO_KEY = cfg["ADAFRUIT_IO_KEY"]

DEVICE_FEEDS = {
    "buzzer": cfg.get("BUZZER_CONTROL_FEED", "buzzer_control"),
    "led_green": cfg["LED_FEEDS"]["green"],
    "led_yellow": cfg["LED_FEEDS"]["yellow"],
    "led_red": cfg["LED_FEEDS"]["red"],
}

LCD_FEED = cfg.get("FEED_KEY", "LCD_display")

NEON_DB_URL = cfg["NEON_DB_URL"]


def send_to_adafruit(feed_key: str, value: str):
    url = f"https://io.adafruit.com/api/v2/{AIO_USERNAME}/feeds/{feed_key}/data"
    headers = {"X-AIO-Key": AIO_KEY, "Content-Type": "application/json"}
    payload = {"value": value}
    r = requests.post(url, headers=headers, json=payload, timeout=5)
    r.raise_for_status()


# -------------------- NEON HELPERS (security tables) --------------------


def get_neon_connection():
    """
    Simple Neon connection helper for security tables.
    Uses RealDictCursor so rows behave like dicts: row["column"].
    """
    conn = psycopg2.connect(NEON_DB_URL, cursor_factory=RealDictCursor)
    return conn


def ensure_security_state(conn):
    """
    Make sure security_state table exists and has the expected columns:
      - id INTEGER PRIMARY KEY (always 1)
      - mode VARCHAR(20) NOT NULL DEFAULT 'disarmed'
      - updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    Also ensures there is a row with id=1.
    This function is idempotent and can be called safely many times.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS security_state (
                id INTEGER PRIMARY KEY
            );
            """
        )

        cur.execute(
            """
            SELECT column_name
            FROM information_schema.columns
            WHERE table_name = 'security_state'
              AND column_name = 'mode';
            """
        )
        if cur.fetchone() is None:
            cur.execute(
                "ALTER TABLE security_state "
                "ADD COLUMN mode VARCHAR(20) NOT NULL DEFAULT 'disarmed';"
            )

        cur.execute(
            """
            SELECT column_name
            FROM information_schema.columns
            WHERE table_name = 'security_state'
              AND column_name = 'updated_at';
            """
        )
        if cur.fetchone() is None:
            cur.execute(
                "ALTER TABLE security_state "
                "ADD COLUMN updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW();"
            )

        cur.execute("SELECT id FROM security_state WHERE id = 1;")
        if cur.fetchone() is None:
            cur.execute(
                "INSERT INTO security_state (id, mode) VALUES (%s, %s);",
                (1, "disarmed"),
            )

    conn.commit()


def get_security_mode(conn) -> str:
    """
    Read current mode ('armed' / 'disarmed') from security_state.
    """
    ensure_security_state(conn)
    with conn.cursor() as cur:
        cur.execute("SELECT mode FROM security_state WHERE id = 1;")
        row = cur.fetchone()
    return row["mode"] if row and row.get("mode") else "disarmed"


def set_security_mode(conn, mode: str):
    """
    Update current mode in security_state.
    """
    ensure_security_state(conn)
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE security_state SET mode = %s, updated_at = NOW() WHERE id = 1;",
            (mode,),
        )
    conn.commit()


# -------------------- FLASK VIEWS --------------------


@app.route("/")
def home():
    return render_template("summary.html")


@app.route("/environment")
def environment_page():
    return render_template("environment.html")


@app.route("/api/environment/history")
def api_env_history():
    """
    Return historical readings for a given date from Neon.
    Query param:
      ?date=YYYY-MM-DD   -> filter on that calendar day (raw_timestamp::date)
    If no date is provided, we fall back to the last 24 hours.
    """
    date_str = request.args.get("date")

    try:
        conn = psycopg2.connect(NEON_DB_URL, cursor_factory=RealDictCursor)
    except Exception as e:
        return jsonify({"error": f"Neon connection failed: {e}"}), 500

    with conn, conn.cursor() as cur:
        if date_str:

            cur.execute(
                """
                SELECT
                    raw_timestamp,
                    temperature,
                    humidity
                FROM environmental_readings
                WHERE raw_timestamp::date = %s::date
                ORDER BY raw_timestamp ASC;
                """,
                (date_str,),
            )
        else:

            cur.execute(
                """
                SELECT
                    raw_timestamp,
                    temperature,
                    humidity
                FROM environmental_readings
                WHERE raw_timestamp >= NOW() - INTERVAL '24 hours'
                ORDER BY raw_timestamp ASC;
                """
            )

        rows = cur.fetchall()

    conn.close()

    labels = []
    temps = []
    hums = []
    pressures = []

    for r in rows:
        ts = r.get("raw_timestamp")

        if ts is None:
            ts_str = None
        elif isinstance(ts, str):

            ts_str = ts
        else:

            ts_str = ts.isoformat()

        labels.append(ts_str)
        temps.append(r.get("temperature"))
        hums.append(r.get("humidity"))

        base = 1013.0
        jitter = (len(pressures) % 5) * 0.4
        pressures.append(round(base + jitter, 2))

    return jsonify(
        {
            "labels": labels,
            "temperature": temps,
            "humidity": hums,
            "pressure": pressures,
        }
    )

@app.route('/api/environment/summary')
def api_env_summary():
    """
    Return the most recent environmental reading from Neon.
    No 24-hour limit. Always picks the newest row.
    Simulates pressure value.
    """
    try:
        conn = psycopg2.connect(NEON_DB_URL, cursor_factory=RealDictCursor)
    except Exception as e:
        pressure = 1013.0 + random.uniform(-3.0, 3.0)
        return jsonify({
            "timestamp": None,
            "temperature": None,
            "humidity": None,
            "pressure": round(pressure, 2),
            "error": f"Neon connection failed: {e}",
        }), 200

    with conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT raw_timestamp, temperature, humidity
            FROM environmental_readings
            ORDER BY raw_timestamp DESC
            LIMIT 1;
            """
        )
        row = cur.fetchone()

    conn.close()

    if not row:
        pressure = 1013.0 + random.uniform(-3.0, 3.0)
        return jsonify({
            "timestamp": None,
            "temperature": None,
            "humidity": None,
            "pressure": round(pressure, 2),
        })

    ts = row.get("raw_timestamp")
    if isinstance(ts, str):
        ts_str = ts
    else:
        ts_str = ts.isoformat() if ts else None

    pressure = 1013.0 + random.uniform(-3.0, 3.0)

    return jsonify({
        "timestamp": ts_str,
        "temperature": row.get("temperature"),
        "humidity": row.get("humidity"),
        "pressure": round(pressure, 2),
    })

@app.route("/devices")
def devices_control():
    return render_template("devices.html")


@app.route("/security")
def security_control():
    return render_template("security.html")


# -------------------- CHART DATA (SQLite) --------------------


# -------------------- DEVICE CONTROL (Adafruit IO) --------------------


@app.route("/api/device-control", methods=["POST"])
def api_device_control():
    data = request.get_json() or {}
    device = data.get("device")
    state = data.get("state")
    if device not in DEVICE_FEEDS:
        return jsonify({"ok": False, "error": "Unknown device"}), 400
    if state not in ("on", "off"):
        return jsonify({"ok": False, "error": "Invalid state"}), 400

    feed = DEVICE_FEEDS[device]
    value = "ON" if state == "on" else "OFF"

    try:
        send_to_adafruit(feed, value)
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/lcd-message", methods=["POST"])
def api_lcd_message():
    data = request.get_json() or {}
    message = data.get("message", "").strip()
    if not message:
        return jsonify({"ok": False, "error": "Empty message"}), 400
    try:
        send_to_adafruit(LCD_FEED, message)
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


# -------------------- SECURITY API (Neon) --------------------


@app.route("/api/security/mode", methods=["GET", "POST"])
def api_security_mode():
    conn = get_neon_connection()
    if request.method == "GET":
        mode = get_security_mode(conn)
        conn.close()
        return jsonify({"mode": mode})

    data = request.get_json() or {}
    mode = (data.get("mode") or "").lower()
    if mode not in ("armed", "disarmed"):
        conn.close()
        return jsonify({"ok": False, "error": "Invalid mode"}), 400

    set_security_mode(conn, mode)
    conn.close()
    return jsonify({"ok": True, "mode": mode})


@app.route("/api/security/overview")
def api_security_overview():
    """
    Summary for "Today at a glance":
      - motion_count
      - smoke_count
      - last_intrusion (ISO string or null)
      - mode (armed/disarmed)

    Uses events from the last 24 hours.
    """
    conn = get_neon_connection()
    mode = get_security_mode(conn)

    with conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT
                COALESCE(SUM(CASE WHEN event_type = 'motion' THEN 1 ELSE 0 END), 0)
                    AS motion_count,
                COALESCE(SUM(CASE WHEN event_type = 'smoke' THEN 1 ELSE 0 END), 0)
                    AS smoke_count,
                MAX(raw_timestamp) AS last_intrusion
            FROM security_events
            WHERE raw_timestamp >= NOW() - INTERVAL '24 hours'
              AND event_type IN ('motion', 'smoke');
            """
        )
        row = cur.fetchone()

    conn.close()

    last_intrusion = (
        row["last_intrusion"].isoformat() if row and row["last_intrusion"] else None
    )

    return jsonify(
        {
            "mode": mode,
            "motion_count": row["motion_count"] if row else 0,
            "smoke_count": row["smoke_count"] if row else 0,
            "last_intrusion": last_intrusion,
        }
    )


@app.route("/api/security/logs")
def api_security_logs():
    """
    Return events for a specific date (YYYY-MM-DD) for the log list.
    """
    date_str = request.args.get("date")
    if not date_str:
        return jsonify([])

    conn = get_neon_connection()
    with conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT event_type, raw_timestamp
            FROM security_events
            WHERE raw_timestamp::date = %s::date
              AND event_type IN ('motion', 'smoke')
            ORDER BY raw_timestamp ASC;
            """,
            (date_str,),
        )
        rows = cur.fetchall()

    conn.close()

    events = []
    for r in rows:
        events.append(
            {
                "event_type": r["event_type"],
                "timestamp": r["raw_timestamp"].isoformat()
                if r["raw_timestamp"]
                else None,
                "label": "Smoke detected"
                if r["event_type"] == "smoke"
                else "Motion detected",
            }
        )

    return jsonify(events)


@app.route("/api/security/graph-data")
def api_security_graph_data():
    """
    Aggregate counts per 5-minute bucket for the last N hours (default 24).
    """
    hours = request.args.get("hours", default=24, type=int)
    if hours <= 0:
        hours = 24

    conn = get_neon_connection()
    with conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT
                date_trunc('minute', raw_timestamp) AS bucket,
                SUM(CASE WHEN event_type = 'motion' THEN 1 ELSE 0 END) AS motion_count,
                SUM(CASE WHEN event_type = 'smoke'  THEN 1 ELSE 0 END) AS smoke_count
            FROM security_events
            WHERE raw_timestamp >= NOW() - (%s || ' hours')::interval
              AND event_type IN ('motion', 'smoke')
            GROUP BY bucket
            ORDER BY bucket;
            """,
            (hours,),
        )
        rows = cur.fetchall()

    conn.close()

    labels = []
    motion = []
    smoke = []
    for r in rows:
        labels.append(r["bucket"].isoformat() if r["bucket"] else None)
        motion.append(r["motion_count"] or 0)
        smoke.append(r["smoke_count"] or 0)

    return jsonify({"labels": labels, "motion": motion, "smoke": smoke})


# -------------------- MAIN --------------------

if __name__ == "__main__":
    app.run(debug=True)

