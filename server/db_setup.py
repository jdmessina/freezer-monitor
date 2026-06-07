import sqlite3
import logging
from global_config import DEBUG, DB_FILE
from datetime import datetime, timedelta
from collections import defaultdict

def init_db():
    try:
        with sqlite3.connect(DB_FILE) as conn:
            c = conn.cursor()
            c.execute("""
            CREATE TABLE IF NOT EXISTS temperatures (
                id INTEGER PRIMARY KEY,
                sensor_id TEXT,
                temperature REAL,
                timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
                status TEXT
            )
            """)

            c.execute("""
                CREATE INDEX IF NOT EXISTS idx_temperatures_timestamp ON temperatures (timestamp)
            """)

            c.execute("""
                CREATE INDEX IF NOT EXISTS idx_temperatures_sensor_timestamp ON temperatures (sensor_id, timestamp)
            """)

            c.execute("""
            CREATE TABLE IF NOT EXISTS alerts (
                sensor_id TEXT PRIMARY KEY,
                last_status TEXT,
                last_state TEXT,
                last_time TEXT
            )
            """)

            # Migration: add state_entry_time column if it doesn't exist
            try:
                c.execute("ALTER TABLE alerts ADD COLUMN state_entry_time TEXT")
            except sqlite3.OperationalError:
                pass  # Column already exists

            c.execute("""
            CREATE TABLE IF NOT EXISTS sensor_settings (
                sensor_id TEXT PRIMARY KEY,
                warning_grace_period INTEGER DEFAULT 0
            )
            """)

        if DEBUG:
           logging.info(f"Initialized DB")
    except Exception as e:
        logging.error(f"Error initializing DB: {e}")
        raise

def get_recent_temps(minutes, offset=0):
    try:
        with sqlite3.connect(DB_FILE) as conn:
            c = conn.cursor()

            # Filter for only rows within the last requested minutes
            end_time = datetime.now() - timedelta(minutes=offset)
            start_time = end_time - timedelta(minutes=minutes)

            c.execute(
                """
                    SELECT sensor_id, temperature, timestamp, status
                    FROM temperatures
                    WHERE timestamp >= ? AND timestamp <= ?
                    ORDER BY timestamp DESC
                """,
                (start_time.strftime("%Y-%m-%d %H:%M:%S"), end_time.strftime("%Y-%m-%d %H:%M:%S"))
            )
            rows = c.fetchall()

            logging.info(f"Retrieving temperature data from {start_time} to {end_time}: Returned {len(rows)} rows.")

            # Return the data in a JSON-compatible format for the frontend
            grouped = defaultdict(list)

            for row in rows:
                entry = {"temperature": row[1], "timestamp": row[2], "status": row[3]}
                grouped[row[0]].append(entry)

            if DEBUG:
                logging.info(f"Reading temperatures from DB for the last {minutes} minutes.")

            return dict(grouped)
    except Exception as e:
        logging.error(f"Error fetching recent temperatures: {e}")
        raise

def store_temperature(sensor_id, temperature, timestamp, status):
    try:
        with sqlite3.connect(DB_FILE) as conn:
            c = conn.cursor()
            c.execute(
                "INSERT INTO temperatures (sensor_id, temperature, timestamp, status) VALUES (?, ?, ?, ?)",
                (sensor_id, temperature, timestamp, status)
            )
            conn.commit()

        if DEBUG:
            logging.info(f"Storing temperature: {temperature} for {sensor_id} at {timestamp} with status {status}")
    except Exception as e:
        logging.error(f"Error storing temperature: {e}")
        raise

def load_last_alerts():
    try:
        with sqlite3.connect(DB_FILE) as conn:
            c = conn.cursor()
            c.execute("SELECT sensor_id, last_status, last_state, last_time, state_entry_time FROM alerts")
            rows = c.fetchall()

        alerts = {}
        for sensor_id, status, state, time_str, entry_time_str in rows:
            alerts[sensor_id] = {
                'status': status,
                'state': state,
                'time': datetime.fromisoformat(time_str) if time_str else None,
                'state_entry_time': datetime.fromisoformat(entry_time_str) if entry_time_str else None
            }

        if DEBUG:
            logging.info(f"Loading last alerts.")

        return alerts
    except Exception as e:
        logging.error(f"Error loading last alerts: {e}")
        raise

def save_last_alert(sensor_id, status, state, time, state_entry_time=None):
    try:
        time_str = time.isoformat() if time else None
        entry_time_str = state_entry_time.isoformat() if state_entry_time else None

        with sqlite3.connect(DB_FILE) as conn:
            c = conn.cursor()
            c.execute("""
                INSERT INTO alerts (sensor_id, last_status, last_state, last_time, state_entry_time)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(sensor_id) DO UPDATE SET
                    last_status=excluded.last_status,
                    last_state=excluded.last_state,
                    last_time=excluded.last_time,
                    state_entry_time=excluded.state_entry_time
            """, (sensor_id, status, state, time_str, entry_time_str))
            conn.commit()

        if DEBUG:
            logging.info(f"Storing last alerts: {sensor_id} at {time_str} with status {status} and state {state}.")
    except Exception as e:
        logging.error(f"Error storing last alerts: {e}")
        raise

def get_sensor_settings(sensor_id):
    try:
        with sqlite3.connect(DB_FILE) as conn:
            c = conn.cursor()
            c.execute("SELECT warning_grace_period FROM sensor_settings WHERE sensor_id = ?", (sensor_id,))
            row = c.fetchone()
        return {'warning_grace_period': row[0] if row else 0}
    except Exception as e:
        logging.error(f"Error loading settings for {sensor_id}: {e}")
        return {'warning_grace_period': 0}

def save_sensor_settings(sensor_id, warning_grace_period):
    try:
        with sqlite3.connect(DB_FILE) as conn:
            c = conn.cursor()
            c.execute("""
                INSERT INTO sensor_settings (sensor_id, warning_grace_period)
                VALUES (?, ?)
                ON CONFLICT(sensor_id) DO UPDATE SET
                    warning_grace_period=excluded.warning_grace_period
            """, (sensor_id, warning_grace_period))
            conn.commit()
        logging.info(f"Saved settings for {sensor_id}: warning_grace_period={warning_grace_period} minutes")
    except Exception as e:
        logging.error(f"Error saving settings for {sensor_id}: {e}")
        raise

def get_all_sensor_settings():
    try:
        with sqlite3.connect(DB_FILE) as conn:
            c = conn.cursor()
            c.execute("SELECT sensor_id, warning_grace_period FROM sensor_settings")
            rows = c.fetchall()
        return {row[0]: {'warning_grace_period': row[1]} for row in rows}
    except Exception as e:
        logging.error(f"Error loading all sensor settings: {e}")
        return {}
