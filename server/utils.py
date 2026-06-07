import logging
import math

from datetime import datetime, timedelta
from db_setup import get_recent_temps, load_last_alerts
from email_alerts import check_and_alert
from global_config import DEBUG, CRITICAL_STATUS, WARNING_STATUS, NOMINAL_STATUS, TEMP_CRITICAL_THRESHOLD, TEMP_WARNING_THRESHOLD, TEMP_OK_THRESHOLD, TEMP_LOW_THRESHOLD
from server_config import MAX_CHART_DATA

# Function to compute the summary status for the dashboard
def compute_summary_status(data):
    now = datetime.now()
    summary_status = NOMINAL_STATUS
    summary_timestamp = now.replace(microsecond=0)
    summary_id = None

    for sensor_id, records in data.items():
        if not records:
            continue

        latest = records[0] # most recent reading
        timestamp = datetime.strptime(latest["timestamp"], "%Y-%m-%d %H:%M:%S")

        if latest["status"] == CRITICAL_STATUS:
            summary_id = sensor_id
            summary_status = CRITICAL_STATUS
            summary_timestamp = datetime.strptime(latest["timestamp"], "%Y-%m-%d %H:%M:%S")
            break
        elif latest["status"] == WARNING_STATUS and summary_status == NOMINAL_STATUS:
            summary_id = sensor_id
            summary_status = WARNING_STATUS
            summary_timestamp = datetime.strptime(latest["timestamp"], "%Y-%m-%d %H:%M:%S")

    if summary_id is None:
        logging.info(f"Computing the summary status for the dashboard: All sensors report {summary_status} at {summary_timestamp}.")
    else:
        logging.info(f"Computing the summary status for the dashboard: Sensor {summary_id} is {summary_status} at {summary_timestamp}.")

    return summary_status

# Function to poll each sensor and check for alerts every minute
def poll_probes():
    if DEBUG:
        logging.info("Running scheduled check for probe alerts")

    try:
        last_reports = get_recent_temps(minutes=60, offset=0)
        known_sensors = load_last_alerts()  # all sensors ever seen, even if quiet >60 min

        # Union: sensors with recent data + sensors that have gone quiet
        all_sensors = set(last_reports.keys()) | set(known_sensors.keys())

        for sensor_id in all_sensors:
            entries = last_reports.get(sensor_id, [])
            if entries:
                last_entry = entries[0]
                last_seen = datetime.strptime(last_entry["timestamp"], "%Y-%m-%d %H:%M:%S")
                logging.info(f"Determining alerts for sensor {sensor_id}: temperature = {last_entry['temperature']}, status = {last_entry['status']}, timestamp = {last_seen}.")
                check_and_alert(sensor_id, last_entry["temperature"], last_entry["status"])
            else:
                # Sensor has dropped out of the recent window — check for offline alert
                logging.info(f"Determining alerts for sensor {sensor_id}: no recent data.")
                check_and_alert(sensor_id, None, None)

    except Exception as e:
        logging.error(f"Error polling the probes: {e}", exc_info=True)

# Function to set the colors to be rendered based on temperature
def get_temp_class(temp):
    if temp < TEMP_LOW_THRESHOLD:
        return 'zone-critical-low'  # Darker blue
    elif TEMP_LOW_THRESHOLD <= temp < TEMP_OK_THRESHOLD:
        return 'zone-warning-low'   # Light blue
    elif TEMP_OK_THRESHOLD <= temp < TEMP_WARNING_THRESHOLD:
        return 'zone-ok'            # Green
    elif TEMP_WARNING_THRESHOLD <= temp < TEMP_CRITICAL_THRESHOLD:
        return 'zone-warning-high'  # Yellow
    else:
        return 'zone-critical-high' # Red

# Function to filter the temperature data set for rendering, based on the time period selected
def filter_data(raw_data, minutes, offset=0):
    now = datetime.now()
    end_time = now - timedelta(minutes=offset)
    start_time = end_time - timedelta(minutes=minutes)

    max_records = max((len(records) for records in raw_data.values()), default=0)
    max_value = MAX_CHART_DATA
    interval = max(1, math.ceil(max_records / max_value))
    data_points = min(max_records, max_value)

    logging.info(f"Requested {minutes} minutes of data to be rendered for {max_records} entries across all sensors:  Filtering data to {data_points} data points at {interval} minute intervals.")

    # Initialize filtered result
    filtered_data = {}

    for sensor_id, records in raw_data.items():
        filtered = []
        last_included = None

        for record in sorted(records, key=lambda r: r['timestamp']):
            ts = datetime.strptime(record['timestamp'], "%Y-%m-%d %H:%M:%S")
            if ts < start_time or ts > end_time:
                continue

            # Only include one record per interval
            if not last_included or (ts - last_included).total_seconds() >= interval * 60:
                filtered.append(record)
                last_included = ts

        filtered_data[sensor_id] = filtered

    return filtered_data