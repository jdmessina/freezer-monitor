import logging
import json

from logging.handlers import RotatingFileHandler
from flask import Flask, render_template, request, jsonify, redirect
from apscheduler.schedulers.background import BackgroundScheduler
from datetime import datetime
from db_setup import init_db, get_recent_temps, store_temperature, load_last_alerts, get_all_sensor_settings, save_sensor_settings
from db_cleanup import cleanup_old_data
from email_alerts import get_sensor_state, check_and_alert
from utils import compute_summary_status, poll_probes, get_temp_class, filter_data
from global_config import DEBUG, TEMP_TOP_THRESHOLD, TEMP_CRITICAL_THRESHOLD, TEMP_WARNING_THRESHOLD, TEMP_OK_THRESHOLD, TEMP_LOW_THRESHOLD, TEMP_BOTTOM_THRESHOLD, NOMINAL_STATUS, WARNING_STATUS, CRITICAL_STATUS, SUBMIT_API_KEY
from server_config import ZONE_CRITICAL_LOW, ZONE_WARNING_LOW, ZONE_OK, ZONE_WARNING_HIGH, ZONE_CRITICAL_HIGH, SERVER_LOG_FILE, REPORTING_CONFIG, SENSOR_DISPLAY_NAMES

app = Flask(__name__)

# Configure logging
log_file = SERVER_LOG_FILE

# Rotate when the log file reaches 5MB, keep 5 backups
handler =  RotatingFileHandler(log_file, maxBytes=5*1024*1024, backupCount=5)
formatter = logging.Formatter('%(asctime)s %(levelname)s: %(message)s')
handler.setFormatter(formatter)

logger = logging.getLogger()
logger.setLevel(logging.INFO)
logger.addHandler(handler)

if DEBUG:
    logging.info(f"Freezer Monitor Service Started: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

# Create a scheduler instance
scheduler = BackgroundScheduler()

# Add task to read the temperature once every minute
scheduler.add_job(func=poll_probes, trigger="interval", minutes=1, id="read_temp")

if DEBUG:
    logging.info("Temperature read job scheduled")

# Add task to delete old data once every day
scheduler.add_job(func=cleanup_old_data, trigger="interval", minutes=1440, id="db_cleanup")

if DEBUG:
    logging.info("Database cleanup job scheduled")

# Read in the reporting range from the configuration file
with open(REPORTING_CONFIG) as f:
    reporting_config = json.load(f)

range_options = reporting_config.get("range_options", [])
offset_options = reporting_config.get("offset_options", [])
range_labels = dict(range_options)
offset_labels = dict(offset_options)

TEMP_ZONES = [
    {"class": "zone-critical-low",  "color": ZONE_CRITICAL_LOW,  "start": TEMP_BOTTOM_THRESHOLD, "end": TEMP_LOW_THRESHOLD},
    {"class": "zone-warning-low",   "color": ZONE_WARNING_LOW,   "start": TEMP_LOW_THRESHOLD,    "end": TEMP_OK_THRESHOLD},
    {"class": "zone-ok",            "color": ZONE_OK,            "start": TEMP_OK_THRESHOLD,     "end": TEMP_WARNING_THRESHOLD},
    {"class": "zone-warning-high",  "color": ZONE_WARNING_HIGH,  "start": TEMP_WARNING_THRESHOLD,"end": TEMP_CRITICAL_THRESHOLD},
    {"class": "zone-critical-high", "color": ZONE_CRITICAL_HIGH, "start": TEMP_CRITICAL_THRESHOLD,"end": TEMP_TOP_THRESHOLD}
]

def _get_display_name(sensor_id):
    return SENSOR_DISPLAY_NAMES.get(sensor_id, sensor_id.replace('_', ' ').title())

def _build_dashboard_data(minutes, offset):
    timestamp_format = "%Y-%m-%d %H:%M:%S"
    raw_data = get_recent_temps(minutes, offset)
    summary_status = compute_summary_status(raw_data)
    data = filter_data(raw_data, minutes, offset)

    chart_data = {}
    table_data = {}

    for sensor in sorted(data.keys()):
        records = data[sensor]
        chart_data[sensor] = [
            {"x": datetime.strptime(r["timestamp"], timestamp_format).isoformat(), "y": r["temperature"]}
            for r in reversed(records)
        ]
        state = get_sensor_state(sensor)
        for record in records:
            record["temp_class"] = get_temp_class(record["temperature"])
        table_data[sensor] = {
            "state": state,
            "state_class": state,
            "display_name": _get_display_name(sensor),
            "records": list(reversed(records))
        }

    return summary_status, chart_data, table_data

def _parse_range_offset(request):
    try:
        minutes = int(request.args.get('range', '60'))
    except ValueError:
        minutes = 60
    try:
        offset = int(request.args.get('offset', '0'))
    except ValueError:
        offset = 0
    return minutes, offset

@app.route("/")
def index():
    minutes, offset = _parse_range_offset(request)
    range_title = range_labels.get(minutes, f"Previous {minutes} Minutes")
    offset_title = offset_labels.get(offset, f"{offset} Minutes Ago")
    title = f"Freezer Monitor - Starting {offset_title} and Covering the {range_title}"

    summary_status, chart_data, table_data = _build_dashboard_data(minutes, offset)

    logging.info("Updating the web site with the latest temperature data")
    return render_template("index.html", table_data=table_data, chart_data=chart_data,
                           summary_status=summary_status, temp_zones=TEMP_ZONES, title=title,
                           range_options=range_options, selected_range=minutes,
                           offset_options=offset_options, selected_offset=offset)

@app.route("/data")
def data():
    minutes, offset = _parse_range_offset(request)
    summary_status, chart_data, table_data = _build_dashboard_data(minutes, offset)
    # Re-key chart_data by display_name to match chart dataset labels in the JS
    chart_data_by_name = {table_data[sid]["display_name"]: pts for sid, pts in chart_data.items()}
    return jsonify({"summary_status": summary_status, "chart_data": chart_data_by_name, "table_data": table_data})

@app.route("/settings", methods=["GET"])
def settings_page():
    all_alerts = load_last_alerts()
    all_settings = get_all_sensor_settings()

    settings = {}
    for sensor_id in sorted(all_alerts.keys()):
        settings[sensor_id] = {
            'display_name': _get_display_name(sensor_id),
            'warning_grace_period': all_settings.get(sensor_id, {}).get('warning_grace_period', 0)
        }

    saved = request.args.get('saved') == '1'
    return render_template("settings.html", settings=settings, saved=saved)

@app.route("/settings", methods=["POST"])
def settings_save():
    for key, value in request.form.items():
        if key.startswith('grace_'):
            sensor_id = key[len('grace_'):]
            try:
                grace = max(0, min(1440, int(value)))
                save_sensor_settings(sensor_id, grace)
            except ValueError:
                logging.warning(f"Invalid grace period value for {sensor_id}: {value}")
    return redirect("/settings?saved=1")

@app.route("/favicon.ico")
def favicon():
    return "", 204

@app.route("/submit", methods=["POST"])
def submit():
    if request.headers.get("X-API-Key") != SUBMIT_API_KEY:
        return jsonify({"error": "Unauthorized"}), 401

    content = request.get_json(silent=True)
    if not content:
        return jsonify({"error": "Invalid JSON"}), 400

    sensor_id = content.get("sensor_id")
    temperature = content.get("temperature")
    timestamp = content.get("timestamp")
    status = content.get("status")

    if not all([sensor_id, timestamp, status]):
        return jsonify({"error": "Missing required fields: sensor_id, timestamp, status"}), 400
    if not isinstance(temperature, (int, float)):
        return jsonify({"error": "temperature must be a number"}), 400
    if status not in (NOMINAL_STATUS, WARNING_STATUS, CRITICAL_STATUS):
        return jsonify({"error": f"Invalid status '{status}'"}), 400

    if request.headers.getlist("X-Forwarded-For"):
        client_ip = request.headers.getlist("X-Forwarded-For")[0]
    else:
        client_ip = request.remote_addr

    logging.info(f"Received remote sensor data for {sensor_id} from client {client_ip}: Temp={temperature} °C, Time={timestamp}, Status={status}")
    store_temperature(sensor_id, temperature, timestamp, status)
    check_and_alert(sensor_id, temperature, status)
    return jsonify({"status": "ok"})

# Factory function to be used by Gunicorn
def create_app():
    logging.info("create_app() called")
    init_db()

    if not scheduler.running:
        scheduler.start()
        logging.info("Scheduler started from create_app()")

    for job in scheduler.get_jobs():
        logging.info(f"Scheduled job: {job}")

    return app

# Still allow direct execution for dev/testing
if __name__ == '__main__':
    init_db()

    for job in scheduler.get_jobs():
        logging.info(f"Scheduled job: {job}")

#    app.run(host='0.0.0.0', port=5000, use_reloader=False)
    create_app().run(host='0.0.0.0', port=5000, use_reloader=False)