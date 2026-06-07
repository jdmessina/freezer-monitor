import sqlite3
import logging

from datetime import datetime, timedelta
from global_config import DEBUG, DB_FILE

DAYS_TO_KEEP = 30

def cleanup_old_data():
    try:
        cutoff = datetime.now() - timedelta(days=DAYS_TO_KEEP)
        cutoff_str = cutoff.strftime('%Y-%m-%d %H:%M:%S')

        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        c.execute("DELETE FROM temperatures WHERE timestamp < ?", (cutoff_str,))
        deleted_rows = c.rowcount
        conn.commit()

        if DEBUG:
            logging.info("Deleting old records from the database.")

        logging.info(f"Deleted {deleted_rows} old records before {cutoff_str} deleted.")

        return deleted_rows

    except Exception as e:
        logging.error(f"Error during database cleanup: {e}")

        return 0

    finally:
        if 'conn' in locals():
            conn.close()