import math
import smtplib
import logging
import requests
import redis
import threading

from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime, timedelta
from redis.exceptions import LockError
from db_setup import get_recent_temps, load_last_alerts, save_last_alert, get_sensor_settings, set_acknowledged, mark_alert_sent
from global_config import (DEBUG, WARNING_GRACE_PERIOD, CRITICAL_GRACE_PERIOD,
                           WARNING_ALERT_DIFFERENTIAL, CRITICAL_ALERT_DIFFERENTIAL,
                           TEMP_WARNING_THRESHOLD, TEMP_CRITICAL_THRESHOLD,
                           NOMINAL_STATUS, WARNING_STATUS, CRITICAL_STATUS,
                           SENSOR_ONLINE, SENSOR_OFFLINE, SENSOR_REPORTING_TIMEOUT,
                           EMAIL, PHONE_NUMBER, EMAIL_SERVER, EMAIL_PORT,
                           EMAIL_USERNAME, EMAIL_PASSWORD, CALLMEBOT_API)

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

    if (now - last_seen).total_seconds() < SENSOR_REPORTING_TIMEOUT:
        logging.info(f"Sensor {sensor_id} is ONLINE: Last seen: {last_seen}")
        alert_state = SENSOR_ONLINE
    else:
        logging.warning(f"Sensor {sensor_id} is OFFLINE: Exceeded {SENSOR_REPORTING_TIMEOUT} second threshold.  Last seen: {last_seen}")
        alert_state = SENSOR_OFFLINE

    return alert_state

def compute_next_d1(temperature, state_threshold, differential):
    """Next alert threshold above temperature, in steps of differential from state_threshold."""
    steps = math.ceil((temperature - state_threshold) / differential)
    return state_threshold + steps * differential

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
    next_alert_temp = sensor_state.get('next_alert_temp')
    acknowledged = sensor_state.get('acknowledged', 0)

    settings = get_sensor_settings(sensor_id)
    warning_grace_secs = settings['warning_grace_period'] * 60
    critical_grace_secs = settings['critical_grace_period'] * 60
    warning_diff = settings['warning_differential']
    critical_diff = settings['critical_differential']

    # --- Online/offline handling ---
    last_seen = sensor_last_seen(sensor_id)
    alert_state = get_sensor_state(sensor_id, last_seen)

    if last_state == SENSOR_OFFLINE and alert_state == SENSOR_ONLINE:
        body = f"INFO: Sensor {sensor_id} is back ONLINE: Last seen {last_seen}."
        logging.info(f"Sending Alert: Sensor {sensor_id} is back ONLINE.")
        threading.Thread(target=send_alert, args=(sensor_id, 'INFO: Sensor is Online', body), daemon=True).start()
        save_last_alert(sensor_id, NOMINAL_STATUS, SENSOR_ONLINE, now, None, next_alert_temp)
        last_status = NOMINAL_STATUS  # reset so subsequent temperature check fires a transition if needed
        last_time = now

    elif last_state == SENSOR_OFFLINE and alert_state == SENSOR_OFFLINE:
        if last_time is None or (now - last_time).total_seconds() > SENSOR_REPORTING_TIMEOUT:
            body = f"WARNING: Sensor {sensor_id} remains OFFLINE: It has not been seen since {last_seen}, exceeding the {SENSOR_REPORTING_TIMEOUT} second timeout."
            logging.warning(f"Sending Alert: Sensor {sensor_id} remains OFFLINE.")
            threading.Thread(target=send_alert, args=(sensor_id, 'WARNING: Sensor remains Offline', body), daemon=True).start()
            save_last_alert(sensor_id, NOMINAL_STATUS, SENSOR_OFFLINE, now, None, next_alert_temp)
        else:
            time_remaining = SENSOR_REPORTING_TIMEOUT - (now - last_time).total_seconds()
            logging.warning(f"Suppressing Alert: Sensor {sensor_id} remains OFFLINE - resending in {time_remaining:.0f}s.")
        return

    elif last_state != SENSOR_OFFLINE and alert_state == SENSOR_OFFLINE:
        body = f"WARNING: Sensor {sensor_id} is OFFLINE: It has not been seen since {last_seen}, exceeding the {SENSOR_REPORTING_TIMEOUT} second timeout."
        logging.warning(f"Sending Alert: Sensor {sensor_id} went OFFLINE.")
        threading.Thread(target=send_alert, args=(sensor_id, 'WARNING: Sensor is Offline', body), daemon=True).start()
        save_last_alert(sensor_id, NOMINAL_STATUS, SENSOR_OFFLINE, now, None, next_alert_temp)
        return

    else:
        time_remaining = SENSOR_REPORTING_TIMEOUT - (now - last_seen).total_seconds() if last_seen else 0
        logging.info(f"Sensor {sensor_id} last seen at {last_seen}: {time_remaining:.0f} seconds until offline alert.")

    # --- Temperature alert logic ---
    if temperature is None:
        logging.info(f"Sensor {sensor_id} has no temperature data; skipping temperature alert evaluation.")
        return

    if temperature > TEMP_CRITICAL_THRESHOLD:
        new_status = CRITICAL_STATUS
    elif temperature > TEMP_WARNING_THRESHOLD:
        new_status = WARNING_STATUS
    else:
        new_status = NOMINAL_STATUS

    # System restart — no prior state recorded
    if last_status is None:
        subject = 'INFO: Freezer Monitoring Service Restarted'
        body = f"INFO: Freezer Monitoring Service Restarted. Sensor {sensor_id} reports {temperature}°C ({status})."
        if new_status == NOMINAL_STATUS:
            new_d1 = TEMP_WARNING_THRESHOLD
            new_last_time = None
        elif new_status == WARNING_STATUS:
            new_d1 = compute_next_d1(temperature, TEMP_WARNING_THRESHOLD, warning_diff)
            new_last_time = now
        else:
            new_d1 = compute_next_d1(temperature, TEMP_CRITICAL_THRESHOLD, critical_diff)
            new_last_time = now
        logging.info(f"System restart alert for {sensor_id}: status={new_status}, D1={new_d1:.1f}°C")
        set_acknowledged(sensor_id, 0)
        mark_alert_sent(sensor_id)
        threading.Thread(target=send_alert, args=(sensor_id, subject, body), daemon=True).start()
        save_last_alert(sensor_id, new_status, alert_state, new_last_time, None, new_d1)
        return

    # State transition — fires immediately regardless of grace period
    if new_status != last_status:
        set_acknowledged(sensor_id, 0)
        if new_status == NOMINAL_STATUS:
            subject = 'INFO: Freezer Temperature Returned to Normal'
            body = f"Temperature Alert Cleared! Sensor {sensor_id} reports {temperature}°C ({status})."
            logging.info(f"Sending transition alert for {sensor_id}: {last_status} → NOMINAL")
            mark_alert_sent(sensor_id)
            threading.Thread(target=send_alert, args=(sensor_id, subject, body), daemon=True).start()
            # S1=0 (last_time=None) so next threshold crossing fires immediately
            save_last_alert(sensor_id, NOMINAL_STATUS, alert_state, None, None, TEMP_WARNING_THRESHOLD)

        elif new_status == WARNING_STATUS:
            subject = 'WARNING: Freezer Temperature Alert'
            body = f"Warning Temperature Alert! Sensor {sensor_id} reports {temperature}°C ({status})."
            new_d1 = compute_next_d1(temperature, TEMP_WARNING_THRESHOLD, warning_diff)
            logging.warning(f"Sending transition alert for {sensor_id}: {last_status} → WARNING, D1={new_d1:.1f}°C")
            mark_alert_sent(sensor_id)
            threading.Thread(target=send_alert, args=(sensor_id, subject, body), daemon=True).start()
            save_last_alert(sensor_id, WARNING_STATUS, alert_state, now, None, new_d1)

        else:  # CRITICAL
            subject = 'CRITICAL: Freezer Temperature Alert'
            body = f"Critical Temperature Alert! Sensor {sensor_id} reports {temperature}°C ({status})."
            new_d1 = compute_next_d1(temperature, TEMP_CRITICAL_THRESHOLD, critical_diff)
            logging.warning(f"Sending transition alert for {sensor_id}: {last_status} → CRITICAL, D1={new_d1:.1f}°C")
            mark_alert_sent(sensor_id)
            threading.Thread(target=send_alert, args=(sensor_id, subject, body), daemon=True).start()
            save_last_alert(sensor_id, CRITICAL_STATUS, alert_state, now, None, new_d1)
        return

    # Same state — no transition
    if new_status == NOMINAL_STATUS:
        logging.info(f"Sensor {sensor_id}: {temperature}°C — nominal, no alert needed.")
        return

    # User acknowledged this alert state — suppress until sensor returns to nominal
    if acknowledged:
        logging.info(f"Sensor {sensor_id}: {new_status} alert acknowledged; suppressing until next state transition.")
        return

    # In WARNING or CRITICAL: evaluate S1 (grace period cooldown) and D1 (differential threshold)
    grace_secs = warning_grace_secs if new_status == WARNING_STATUS else critical_grace_secs
    state_threshold = TEMP_WARNING_THRESHOLD if new_status == WARNING_STATUS else TEMP_CRITICAL_THRESHOLD
    differential = warning_diff if new_status == WARNING_STATUS else critical_diff

    # D1 not yet set — initialise without alerting (e.g. after DB migration or first reading)
    if next_alert_temp is None:
        next_alert_temp = compute_next_d1(temperature, state_threshold, differential)
        save_last_alert(sensor_id, new_status, alert_state, last_time, None, next_alert_temp)
        logging.info(f"Initialised D1={next_alert_temp:.1f}°C for {sensor_id} ({new_status}).")
        return

    # Above 0°C: bypass grace and D1, alert every reading
    if temperature > 0:
        subject = f'{new_status}: Freezer Temperature Alert — ABOVE FREEZING'
        body = f"{'Warning' if new_status == WARNING_STATUS else 'Critical'} Temperature Alert! Sensor {sensor_id} reports {temperature:.3f}°C ({status}) — temperature is ABOVE FREEZING."
        logging.warning(f"Sending above-freezing alert for {sensor_id}: {temperature:.3f}°C — bypassing grace/D1")
        mark_alert_sent(sensor_id)
        threading.Thread(target=send_alert, args=(sensor_id, subject, body), daemon=True).start()
        save_last_alert(sensor_id, new_status, alert_state, now, None, next_alert_temp)
        return

    grace_expired = (last_time is None or (now - last_time).total_seconds() >= grace_secs)
    temp_exceeded = temperature > next_alert_temp

    if grace_expired and temp_exceeded:
        subject = f'{new_status}: Freezer Temperature Alert'
        body = f"{'Warning' if new_status == WARNING_STATUS else 'Critical'} Temperature Alert! Sensor {sensor_id} reports {temperature}°C ({status})."
        new_d1 = compute_next_d1(temperature, state_threshold, differential)
        logging.warning(f"Sending {new_status} alert for {sensor_id}: {temperature:.3f}°C > D1={next_alert_temp:.1f}°C — new D1={new_d1:.1f}°C")
        mark_alert_sent(sensor_id)
        threading.Thread(target=send_alert, args=(sensor_id, subject, body), daemon=True).start()
        save_last_alert(sensor_id, new_status, alert_state, now, None, new_d1)
    else:
        if not grace_expired:
            remaining = grace_secs - (now - last_time).total_seconds()
            logging.info(f"Suppressing {new_status} for {sensor_id}: {remaining:.0f}s remaining in grace period. temp={temperature:.3f}°C, D1={next_alert_temp:.1f}°C")
        else:
            logging.info(f"Suppressing {new_status} for {sensor_id}: temp {temperature:.3f}°C has not exceeded D1={next_alert_temp:.1f}°C")
