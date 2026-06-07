import smtplib
import logging
import requests
import redis
import threading

from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime, timedelta
from redis.exceptions import LockError
from db_setup import get_recent_temps, load_last_alerts, save_last_alert, get_sensor_settings
from global_config import DEBUG, WARNING_TIMEOUT, TEMP_WARNING_THRESHOLD, CRITICAL_TIMEOUT, TEMP_CRITICAL_THRESHOLD, NOMINAL_STATUS, WARNING_STATUS, CRITICAL_STATUS, SENSOR_ONLINE, SENSOR_OFFLINE, SENSOR_REPORTING_TIMEOUT, EMAIL, PHONE_NUMBER, EMAIL_SERVER, EMAIL_PORT, EMAIL_USERNAME, EMAIL_PASSWORD, CALLMEBOT_API

def send_whatsapp_alert(phone_number, apikey, message):
    url = 'https://api.callmebot.com/whatsapp.php'
    params = {
        'phone': phone_number,
        'apikey': apikey,
        'text': message
    }

    try:
        response = requests.get(url, params=params)
        if response.status_code == 200:
            logging.info("WhatsApp message sent successfully")
        else:
            logging.warning(f"Failed to send message: {response.text}")
    except Exception as e:
        logging.error(f"Error sending WhatsApp message: {e}")

def send_alert(sensor_id, subject, body):
# - disable    send_whatsapp_alert(PHONE_NUMBER, CALLMEBOT_API, body)

    msg = MIMEMultipart()
    msg['From'] = EMAIL
    msg['Subject'] = subject
    msg.attach(MIMEText(body, 'plain'))

    recipients = [EMAIL]

    try:
        server = smtplib.SMTP(EMAIL_SERVER, EMAIL_PORT)
        server.starttls()
        server.login(EMAIL_USERNAME, EMAIL_PASSWORD)
        for recipient in recipients:
            msg['To'] = recipient
            server.sendmail(EMAIL, recipient, msg.as_string())
        server.quit()

        logging.info(f"Email sent for sensor {sensor_id} with subject '{subject}' to {EMAIL}")

    except Exception as e:
        logging.error(f"Failed to send alert for {sensor_id}: {e}", exc_info=True)

def sensor_last_seen(sensor_id):
    last_reports = get_recent_temps(minutes=60, offset=0)
    last_seen = None

    if sensor_id in last_reports and last_reports[sensor_id]:
        last_entry = last_reports[sensor_id][0]
        last_timestamp = last_entry["timestamp"]
        last_seen = datetime.strptime(last_timestamp, "%Y-%m-%d %H:%M:%S")
    else:
        last_seen = None

    return last_seen

def get_sensor_state(sensor_id, last_seen=None):
    now = datetime.now()
    if last_seen is None:
        last_seen = sensor_last_seen(sensor_id)
    alert_state = SENSOR_ONLINE

    if last_seen is None:
        logging.error(f"Sensor {sensor_id} has no last_seen timestamp!")
        return SENSOR_OFFLINE

    # Determine if the sensor is online
    if (now - last_seen).total_seconds() < SENSOR_REPORTING_TIMEOUT:
        logging.info(f"Sensor {sensor_id} is ONLINE: Last seen: {last_seen}")
        alert_state = SENSOR_ONLINE
    else:
        logging.warning(f"Sensor {sensor_id} is OFFLINE: Exceeded {SENSOR_REPORTING_TIMEOUT} second threshold.  Last seen: {last_seen}")
        alert_state = SENSOR_OFFLINE

    return alert_state

def check_and_alert(sensor_id, temperature, status):
    r = redis.Redis()
    lock = r.lock(f"alert_lock:{sensor_id}", timeout=30)

    if lock.acquire(blocking=False):
        try:
            logging.info(f"Lock acquired for Sensor {sensor_id}.")
            update_alert_status(sensor_id, temperature, status)

        except Exception as e:
            logging.error(f"Failed to update alert status for {sensor_id}: {e}", exc_info=True)

        finally:
            lock.release()

    else:
        logging.info(f"Lock already held for Sensor {sensor_id}.")

def update_alert_status(sensor_id, temperature, status):
    now = datetime.now()
    last_alerts = load_last_alerts()
    sensor_state = last_alerts.get(sensor_id, {})
    last_time = sensor_state.get('time')
    last_status = sensor_state.get('status')
    last_state = sensor_state.get('state')
    state_entry_time = sensor_state.get('state_entry_time')

    alert_type = NOMINAL_STATUS
    trigger_alert = False
    save_state_now = False  # Save state change without sending an alert
    alert_sent = False
    alert_delay = 0
    new_state_entry_time = state_entry_time  # Updated when entering a new alert state

    # Per-sensor warning grace period (converted from minutes to seconds)
    sensor_settings = get_sensor_settings(sensor_id)
    warning_grace_seconds = sensor_settings.get('warning_grace_period', 0) * 60

    # Determine online/offline state
    last_seen = sensor_last_seen(sensor_id)
    alert_state = get_sensor_state(sensor_id, last_seen)

    # --- Online/offline handling ---
    if last_state == SENSOR_OFFLINE:
        if alert_state == SENSOR_ONLINE:
            subject = 'INFO: Sensor is Online'
            body = f"INFO: Sensor {sensor_id} is back ONLINE: Last seen {last_seen}."
            trigger_alert = True
            logging.info(f"Sending Alert: Sensor {sensor_id} is back {alert_state}.")
        else:
            if last_time is None or (now - last_time).total_seconds() > SENSOR_REPORTING_TIMEOUT:
                subject = 'WARNING: Sensor remains Offline'
                body = f"WARNING: Sensor {sensor_id} remains OFFLINE: It has not been seen since {last_seen}, exceeding the {SENSOR_REPORTING_TIMEOUT} second timeout."
                trigger_alert = True
                logging.warning(f"Sending Alert: Sensor {sensor_id} remains {alert_state}.")
            else:
                time_remaining = SENSOR_REPORTING_TIMEOUT - (now - last_time).total_seconds()
                next_alert = now + timedelta(seconds=time_remaining)
                logging.warning(f"Suppressing Alert: Sensor {sensor_id} remains {alert_state} - resending in {time_remaining:.0f}s at {next_alert}.")
    else:
        if alert_state == SENSOR_OFFLINE:
            subject = 'WARNING: Sensor is Offline'
            body = f"WARNING: Sensor {sensor_id} is OFFLINE: It has not been seen since {last_seen}, exceeding the {SENSOR_REPORTING_TIMEOUT} second timeout."
            trigger_alert = True
            logging.warning(f"Sending Alert: Sensor {sensor_id} went {alert_state}.")
        else:
            time_remaining = SENSOR_REPORTING_TIMEOUT - (now - last_seen).total_seconds() if last_seen else 0
            logging.info(f"Sensor {sensor_id} last seen at {last_seen}: {time_remaining:.0f} seconds until alert.")

    # Send online/offline alert
    if trigger_alert:
        logging.info(f"Sending {alert_state} alert for sensor {sensor_id}.")
        threading.Thread(target=send_alert, args=(sensor_id, subject, body), daemon=True).start()
        save_last_alert(sensor_id, alert_type, alert_state, now, new_state_entry_time)
        alert_sent = True
        trigger_alert = False

    # --- Temperature alert logic ---
    if temperature is None:
        logging.info(f"Sensor {sensor_id} has no temperature data; skipping temperature alert evaluation.")

    elif temperature < TEMP_WARNING_THRESHOLD:
        if last_status is not None and last_status != NOMINAL_STATUS:
            if last_time is not None:
                # An alert was previously sent — send "cleared" alert
                alert_type = NOMINAL_STATUS
                new_state_entry_time = None
                subject = 'INFO: Freezer Temperature Returned to Normal'
                body = f"Temperature Alert Cleared! Sensor {sensor_id} reports {temperature}°C and {status} status."
                trigger_alert = True
                logging.info(f"Sending Alert: Temperature alert cleared for Sensor {sensor_id}.")
            else:
                # Returned to nominal within grace period — clear state silently, no alert
                alert_type = NOMINAL_STATUS
                new_state_entry_time = None
                save_state_now = True
                logging.info(f"Sensor {sensor_id} returned to nominal within grace period — no alert sent.")
        elif last_status is None:
            alert_type = NOMINAL_STATUS
            subject = 'INFO: Freezer Monitoring Service Restarted (Sensor Normal)'
            body = f"INFO: Freezer Monitoring Service Restarted. Sensor {sensor_id} reports {temperature}°C and {status} status."
            trigger_alert = True
            logging.info(f"System Alert: Freezer Monitoring service restarted.")

    elif temperature > TEMP_CRITICAL_THRESHOLD:
        # Critical — always alert immediately, no grace period
        logging.warning(f"CRITICAL ALERT on sensor {sensor_id} with temp {temperature}°C > {TEMP_CRITICAL_THRESHOLD}°C")
        alert_type = CRITICAL_STATUS
        alert_delay = CRITICAL_TIMEOUT
        subject = 'CRITICAL: Freezer Temperature Alert'
        body = f"Critical Temperature Alert! Sensor {sensor_id} reports {temperature}°C and {status} status."
        trigger_alert = True
        if last_status != CRITICAL_STATUS:
            new_state_entry_time = now  # Reset entry time on escalation

    elif temperature > TEMP_WARNING_THRESHOLD:
        logging.warning(f"WARNING on sensor {sensor_id} with temp {temperature}°C > {TEMP_WARNING_THRESHOLD}°C")
        alert_type = WARNING_STATUS
        alert_delay = WARNING_TIMEOUT

        if last_status != WARNING_STATUS:
            # Just entered WARNING — record entry time, suppress alert during grace period
            new_state_entry_time = now
            save_state_now = True
            logging.info(f"Sensor {sensor_id} entered WARNING state. Grace period: {warning_grace_seconds:.0f}s.")
        else:
            # Already in WARNING — check grace period
            if warning_grace_seconds > 0 and state_entry_time and \
               (now - state_entry_time).total_seconds() < warning_grace_seconds:
                remaining = warning_grace_seconds - (now - state_entry_time).total_seconds()
                logging.info(f"Suppressing WARNING for {sensor_id}: {remaining:.0f}s remaining in {warning_grace_seconds/60:.0f}min grace period.")
            else:
                # Grace period expired or disabled — allow alert
                subject = 'WARNING: Freezer Temperature Alert'
                body = f"Warning Temperature Alert! Sensor {sensor_id} reports {temperature}°C and {status} status."
                trigger_alert = True
                logging.warning(f"Sending WARNING alert for Sensor {sensor_id}: {temperature}°C.")

    else:
        alert_type = NOMINAL_STATUS

    # Spam check — suppress repeat alerts within the cooldown window
    if trigger_alert and alert_type != NOMINAL_STATUS:
        if (last_status == alert_type and last_time and (now - last_time).total_seconds() < alert_delay) or \
           (last_status == CRITICAL_STATUS and alert_type == WARNING_STATUS and last_time and
            (now - last_time).total_seconds() < CRITICAL_TIMEOUT):
            next_alert_time = last_time + timedelta(seconds=alert_delay if last_status == alert_type else CRITICAL_TIMEOUT)
            logging.info(f"Pending Alert: Sensor {sensor_id} last alerted at {last_time}. Next {alert_type} alert at {next_alert_time}")
            trigger_alert = False

    # Send temperature alert and persist state
    if trigger_alert:
        logging.info(f"Sending {alert_type} alert for sensor {sensor_id}.")
        threading.Thread(target=send_alert, args=(sensor_id, subject, body), daemon=True).start()
        save_last_alert(sensor_id, alert_type, alert_state, now, new_state_entry_time)
        alert_sent = True
    elif save_state_now:
        # Persist state change (grace period entry or silent clear) without sending an alert
        save_last_alert(sensor_id, alert_type, alert_state, last_time, new_state_entry_time)

    if not alert_sent and not save_state_now:
        logging.info(f"No alerts sent for {sensor_id}.")