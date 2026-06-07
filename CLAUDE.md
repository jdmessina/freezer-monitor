# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Distributed temperature monitoring system for Raspberry Pi. DS18B20 sensors on freezer-side Pis (clients) POST readings to a central Flask server. The server stores readings in SQLite, serves a Chart.js dashboard, and fires email/WhatsApp alerts when temperatures go out of range.

## Running the Server

**Development:**
```bash
cd server
python probe_server.py
# Dashboard at http://localhost:5000
```

**Production (Gunicorn):**
```bash
/home/pi/freezer_monitor/venv/bin/gunicorn -w 1 -b 0.0.0.0:5000 "probe_server:create_app()"
```

**Client (on each sensor Pi):**
```bash
cd client
python probe_client.py
```

**Systemd (production):**
```bash
sudo systemctl start probe_server.service
sudo journalctl -u probe_server.service -f
```

## Testing Endpoints Manually

```bash
# Submit a reading
curl -X POST http://localhost:5000/submit \
  -H "Content-Type: application/json" \
  -d '{"sensor_id":"large_freezer","temperature":-15.5,"timestamp":"2026-03-26 12:00:00","status":"OK"}'

# Check the DB
sqlite3 server/data.db "SELECT * FROM temperatures ORDER BY timestamp DESC LIMIT 10;"
```

## Architecture

**Data flow:** `probe_client.py` reads DS18B20 sensor every 60s → POST `/submit` → `db_setup.py` inserts into SQLite → `email_alerts.py` checks thresholds → APScheduler runs `poll_probes()` every minute and `db_cleanup.py` daily.

**Key modules:**
- `server/probe_server.py` — Flask app factory, routes (`GET /`, `POST /submit`), scheduler setup
- `server/global_config.py` — shared config loaded from `secrets.env` via `python-dotenv`; temperature thresholds, alert timeouts, status constants (`NOMINAL_STATUS`, `WARNING_STATUS`, `CRITICAL_STATUS`)
- `server/db_setup.py` — SQLite init and all DB operations; tables: `temperatures`, `alerts`
- `server/email_alerts.py` — alert state machine; uses Redis distributed lock to prevent alert spam
- `server/utils.py` — `compute_summary_status()`, `filter_data()` (downsampling for chart), `poll_probes()` (offline detection)
- `client/probe_client.py` — sensor daemon; reads `/sys/bus/w1/devices/<id>/w1_slave`, determines status, POSTs to server
- `server/templates/index.html` — Jinja2 template with Chart.js 4.x + Luxon for time-series visualization

**Config hierarchy:** `secrets.env` → `global_config.py` → `server_config.py` / `client_config.py`

## Known Issues (from `Freezer Monitor - Code Review.md`)

This file documents 32 issues. The highest-priority ones to be aware of:

- **`filter_data()` crashes on empty dataset** — `max()` of empty sequence raises `ValueError`
- **`get_sensor_state()` crashes if sensor never reported** — `None` comparison raises `TypeError`
- **`reversed(records)` sent to Jinja2** — iterator consumed on first use; second use in template yields nothing
- **`/submit` has no authentication** — any host can POST fake readings
- **`secrets.env` contains plaintext credentials** — email password stored in repo

## Deployment Notes

- Target path: `/home/pi/freezer_monitor/`
- Logs: `/home/pi/freezer_monitor/logs/` (rotating, 5 MB max, 5 backups)
- Database: `server/data.db` (SQLite, auto-created on first run)
- Client service file: `client/services/probe_client.service`
- 1-Wire sensor requires `dtoverlay=w1-gpio` in `/boot/config.txt` and `w1-gpio`/`w1-therm` kernel modules loaded
