import sqlite3
import logging
from global_config import DEBUG, DB_FILE, WARNING_GRACE_PERIOD, CRITICAL_GRACE_PERIOD, WARNING_ALERT_DIFFERENTIAL, CRITICAL_ALERT_DIFFERENTIAL
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

            for col_def in ["alert_sent INTEGER DEFAULT 0"]:
                try:
                    c.execute(f"ALTER TABLE temperatures ADD COLUMN {col_def}")
                except sqlite3.OperationalError:
                    pass

            c.execute("""
            CREATE TABLE IF NOT EXISTS alerts (
                sensor_id TEXT PRIMARY KEY,
                last_status TEXT,
                last_state TEXT,
                last_time TEXT,
                state_entry_time TEXT
            )
            """)

            for col_def in [
                "state_entry_time TEXT",
                "next_alert_temp REAL",
                "acknowledged INTEGER DEFAULT 0",
            ]:
                try:
                    c.execute(f"ALTER TABLE alerts ADD COLUMN {col_def}")
                except sqlite3.OperationalError:
                    pass

            c.execute("""
            CREATE TABLE IF NOT EXISTS sensor_settings (
                sensor_id TEXT PRIMARY KEY,
                warning_grace_period INTEGER DEFAULT 0
            )
            """)

            for col_def in [
                "critical_grace_period INTEGER DEFAULT 5",
                "warning_differential REAL DEFAULT 1.0",
                "critical_differential REAL DEFAULT 0.5",
                "verbose INTEGER DEFAULT 1",
            ]:
                try:
                    c.execute(f"ALTER TABLE sensor_settings ADD COLUMN {col_def}")
                except sqlite3.OperationalError:
                    pass

        if DEBUG:
           logging.info(f"Initialized DB")
    except Exception as e:
        logging.error(f"Error initializing DB: {e}")
        raise

def get_recent_temps(minutes, offset=0):
    try:
        with sqlite3.connect(DB_FILE) as conn:
            c = conn.cursor()

            end_time = datetime.now() - timedelta(minutes=offset)
            start_time = end_time - timedelta(minutes=minutes)

            c.execute(
                """
                    SELECT sensor_id, temperature, timestamp, status, COALESCE(alert_sent, 0)
                    FROM temperatures
                    WHERE timestamp >= ? AND timestamp <= ?
                    ORDER BY timestamp DESC
                """,
                (start_time.strftime("%Y-%m-%d %H:%M:%S"), end_time.strftime("%Y-%m-%d %H:%M:%S"))
            )
            rows = c.fetchall()

            logging.info(f"Retrieving temperature data from {start_time} to {end_time}: Returned {len(rows)} rows.")

            grouped = defaultdict(list)
            for row in rows:
                entry = {"temperature": row[1], "timestamp": row[2], "status": row[3], "alert_sent": row[4]}
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

def mark_alert_sent(sensor_id):
    try:
        with sqlite3.connect(DB_FILE) as conn:
            c = conn.cursor()
            c.execute("""
                UPDATE temperatures SET alert_sent = 1
                WHERE id = (
                    SELECT id FROM temperatures WHERE sensor_id = ?
                    ORDER BY timestamp DESC LIMIT 1
                )
            """, (sensor_id,))
            conn.commit()
    except Exception as e:
        logging.error(f"Error marking alert_sent for {sensor_id}: {e}")

def load_last_alerts():
    try:
        with sqlite3.connect(DB_FILE) as conn:
            c = conn.cursor()
            c.execute("SELECT sensor_id, last_status, last_state, last_time, state_entry_time, next_alert_temp, acknowledged FROM alerts")
            rows = c.fetchall()

        alerts = {}
        for sensor_id, status, state, time_str, entry_time_str, next_alert_temp, acknowledged in rows:
            alerts[sensor_id] = {
                'status': status,
                'state': state,
                'time': datetime.fromisoformat(time_str) if time_str else None,
                'state_entry_time': datetime.fromisoformat(entry_time_str) if entry_time_str else None,
                'next_alert_temp': next_alert_temp,
                'acknowledged': acknowledged or 0,
            }

        if DEBUG:
            logging.info(f"Loading last alerts.")

        return alerts
    except Exception as e:
        logging.error(f"Error loading last alerts: {e}")
        raise

def save_last_alert(sensor_id, status, state, time, state_entry_time=None, next_alert_temp=None):
    try:
        time_str = time.isoformat() if time else None
        entry_time_str = state_entry_time.isoformat() if state_entry_time else None

        with sqlite3.connect(DB_FILE) as conn:
            c = conn.cursor()
            c.execute("""
                INSERT INTO alerts (sensor_id, last_status, last_state, last_time, state_entry_time, next_alert_temp)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(sensor_id) DO UPDATE SET
                    last_status=excluded.last_status,
                    last_state=excluded.last_state,
                    last_time=excluded.last_time,
                    state_entry_time=excluded.state_entry_time,
                    next_alert_temp=excluded.next_alert_temp
            """, (sensor_id, status, state, time_str, entry_time_str, next_alert_temp))
            conn.commit()

        if DEBUG:
            logging.info(f"Storing last alert: {sensor_id} status={status} state={state} time={time_str} D1={next_alert_temp}")
    except Exception as e:
        logging.error(f"Error storing last alerts: {e}")
        raise

def set_acknowledged(sensor_id, value):
    try:
        with sqlite3.connect(DB_FILE) as conn:
            c = conn.cursor()
            c.execute("UPDATE alerts SET acknowledged = ? WHERE sensor_id = ?", (value, sensor_id))
            conn.commit()
        logging.info(f"Set acknowledged={value} for {sensor_id}")
    except Exception as e:
        logging.error(f"Error setting acknowledged for {sensor_id}: {e}")

def get_sensor_settings(sensor_id):
    try:
        with sqlite3.connect(DB_FILE) as conn:
            c = conn.cursor()
            c.execute("""
                SELECT warning_grace_period, critical_grace_period, warning_differential, critical_differential, verbose
                FROM sensor_settings WHERE sensor_id = ?
            """, (sensor_id,))
            row = c.fetchone()
        if row:
            return {
                'warning_grace_period': row[0],
                'critical_grace_period': row[1],
                'warning_differential': row[2],
                'critical_differential': row[3],
                'verbose': row[4],
            }
        return {
            'warning_grace_period': WARNING_GRACE_PERIOD // 60,
            'critical_grace_period': CRITICAL_GRACE_PERIOD // 60,
            'warning_differential': WARNING_ALERT_DIFFERENTIAL,
            'critical_differential': CRITICAL_ALERT_DIFFERENTIAL,
            'verbose': 1,
        }
    except Exception as e:
        logging.error(f"Error loading settings for {sensor_id}: {e}")
        return {
            'warning_grace_period': WARNING_GRACE_PERIOD // 60,
            'critical_grace_period': CRITICAL_GRACE_PERIOD // 60,
            'warning_differential': WARNING_ALERT_DIFFERENTIAL,
            'critical_differential': CRITICAL_ALERT_DIFFERENTIAL,
        }

def save_sensor_settings(sensor_id, warning_grace_period, critical_grace_period, warning_differential, critical_differential, verbose=1):
    try:
        with sqlite3.connect(DB_FILE) as conn:
            c = conn.cursor()
            c.execute("""
                INSERT INTO sensor_settings (sensor_id, warning_grace_period, critical_grace_period, warning_differential, critical_differential, verbose)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(sensor_id) DO UPDATE SET
                    warning_grace_period=excluded.warning_grace_period,
                    critical_grace_period=excluded.critical_grace_period,
                    warning_differential=excluded.warning_differential,
                    critical_differential=excluded.critical_differential,
                    verbose=excluded.verbose
            """, (sensor_id, warning_grace_period, critical_grace_period, warning_differential, critical_differential, verbose))
            conn.commit()
        logging.info(f"Saved settings for {sensor_id}: warning_grace={warning_grace_period}m, critical_grace={critical_grace_period}m, warning_diff={warning_differential}°C, critical_diff={critical_differential}°C, verbose={verbose}")
    except Exception as e:
        logging.error(f"Error saving settings for {sensor_id}: {e}")
        raise

def get_all_sensor_settings():
    try:
        with sqlite3.connect(DB_FILE) as conn:
            c = conn.cursor()
            c.execute("SELECT sensor_id, warning_grace_period, critical_grace_period, warning_differential, critical_differential, verbose FROM sensor_settings")
            rows = c.fetchall()
        return {row[0]: {
            'warning_grace_period': row[1],
            'critical_grace_period': row[2],
            'warning_differential': row[3],
            'critical_differential': row[4],
            'verbose': row[5],
        } for row in rows}
    except Exception as e:
        logging.error(f"Error loading all sensor settings: {e}")
        return {}
