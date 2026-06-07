# Freezer Monitor

A distributed temperature monitoring system for Raspberry Pi. DS18B20 temperature sensors on one or more client Pis POST readings to a central Flask server, which stores them in SQLite, serves a live web dashboard, and fires email and WhatsApp alerts when temperatures go out of range.

## Features

- Real-time temperature chart with color-coded zones (critical, warning, OK)
- Per-sensor status cards showing current temperature, online/offline state, and recent readings
- Email and WhatsApp (via CallMeBot) alerts with configurable grace periods
- Mobile-responsive dashboard with portrait and landscape support
- Time range selector — view the last hour, 6 hours, 24 hours, or a custom offset window
- Celsius / Fahrenheit toggle
- Multi-sensor support — add as many client Pis as you need
- Systemd services for automatic startup and restart on both server and client Pis
- Interactive install/reconfigure/uninstall script (`setup.sh`)

## Hardware

- Raspberry Pi (any model) for the server
- Raspberry Pi (any model) for each sensor location
- DS18B20 1-Wire temperature sensor(s) wired to GPIO4 on each client Pi
- Network connectivity between all Pis

## Architecture

```
[Client Pi]                        [Server Pi]
probe_client.py                    probe_server.py (Gunicorn)
  reads DS18B20 sensor  ──POST──>    stores in SQLite
  every 60 seconds                   checks alert thresholds
                                     serves web dashboard
                                     APScheduler: poll + cleanup
```

**Key modules:**

| File | Purpose |
|---|---|
| `server/probe_server.py` | Flask app factory, routes (`GET /`, `POST /submit`, `GET /data`, `GET/POST /settings`) |
| `server/global_config.py` | Shared config loaded from `secrets.env`; temperature thresholds, alert timeouts, status constants |
| `server/server_config.py` | Server-specific paths, zone colors, chart limits, sensor display names |
| `server/db_setup.py` | SQLite init and all DB operations (`temperatures`, `alerts`, `sensor_settings` tables) |
| `server/email_alerts.py` | Alert state machine; Redis distributed lock prevents duplicate alerts |
| `server/utils.py` | `compute_summary_status()`, `filter_data()` (downsampling for chart), `poll_probes()` (offline detection) |
| `server/db_cleanup.py` | Daily cleanup of old temperature records |
| `client/probe_client.py` | Sensor daemon — reads `/sys/bus/w1/devices/<id>/w1_slave`, determines status, POSTs to server |
| `client/client_config.py` | Client-specific settings: server URL, sensor ID, log path |
| `server/templates/index.html` | Jinja2 template with Chart.js 4.x + Luxon for time-series visualization |
| `server/templates/settings.html` | Per-sensor alert grace period settings UI |

**Config hierarchy:** `secrets.env` → `global_config.py` → `server_config.py` / `client_config.py`

## Quick Start

### 1. Clone the repository

```bash
git clone https://github.com/your-username/freezer-monitor.git
cd freezer-monitor
```

### 2. Run the install script

The install script handles everything: system packages, Python virtualenv, configuration, 1-Wire setup, and systemd services. Run it on each Pi with `sudo`:

```bash
sudo bash setup.sh
```

Select **Server** on the Pi that will host the dashboard, **Client** on each sensor Pi, or **Both** if running server and a sensor on the same Pi.

The script will prompt for:
- **Server:** SMTP credentials, WhatsApp/CallMeBot API key (optional), sensor display names
- **Client:** Server URL, sensor ID, SUBMIT_API_KEY (shown at end of server setup)

### 3. Access the dashboard

```
http://<server-ip>:5000
```

## Manual Setup (without the install script)

### Server

```bash
cd server
cp secrets.env.example secrets.env
# Edit secrets.env with your credentials
pip install flask apscheduler gunicorn gevent redis python-dotenv requests
python probe_server.py
```

### Client

```bash
cd client
# Edit client_config.py: set SERVER_URL and SENSOR_ID
pip install requests apscheduler python-dotenv
python probe_client.py
```

### Production (Gunicorn + systemd)

Copy the appropriate service file from `server/services/` or `client/services/` to `/etc/systemd/system/`, update the paths to match your install directory and username, then:

```bash
sudo systemctl daemon-reload
sudo systemctl enable <service-name>
sudo systemctl start <service-name>
```

The install script generates and installs these automatically with the correct paths.

## Configuration

### Temperature thresholds

Edit `server/global_config.py`:

```python
TEMP_LOW_THRESHOLD      = -20   # °C — below this is critical low
TEMP_OK_THRESHOLD       = -18   # °C — lower bound of normal range
TEMP_WARNING_THRESHOLD  = -12   # °C — upper bound of normal range
TEMP_CRITICAL_THRESHOLD = -6.6  # °C — above this is critical high
```

### Sensor display names

Edit `server/server_config.py`:

```python
SENSOR_DISPLAY_NAMES = {
    "freezer_1": "Large Freezer",
    "freezer_2": "Small Freezer",
}
```

The install script can add/update these interactively.

### Alert grace periods

Visit `http://<server-ip>:5000/settings` to configure per-sensor warning grace periods (how long a sensor must stay in WARNING before an alert fires).

### secrets.env

Copy `server/secrets.env.example` to `server/secrets.env` and fill in your values. This file is excluded from git and must never be committed.

```
EMAIL = your@email.com
PHONE_NUMBER = +15551234567
EMAIL_SERVER = smtp.gmail.com
EMAIL_PORT = 587
EMAIL_USERNAME = your@email.com
EMAIL_PASSWORD = your_smtp_password
CALLMEBOT_API = your_callmebot_api_key
SUBMIT_API_KEY = <generate with: python3 -c "import secrets; print(secrets.token_hex(32))">
```

## 1-Wire Sensor Setup (Raspberry Pi)

Add to `/boot/config.txt` (or `/boot/firmware/config.txt` on newer Pi OS):

```
dtoverlay=w1-gpio
```

Load the kernel modules:

```bash
sudo modprobe w1-gpio
sudo modprobe w1-therm
```

Verify the sensor is detected:

```bash
ls /sys/bus/w1/devices/28-*
```

The install script handles all of this automatically, including prompting to reboot if needed.

## Testing Endpoints Manually

```bash
# Submit a test reading
curl -X POST http://localhost:5000/submit \
  -H "Content-Type: application/json" \
  -H "X-API-Key: <your-SUBMIT_API_KEY>" \
  -d '{"sensor_id":"freezer_1","temperature":-15.5,"timestamp":"2026-01-01 12:00:00","status":"OK"}'

# Check the database
sqlite3 server/data.db "SELECT * FROM temperatures ORDER BY timestamp DESC LIMIT 10;"
```

## Logs

- Server: `/home/pi/freezer_monitor/logs/server.log` (rotating, 5 MB max, 5 backups)
- Client: `/home/pi/freezer_monitor/logs/client_<sensor_id>.log`

Or via systemd:

```bash
sudo journalctl -u freezer-monitor-server -f
sudo journalctl -u freezer-monitor-client-freezer_1 -f
```

## Managing Services

```bash
sudo bash setup.sh   # choose option 2 (Start/Stop/Restart) or 3 (Status)
```

Or directly:

```bash
sudo systemctl restart freezer-monitor-server
sudo systemctl status freezer-monitor-client-freezer_1
```
