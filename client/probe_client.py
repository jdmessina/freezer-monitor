import requests
import glob
import time
import logging
import concurrent.futures

from logging.handlers import RotatingFileHandler
from apscheduler.schedulers.background import BackgroundScheduler
from datetime import datetime, timedelta
from email_alerts import send_alert
from client_config import SERVER_URL, SENSOR_ID, CLIENT_LOG_FILE
from global_config import DEBUG, TEMP_WARNING_THRESHOLD, TEMP_CRITICAL_THRESHOLD, CRITICAL_STATUS, WARNING_STATUS, NOMINAL_STATUS, SENSOR_REPORTING_TIMEOUT, SUBMIT_API_KEY

# Configure logging
log_file = CLIENT_LOG_FILE

# Rotate when the log file reaches 5MB, keep 5 backups
handler =  RotatingFileHandler(log_file, maxBytes=5*1024*1024, backupCount=5)
formatter = logging.Formatter('%(asctime)s %(levelname)s: %(message)s')
handler.setFormatter(formatter)

logger = logging.getLogger()
logger.setLevel(logging.INFO)
logger.addHandler(handler)

last_alert_time = None

if DEBUG:
    logging.info(f"Freezer Monitor {SENSOR_ID} Client Service Started: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

# Create a scheduler instance
scheduler = BackgroundScheduler()

def _read_sensor_file(path):
    with open(path, 'r') as f:
        return f.readlines()

def _read_with_timeout(path, timeout=5):
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
        future = ex.submit(_read_sensor_file, path)
        try:
            return future.result(timeout=timeout)
        except concurrent.futures.TimeoutError:
            raise TimeoutError(f"Sensor read timed out after {timeout} seconds")

# Function to read the temperature from the DS18B20 probe
def read_temperature_from_sensor():
    base_dir = '/sys/bus/w1/devices/'
    matches = glob.glob(base_dir + '28*')
    if not matches:
        raise FileNotFoundError(f"No DS18B20 sensor found in {base_dir}")
    device_folder = matches[0]
    device_file = device_folder + '/w1_slave'

    lines = _read_with_timeout(device_file)

    # Wait until CRC check passes (retrying up to 5 times)
    attempts = 0
    while lines[0].strip()[-3:] != 'YES' and attempts < 5:
        time.sleep(0.2)
        lines = _read_with_timeout(device_file)
        attempts += 1
    if attempts >= 5:
        logging.error("Failed to read temperature after multiple attempts.")
        raise Exception("Failed to read temperature after multiple attempts.")

    # Parse the temperature value
    equals_pos = lines[1].find('t=')
    if equals_pos != -1:
        temp_string = lines[1][equals_pos+2:]
        temp_c = float(temp_string) / 1000.0
        logging.info(f"Temperature probe reports {temp_c}°C")
        return temp_c
    else:
        logging.error("Could not read the temperature from the DS18B20 probe.")
        raise Exception("Error reading temperature from sensor.")

# Push temperature function
def push_temperature():
    global last_alert_time

    push_time = datetime.now()
    should_alert = (last_alert_time is None or push_time - last_alert_time > timedelta(seconds=SENSOR_REPORTING_TIMEOUT))

    if DEBUG:
        logging.info("Running scheduled temperature probe read")

    try:
        sensor_id = SENSOR_ID
        temperature = read_temperature_from_sensor()
        timestamp = push_time.strftime("%Y-%m-%d %H:%M:%S")

        if temperature > TEMP_CRITICAL_THRESHOLD:
            status = CRITICAL_STATUS
        elif temperature > TEMP_WARNING_THRESHOLD:
            status = WARNING_STATUS
        else:
            status = NOMINAL_STATUS

        response = requests.post(f"{SERVER_URL}/submit",
            json={
                "sensor_id": sensor_id,
                "temperature": temperature,
                "timestamp": timestamp,
                "status": status
            },
            headers={"X-API-Key": SUBMIT_API_KEY}
        )

        if response.status_code == 200:
            if not should_alert:
                subject = "INFO: Client sensor reconnected to server"
                body = f"CONNECTION RESTORED: Successfully pushed data for {sensor_id} to the server at {SERVER_URL}."
                send_alert(sensor_id, subject, body)
                logging.info(f"Sending Alert: Connection to server at {SERVER_URL} for {sensor_id} restored.")

            last_alert_time = None
            logging.info(f"Successfully pushed data for {sensor_id} to server at {SERVER_URL}: {temperature}°C, Status: {status}")

        else:
            if should_alert:
                subject = "ERROR: Client sensor failed to post to server"
                body = f"FAILED POST: Failed to push data for {sensor_id} to the server at {SERVER_URL}: {response.status_code} {response.text}"
                send_alert(sensor_id, subject, body)
                last_alert_time = push_time
                logging.warning(f"Sending Alert: Failed to push data for {sensor_id} to server at {SERVER_URL}: {response.status_code} {response.text}")
            else:
                logging.warning(f"Suppressed Alert: Repeated error to push data for {sensor_id} to server at {SERVER_URL}: {response.status_code} {response.text}")

    except Exception as e:
        if should_alert:
            subject = "ERROR: Freezer Monitoring Service refused Client connection"
            body = f"FAILED POST: Failed to push data for {sensor_id} to the server at {SERVER_URL}: {e}"
            send_alert(sensor_id, subject, body)
            last_alert_time = push_time
            logging.error(f"Sending Alert: Failed to connect to server: {e}")
        else:
            logging.warning(f"Suppressed Alert: Repeated error for {sensor_id} to connect to server at {SERVER_URL}: {e}")

# Schedule the push_temperature function to run every 60 seconds
scheduler.add_job(push_temperature, 'interval', seconds=60)

if __name__ == "__main__":
    scheduler.start()
    logging.info("Scheduler started, pushing temperature every 60 seconds.")
    try:
        # This will block the main thread, keeping the scheduler running
        while True:
            time.sleep(60)  # Keep the script running to allow scheduled jobs
    except (KeyboardInterrupt, SystemExit):
        logging.info("Freezer Monitor Client Service Stopped")
        scheduler.shutdown()