import psycopg2
import logging
import json
from datetime import datetime

log = logging.getLogger("neon")


class NeonClient:
    def __init__(self, db_url: str):
        self.db_url = db_url
        self.conn = psycopg2.connect(db_url)
        self.conn.autocommit = True
        self.cur = self.conn.cursor()
        log.info("Connected to Neon PostgreSQL")
        self._ensure_tables()

    def _ensure_tables(self):
        try:

            self.cur.execute("""
                CREATE TABLE IF NOT EXISTS environmental_readings (
                    id SERIAL PRIMARY KEY,
                    temperature DOUBLE PRECISION,
                    humidity DOUBLE PRECISION,
                    raw_timestamp TIMESTAMPTZ
                );
            """)

            # Security events table
            self.cur.execute("""
                CREATE TABLE IF NOT EXISTS security_events (
                    id SERIAL PRIMARY KEY,
                    event_type VARCHAR(20) NOT NULL,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    raw_timestamp TIMESTAMPTZ,
                    metadata JSONB
                );
            """)
        except Exception as e:
            log.error("Failed to ensure Neon tables: %s", e)

    def insert_environmental(self, data: dict):
        try:
            self.cur.execute(
                """
                INSERT INTO environmental_readings
                (temperature, humidity, raw_timestamp)
                VALUES (%s, %s, %s)
                """,
                (
                    data.get("temperature"),
                    data.get("humidity"),
                    data.get("timestamp"),
                )
            )
        except Exception as e:
            log.error("Failed to insert environmental: %s", e)

    def insert_security_event(self, event_type: str, sec: dict):
        """
        Insert a motion/smoke event into Neon.
        sec is the dict coming from security_module.get_security_data()
        """
        try:
            raw_ts = sec.get("timestamp")
            if not raw_ts:

                raw_ts = datetime.utcnow().isoformat()

            metadata = json.dumps(sec)

            self.cur.execute(
                """
                INSERT INTO security_events (event_type, raw_timestamp, metadata)
                VALUES (%s, %s, %s)
                """,
                (event_type, raw_ts, metadata)
            )
        except Exception as e:
            log.error("Failed to insert security event: %s", e)

    def close(self):
        try:
            self.cur.close()
            self.conn.close()
        except Exception:
            pass

