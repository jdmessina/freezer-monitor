from dotenv import load_dotenv
import os

DB_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data.db")

# Load .env
load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), "secrets.env"))

# List of required environment variables
REQUIRED_VARS = [
    "EMAIL",
    "PHONE_NUMBER",
    "EMAIL_SERVER",
    "EMAIL_PORT",
    "EMAIL_USERNAME",
    "EMAIL_PASSWORD",
    "CALLMEBOT_API",
    "SUBMIT_API_KEY"
]

# Helper to load and validate all required env vars
def load_required_env_vars(var_names):
    missing = []
    env_vars = {}
    for var in var_names:
        value = os.getenv(var)
        if value is None:
            missing.append(var)
        else:
            env_vars[var] = value
    if missing:
        raise RuntimeError(
            f"The following required environment variables are missing: {', '.join(missing)}"
        )
    return env_vars

# Load all variables safely
config = load_required_env_vars(REQUIRED_VARS)

# Global
DEBUG=False
NOMINAL_STATUS = "OK"
WARNING_STATUS = "WARNING"
CRITICAL_STATUS = "CRITICAL"
TEMP_BOTTOM_THRESHOLD = -30  # Temperature in °C
TEMP_LOW_THRESHOLD = -20  # Temperature in °C
TEMP_OK_THRESHOLD = -18  # Temperature in °C
TEMP_WARNING_THRESHOLD = -12  # Temperature in °C
TEMP_CRITICAL_THRESHOLD = -6.6  # Temperature in °C
TEMP_TOP_THRESHOLD = 30  # Temperature in °C
SENSOR_ONLINE = "ONLINE"
SENSOR_OFFLINE = "OFFLINE"
SENSOR_REPORTING_TIMEOUT = 300  # 5 minutes
WARNING_GRACE_PERIOD = 900   # seconds — default warning grace period (15 min)
CRITICAL_GRACE_PERIOD = 300  # seconds — default critical grace period (5 min)
WARNING_ALERT_DIFFERENTIAL = 1.0   # °C — default per-degree alert step in WARNING
CRITICAL_ALERT_DIFFERENTIAL = 0.5  # °C — default per-degree alert step in CRITICAL
SUBMIT_API_KEY = config["SUBMIT_API_KEY"]
EMAIL = config["EMAIL"]
PHONE_NUMBER = config["PHONE_NUMBER"]
EMAIL_SERVER = config["EMAIL_SERVER"]
EMAIL_PORT = int(config["EMAIL_PORT"])  # cast to int if needed
EMAIL_USERNAME = config["EMAIL_USERNAME"]
EMAIL_PASSWORD = config["EMAIL_PASSWORD"]
CALLMEBOT_API = config["CALLMEBOT_API"]