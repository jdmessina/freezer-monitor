#!/bin/bash
# Freezer Monitor Management Script
# Install, configure, control, and uninstall server/client roles.
# Safe to re-run: existing values are loaded as defaults; API key is preserved.

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SERVER_SERVICE="freezer-monitor-server"

# ── Colour helpers ─────────────────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'
info()    { echo -e "${GREEN}[INFO]${NC}  $*"; }
warn()    { echo -e "${YELLOW}[WARN]${NC}  $*"; }
error()   { echo -e "${RED}[ERROR]${NC} $*" >&2; }
section() { echo; echo -e "${GREEN}━━━ $* ━━━${NC}"; }

# ── Root check ─────────────────────────────────────────────────────────────────
if [[ $EUID -ne 0 ]]; then
    error "This script must be run as root (use sudo)."
    exit 1
fi

REAL_USER="${SUDO_USER:-$(logname 2>/dev/null || echo "$USER")}"

# ── Config-reading helpers ─────────────────────────────────────────────────────
read_cfg_var() {
    local file="$1" key="$2"
    [[ -f "$file" ]] || return 0
    grep -E "^\s*${key}\s*=" "$file" 2>/dev/null \
        | head -1 \
        | sed 's/^[^=]*=\s*//' \
        | tr -d "\"'" \
        | xargs
}

# ── Prompt helpers ─────────────────────────────────────────────────────────────
ask() {
    local var="$1" prompt="$2" default="$3" input
    if [[ -n "$default" ]]; then
        read -rp "$(echo -e "${prompt} [${default}]: ")" input
        input="${input:-$default}"
    else
        while [[ -z "$input" ]]; do
            read -rp "$(echo -e "${prompt}: ")" input
        done
    fi
    printf -v "$var" '%s' "$input"
}

ask_password() {
    # If existing value provided, offer "press Enter to keep current"
    local var="$1" prompt="$2" existing="$3" input
    if [[ -n "$existing" ]]; then
        read -rsp "$(echo -e "${prompt} [press Enter to keep current]: ")" input
        echo
        input="${input:-$existing}"
    else
        while [[ -z "$input" ]]; do
            read -rsp "$(echo -e "${prompt}: ")" input
            echo
        done
    fi
    printf -v "$var" '%s' "$input"
}

ask_yn() {
    local prompt="$1" default="${2:-y}" input yn_hint="[Y/n]"
    [[ "$default" == "n" ]] && yn_hint="[y/N]"
    read -rp "$(echo -e "${prompt} ${yn_hint}: ")" input
    input="${input:-$default}"
    [[ "$input" =~ ^[Yy] ]]
}

# ── Service discovery ──────────────────────────────────────────────────────────
# Prints all installed Freezer Monitor service names, one per line
find_installed_services() {
    [[ -f "/etc/systemd/system/${SERVER_SERVICE}.service" ]] \
        && echo "$SERVER_SERVICE"
    for sf in /etc/systemd/system/freezer-monitor-client-*.service; do
        [[ -f "$sf" ]] && echo "$(basename "$sf" .service)"
    done
}

# Detect the project install directory from an existing service file
find_install_dir() {
    local sf wd
    sf="/etc/systemd/system/${SERVER_SERVICE}.service"
    if [[ -f "$sf" ]]; then
        wd=$(grep "^WorkingDirectory=" "$sf" | cut -d= -f2-)
        echo "${wd%/server}"; return
    fi
    for sf in /etc/systemd/system/freezer-monitor-client-*.service; do
        [[ -f "$sf" ]] || continue
        wd=$(grep "^WorkingDirectory=" "$sf" | cut -d= -f2-)
        echo "${wd%/client}"; return
    done
}

set_dirs() {
    SERVER_DIR="$INSTALL_DIR/server"
    CLIENT_DIR="$INSTALL_DIR/client"
    LOG_DIR="$INSTALL_DIR/logs"
    VENV_DIR="$INSTALL_DIR/venv"
    PYTHON="$VENV_DIR/bin/python"
    PIP="$VENV_DIR/bin/pip"
}

# ── Component selection menu ───────────────────────────────────────────────────
# Sets COMPONENT to "server", "client", or "both"
select_component() {
    local prompt="${1:-Select component}"
    echo
    echo "  1) Server"
    echo "  2) Client"
    echo "  3) Both"
    local choice=""
    while [[ "$choice" != "1" && "$choice" != "2" && "$choice" != "3" ]]; do
        read -rp "$prompt [1/2/3]: " choice
    done
    case "$choice" in
        1) COMPONENT="server" ;;
        2) COMPONENT="client" ;;
        3) COMPONENT="both"   ;;
    esac
}

# ═══════════════════════════════════════════════════════════════════════════════
# STATUS
# ═══════════════════════════════════════════════════════════════════════════════
do_status() {
    section "Service Status"
    mapfile -t services < <(find_installed_services)

    if [[ ${#services[@]} -eq 0 ]]; then
        warn "No Freezer Monitor services are installed on this machine."
        return
    fi

    for svc in "${services[@]}"; do
        echo
        echo -e "  ${GREEN}${svc}${NC}"
        local state enabled
        state=$(systemctl is-active   "$svc" 2>/dev/null || echo "unknown")
        enabled=$(systemctl is-enabled "$svc" 2>/dev/null || echo "unknown")

        local state_color="$NC"
        [[ "$state" == "active"   ]] && state_color="$GREEN"
        [[ "$state" == "inactive" ]] && state_color="$YELLOW"
        [[ "$state" == "failed"   ]] && state_color="$RED"

        printf "    %-10s %b%s%b\n" "State:"   "$state_color" "$state"   "$NC"
        printf "    %-10s %s\n"     "Enabled:" "$enabled"

        # Show uptime or time since stop
        local ts
        if [[ "$state" == "active" ]]; then
            ts=$(systemctl show "$svc" --property=ActiveEnterTimestamp --value 2>/dev/null || true)
            [[ -n "$ts" ]] && printf "    %-10s %s\n" "Since:" "$ts"
        fi

        # Show recent log lines
        echo "    Recent log:"
        journalctl -u "$svc" -n 4 --no-pager --output=short 2>/dev/null \
            | sed 's/^/      /' || true
    done
    echo
}

# ═══════════════════════════════════════════════════════════════════════════════
# SERVICE CONTROL  (start / stop / restart)
# ═══════════════════════════════════════════════════════════════════════════════
do_service_control() {
    section "Service Control"
    mapfile -t services < <(find_installed_services)

    if [[ ${#services[@]} -eq 0 ]]; then
        warn "No Freezer Monitor services are installed on this machine."
        return
    fi

    # Build selection menu from installed services
    echo "Installed services:"
    local i=1
    for svc in "${services[@]}"; do
        local state; state=$(systemctl is-active "$svc" 2>/dev/null || echo "unknown")
        printf "  %d) %-50s [%s]\n" "$i" "$svc" "$state"
        ((i++))
    done
    # Add "all" option if more than one service
    if [[ ${#services[@]} -gt 1 ]]; then
        printf "  %d) All services\n" "$i"
    fi

    local max="$((${#services[@]} + (${#services[@]} > 1 ? 1 : 0)))"
    local sel=""
    while ! [[ "$sel" =~ ^[0-9]+$ ]] || (( sel < 1 || sel > max )); do
        read -rp "Select service [1-${max}]: " sel
    done

    local targets=()
    if (( sel <= ${#services[@]} )); then
        targets=("${services[$((sel-1))]}")
    else
        targets=("${services[@]}")
    fi

    echo
    echo "  1) Start"
    echo "  2) Stop"
    echo "  3) Restart"
    local action=""
    while [[ "$action" != "1" && "$action" != "2" && "$action" != "3" ]]; do
        read -rp "Select action [1/2/3]: " action
    done
    local cmd
    case "$action" in
        1) cmd="start"   ;;
        2) cmd="stop"    ;;
        3) cmd="restart" ;;
    esac

    for svc in "${targets[@]}"; do
        systemctl "$cmd" "$svc"
        local state; state=$(systemctl is-active "$svc" 2>/dev/null || echo "unknown")
        info "${cmd^} $svc — now: $state"
    done
}

# ═══════════════════════════════════════════════════════════════════════════════
# UNINSTALL
# ═══════════════════════════════════════════════════════════════════════════════
do_uninstall() {
    section "Uninstall"
    mapfile -t services < <(find_installed_services)

    if [[ ${#services[@]} -eq 0 ]]; then
        warn "No Freezer Monitor services are installed on this machine."
        return
    fi

    echo "Installed services:"
    for svc in "${services[@]}"; do
        echo "  - $svc"
    done
    echo

    select_component "Remove which component"

    # Determine which services to remove
    local to_remove=()
    for svc in "${services[@]}"; do
        case "$COMPONENT" in
            server) [[ "$svc" == "$SERVER_SERVICE" ]]             && to_remove+=("$svc") ;;
            client) [[ "$svc" == freezer-monitor-client-* ]]      && to_remove+=("$svc") ;;
            both)   to_remove+=("$svc") ;;
        esac
    done

    if [[ ${#to_remove[@]} -eq 0 ]]; then
        warn "No matching installed services found for '$COMPONENT'."
        return
    fi

    echo "Will remove services:"
    for svc in "${to_remove[@]}"; do
        echo "  - $svc"
    done

    ask_yn "Proceed?" "n" || return

    for svc in "${to_remove[@]}"; do
        systemctl stop    "$svc" 2>/dev/null || true
        systemctl disable "$svc" 2>/dev/null || true
        rm -f "/etc/systemd/system/${svc}.service"
        info "Removed service: $svc"
    done
    systemctl daemon-reload

    # Offer to remove project files
    local detected_install_dir; detected_install_dir=$(find_install_dir)
    if [[ -n "$detected_install_dir" && -d "$detected_install_dir" ]]; then
        echo
        warn "Project directory: $detected_install_dir"
        warn "This includes the database, logs, configuration, and virtual environment."
        if ask_yn "Also delete all project files? This is irreversible." "n"; then
            rm -rf "$detected_install_dir"
            info "Removed $detected_install_dir"
        fi
    fi

    info "Uninstall complete."
}

# ═══════════════════════════════════════════════════════════════════════════════
# INSTALL / RECONFIGURE  — shared setup steps
# ═══════════════════════════════════════════════════════════════════════════════
install_system_packages() {
    section "System Packages"
    info "Updating package lists..."
    apt-get update -qq
    local pkgs="python3 python3-dev python3-venv python3-pip"
    [[ "$1" == *server* || "$1" == *both* ]] && pkgs="$pkgs redis-server"
    info "Installing: $pkgs"
    apt-get install -y $pkgs
    if [[ "$1" == *server* || "$1" == *both* ]]; then
        systemctl enable redis-server
        systemctl start  redis-server
    fi
}

install_venv() {
    section "Python Environment"
    if [[ ! -d "$VENV_DIR" ]]; then
        info "Creating virtual environment at $VENV_DIR ..."
        sudo -u "$REAL_USER" python3 -m venv "$VENV_DIR"
    else
        info "Virtual environment already exists — skipping creation."
    fi
    info "Installing/updating Python packages..."
    sudo -u "$REAL_USER" "$PIP" install --upgrade pip --quiet
    if [[ "$1" == *server* || "$1" == *both* ]]; then
        sudo -u "$REAL_USER" "$PIP" install \
            flask apscheduler gunicorn gevent redis python-dotenv requests --quiet
    else
        sudo -u "$REAL_USER" "$PIP" install \
            requests apscheduler python-dotenv --quiet
    fi
    info "Python packages up to date."
}

# ── Server install/reconfigure ─────────────────────────────────────────────────
install_server() {
    section "Server Configuration"
    local SECRETS_FILE="$SERVER_DIR/secrets.env"

    # Load existing values as defaults
    local CUR_EMAIL;        CUR_EMAIL=$(read_cfg_var        "$SECRETS_FILE" "EMAIL")
    local CUR_EMAIL_SERVER; CUR_EMAIL_SERVER=$(read_cfg_var "$SECRETS_FILE" "EMAIL_SERVER")
    local CUR_EMAIL_PORT;   CUR_EMAIL_PORT=$(read_cfg_var   "$SECRETS_FILE" "EMAIL_PORT")
    local CUR_EMAIL_USER;   CUR_EMAIL_USER=$(read_cfg_var   "$SECRETS_FILE" "EMAIL_USERNAME")
    local CUR_EMAIL_PASS;   CUR_EMAIL_PASS=$(read_cfg_var   "$SECRETS_FILE" "EMAIL_PASSWORD")
    local CUR_PHONE;        CUR_PHONE=$(read_cfg_var        "$SECRETS_FILE" "PHONE_NUMBER")
    local CUR_CALLMEBOT;    CUR_CALLMEBOT=$(read_cfg_var    "$SECRETS_FILE" "CALLMEBOT_API")
    local CUR_API_KEY;      CUR_API_KEY=$(read_cfg_var      "$SECRETS_FILE" "SUBMIT_API_KEY")

    # Email / SMTP
    echo "Email is used to send temperature and sensor alerts."
    local EMAIL_ADDR EMAIL_SERVER EMAIL_PORT EMAIL_USER EMAIL_PASS
    ask          EMAIL_ADDR   "Alert email address (from & to)"  "$CUR_EMAIL"
    ask          EMAIL_SERVER "SMTP server"                       "${CUR_EMAIL_SERVER:-smtp.gmail.com}"
    ask          EMAIL_PORT   "SMTP port"                         "${CUR_EMAIL_PORT:-587}"
    ask          EMAIL_USER   "SMTP username"                     "${CUR_EMAIL_USER:-$EMAIL_ADDR}"
    ask_password EMAIL_PASS   "SMTP password"                     "$CUR_EMAIL_PASS"

    # WhatsApp (optional)
    echo
    local PHONE_NUMBER="" CALLMEBOT_API=""
    local WA_DEFAULT="n"; [[ -n "$CUR_PHONE" ]] && WA_DEFAULT="y"
    if ask_yn "Configure WhatsApp alerts via CallMeBot?" "$WA_DEFAULT"; then
        ask PHONE_NUMBER  "WhatsApp phone number (with country code)"  "$CUR_PHONE"
        ask CALLMEBOT_API "CallMeBot API key"                           "$CUR_CALLMEBOT"
    fi

    # API key — preserve existing
    local SUBMIT_API_KEY
    if [[ -n "$CUR_API_KEY" ]]; then
        SUBMIT_API_KEY="$CUR_API_KEY"
        info "Preserving existing SUBMIT_API_KEY."
    else
        SUBMIT_API_KEY=$(python3 -c "import secrets; print(secrets.token_hex(32))")
        info "Generated new SUBMIT_API_KEY."
    fi

    # Write secrets.env
    cat > "$SECRETS_FILE" <<EOF
EMAIL = $EMAIL_ADDR
PHONE_NUMBER = $PHONE_NUMBER
EMAIL_SERVER = $EMAIL_SERVER
EMAIL_PORT = $EMAIL_PORT
EMAIL_USERNAME = $EMAIL_USER
EMAIL_PASSWORD = $EMAIL_PASS
CALLMEBOT_API = $CALLMEBOT_API
SUBMIT_API_KEY = $SUBMIT_API_KEY
EOF
    chmod 600 "$SECRETS_FILE"
    chown "$REAL_USER:$REAL_USER" "$SECRETS_FILE"
    info "Wrote $SECRETS_FILE (mode 600)."

    # Update server_config.py
    local SERVER_LOG_FILE="$LOG_DIR/server.log"
    local REPORTING_CONFIG="$SERVER_DIR/reporting_config.json"
    sed -i \
        -e "s|SERVER_LOG_FILE = .*|SERVER_LOG_FILE = \"$SERVER_LOG_FILE\"|" \
        -e "s|REPORTING_CONFIG = .*|REPORTING_CONFIG = \"$REPORTING_CONFIG\"|" \
        "$SERVER_DIR/server_config.py"
    info "Updated server_config.py"

    # Add or update a sensor display name
    echo
    if ask_yn "Add or update a sensor display name?" "n"; then
        local DISP_SENSOR_ID DISP_SENSOR_NAME
        ask DISP_SENSOR_ID   "Sensor ID (as reported by the client)"
        ask DISP_SENSOR_NAME "Display name"
        python3 - "$SERVER_DIR/server_config.py" "$DISP_SENSOR_ID" "$DISP_SENSOR_NAME" <<'PYEOF'
import re, sys
path, sensor_id, display_name = sys.argv[1], sys.argv[2], sys.argv[3]
with open(path) as f:
    content = f.read()
updated = re.sub(
    r'("' + re.escape(sensor_id) + r'"\s*:\s*)"[^"]*"',
    r'\1"' + display_name + '"',
    content
)
if updated != content:
    print(f"Updated display name for '{sensor_id}'.")
else:
    updated = re.sub(
        r'(SENSOR_DISPLAY_NAMES\s*=\s*\{)',
        r'\1\n    "' + sensor_id + '": "' + display_name + '",',
        content
    )
    print(f"Added display name for '{sensor_id}'.")
with open(path, 'w') as f:
    f.write(updated)
PYEOF
    fi

    # Initialise / migrate database
    section "Database"
    info "Running database init (safe to re-run) ..."
    pushd "$SERVER_DIR" > /dev/null
    sudo -u "$REAL_USER" "$PYTHON" -c "from db_setup import init_db; init_db()"
    popd > /dev/null
    info "Database ready."

    # Systemd service
    section "Systemd Service (server)"
    local sf="/etc/systemd/system/${SERVER_SERVICE}.service"
    cat > "$sf" <<EOF
[Unit]
Description=Freezer Monitor Server (Gunicorn)
After=network.target redis.service

[Service]
User=$REAL_USER
WorkingDirectory=$SERVER_DIR
ExecStart=$VENV_DIR/bin/gunicorn -w 1 -b 0.0.0.0:5000 --worker-class gevent probe_server:create_app()
Environment=PYTHONUNBUFFERED=1
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF
    systemctl daemon-reload
    systemctl enable "$SERVER_SERVICE"
    if systemctl is-active --quiet "$SERVER_SERVICE" 2>/dev/null; then
        systemctl restart "$SERVER_SERVICE"
        info "Service $SERVER_SERVICE restarted."
    else
        systemctl start "$SERVER_SERVICE"
        info "Service $SERVER_SERVICE started."
    fi

    echo
    info "Server setup complete."
    echo "  Dashboard:      http://$(hostname -I | awk '{print $1}'):5000"
    echo "  Log file:       $LOG_DIR/server.log"
    echo "  SUBMIT_API_KEY: $SUBMIT_API_KEY"
    [[ -z "$CUR_API_KEY" ]] && warn "Copy the SUBMIT_API_KEY — you will need it when setting up each client."
}

# ── Client install/reconfigure ─────────────────────────────────────────────────
install_client() {
    section "Client Configuration"
    local SECRETS_FILE="$SERVER_DIR/secrets.env"

    # Load existing values as defaults
    local CUR_SERVER_URL; CUR_SERVER_URL=$(read_cfg_var "$CLIENT_DIR/client_config.py" "SERVER_URL")
    local CUR_SENSOR_ID;  CUR_SENSOR_ID=$(read_cfg_var  "$CLIENT_DIR/client_config.py" "SENSOR_ID")
    local CUR_EMAIL;      CUR_EMAIL=$(read_cfg_var      "$SECRETS_FILE" "EMAIL")
    local CUR_EMAIL_SERVER; CUR_EMAIL_SERVER=$(read_cfg_var "$SECRETS_FILE" "EMAIL_SERVER")
    local CUR_EMAIL_PORT;   CUR_EMAIL_PORT=$(read_cfg_var   "$SECRETS_FILE" "EMAIL_PORT")
    local CUR_EMAIL_USER;   CUR_EMAIL_USER=$(read_cfg_var   "$SECRETS_FILE" "EMAIL_USERNAME")
    local CUR_EMAIL_PASS;   CUR_EMAIL_PASS=$(read_cfg_var   "$SECRETS_FILE" "EMAIL_PASSWORD")
    local CUR_API_KEY;      CUR_API_KEY=$(read_cfg_var      "$SECRETS_FILE" "SUBMIT_API_KEY")

    local SERVER_URL SENSOR_ID SUBMIT_KEY
    ask SERVER_URL "Server URL"    "${CUR_SERVER_URL:-http://192.168.1.10:5000}"
    ask SENSOR_ID  "Sensor ID"     "${CUR_SENSOR_ID:-freezer_1}"
    ask SUBMIT_KEY "SUBMIT_API_KEY (from server setup)" "$CUR_API_KEY"

    # Email alerts
    echo
    local EMAIL_ADDR="" EMAIL_SERVER_C="" EMAIL_PORT_C="" EMAIL_USER_C="" EMAIL_PASS_C=""
    local EMAIL_DEFAULT="n"; [[ -n "$CUR_EMAIL" ]] && EMAIL_DEFAULT="y"
    if ask_yn "Configure email alerts on this client?" "$EMAIL_DEFAULT"; then
        ask          EMAIL_ADDR     "Alert email address"  "$CUR_EMAIL"
        ask          EMAIL_SERVER_C "SMTP server"           "${CUR_EMAIL_SERVER:-smtp.gmail.com}"
        ask          EMAIL_PORT_C   "SMTP port"             "${CUR_EMAIL_PORT:-587}"
        ask          EMAIL_USER_C   "SMTP username"         "${CUR_EMAIL_USER:-$EMAIL_ADDR}"
        ask_password EMAIL_PASS_C   "SMTP password"         "$CUR_EMAIL_PASS"
    fi

    # Write secrets.env
    cat > "$SECRETS_FILE" <<EOF
EMAIL = $EMAIL_ADDR
PHONE_NUMBER =
EMAIL_SERVER = $EMAIL_SERVER_C
EMAIL_PORT = $EMAIL_PORT_C
EMAIL_USERNAME = $EMAIL_USER_C
EMAIL_PASSWORD = $EMAIL_PASS_C
CALLMEBOT_API =
SUBMIT_API_KEY = $SUBMIT_KEY
EOF
    chmod 600 "$SECRETS_FILE"
    chown "$REAL_USER:$REAL_USER" "$SECRETS_FILE"
    info "Wrote $SECRETS_FILE (mode 600)."

    # Update client_config.py; stop old service if sensor ID changed
    local CLIENT_LOG_FILE="$LOG_DIR/client_${SENSOR_ID}.log"
    if [[ -n "$CUR_SENSOR_ID" && "$CUR_SENSOR_ID" != "$SENSOR_ID" ]]; then
        local OLD_SVC="freezer-monitor-client-${CUR_SENSOR_ID}"
        if [[ -f "/etc/systemd/system/${OLD_SVC}.service" ]]; then
            warn "Sensor ID changed '$CUR_SENSOR_ID' → '$SENSOR_ID'; removing old service."
            systemctl stop    "$OLD_SVC" 2>/dev/null || true
            systemctl disable "$OLD_SVC" 2>/dev/null || true
            rm -f "/etc/systemd/system/${OLD_SVC}.service"
        fi
    fi

    sed -i \
        -e "s|SERVER_URL = .*|SERVER_URL = \"$SERVER_URL\"|" \
        -e "s|SENSOR_ID = .*|SENSOR_ID = \"$SENSOR_ID\"|" \
        -e "s|CLIENT_LOG_FILE = .*|CLIENT_LOG_FILE = \"$CLIENT_LOG_FILE\"|" \
        "$CLIENT_DIR/client_config.py"
    info "Updated client_config.py"

    # 1-Wire interface
    section "1-Wire Interface"
    local ONEWIRE_ENABLED=false
    local BOOT_CONFIG="/boot/config.txt"
    [[ -f /boot/firmware/config.txt ]] && BOOT_CONFIG="/boot/firmware/config.txt"

    if grep -q "dtoverlay=w1-gpio" "$BOOT_CONFIG" 2>/dev/null; then
        info "1-Wire already enabled in $BOOT_CONFIG."
    else
        warn "1-Wire does not appear to be enabled."
        if ask_yn "Enable 1-Wire (dtoverlay=w1-gpio) in $BOOT_CONFIG?" "y"; then
            echo "dtoverlay=w1-gpio" >> "$BOOT_CONFIG"
            info "Added dtoverlay=w1-gpio to $BOOT_CONFIG."
            ONEWIRE_ENABLED=true
        fi
    fi

    if ! lsmod | grep -q w1_gpio 2>/dev/null; then
        modprobe w1-gpio  2>/dev/null || true
        modprobe w1-therm 2>/dev/null || true
        info "Loaded w1_gpio and w1_therm kernel modules."
    fi

    local W1_BASE="/sys/bus/w1/devices"
    sleep 2
    local SENSOR_PATH; SENSOR_PATH=$(ls -d "${W1_BASE}/28-"* 2>/dev/null | head -1 || true)
    if [[ -n "$SENSOR_PATH" ]]; then
        info "DS18B20 sensor detected at: $SENSOR_PATH"
    else
        warn "No DS18B20 sensor detected at $W1_BASE/28-*"
        $ONEWIRE_ENABLED \
            && warn "A reboot is required before the sensor will be accessible." \
            || warn "Check wiring — sensor should be connected to GPIO4."
    fi

    # Systemd service
    section "Systemd Service (client)"
    local SVC_NAME="freezer-monitor-client-${SENSOR_ID}"
    cat > "/etc/systemd/system/${SVC_NAME}.service" <<EOF
[Unit]
Description=Freezer Monitor Client ($SENSOR_ID)
After=network.target

[Service]
User=$REAL_USER
WorkingDirectory=$CLIENT_DIR
ExecStart=$VENV_DIR/bin/python $CLIENT_DIR/probe_client.py
Environment=PYTHONUNBUFFERED=1
Environment=PYTHONPATH=$SERVER_DIR
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF
    systemctl daemon-reload
    systemctl enable "$SVC_NAME"

    if $ONEWIRE_ENABLED; then
        warn "Service installed but NOT started — reboot required for 1-Wire."
    elif systemctl is-active --quiet "$SVC_NAME" 2>/dev/null; then
        systemctl restart "$SVC_NAME"
        info "Service $SVC_NAME restarted."
    else
        systemctl start "$SVC_NAME"
        info "Service $SVC_NAME started."
    fi

    echo
    info "Client setup complete."
    echo "  Sensor ID:  $SENSOR_ID"
    echo "  Server URL: $SERVER_URL"
    echo "  Log file:   $CLIENT_LOG_FILE"

    if $ONEWIRE_ENABLED; then
        echo
        warn "1-Wire was just enabled. A reboot is required before the sensor works."
        if ask_yn "Reboot now?" "y"; then
            reboot
        fi
    fi
}

# ── Top-level install/reconfigure dispatcher ───────────────────────────────────
do_install() {
    section "Install / Reconfigure"

    if [[ ! -f "$SCRIPT_DIR/server/probe_server.py" ]]; then
        error "Cannot find server/probe_server.py relative to $SCRIPT_DIR."
        error "Run this script from the root of the Freezer Monitor project."
        return 1
    fi

    select_component "Install which component"

    # Determine install directory (auto-detect from existing services or ask)
    local detected; detected=$(find_install_dir)
    ask INSTALL_DIR "Project directory" "${detected:-$SCRIPT_DIR}"
    set_dirs
    mkdir -p "$LOG_DIR"
    chown -R "$REAL_USER:$REAL_USER" "$LOG_DIR"

    if [[ "$INSTALL_DIR" != "$SCRIPT_DIR" ]]; then
        info "Copying project files to $INSTALL_DIR ..."
        mkdir -p "$INSTALL_DIR"
        cp -r "$SCRIPT_DIR/"* "$INSTALL_DIR/"
    fi

    install_system_packages "$COMPONENT"
    install_venv "$COMPONENT"

    [[ "$COMPONENT" == "server" || "$COMPONENT" == "both" ]] && install_server
    [[ "$COMPONENT" == "client" || "$COMPONENT" == "both" ]] && install_client
}

# ═══════════════════════════════════════════════════════════════════════════════
# MAIN MENU
# ═══════════════════════════════════════════════════════════════════════════════
while true; do
    section "Freezer Monitor"
    echo "  1) Install / Reconfigure"
    echo "  2) Start / Stop / Restart services"
    echo "  3) Check service status"
    echo "  4) Uninstall"
    echo "  5) Exit"
    echo
    MENU=""
    while ! [[ "$MENU" =~ ^[1-5]$ ]]; do
        read -rp "Select option [1-5]: " MENU
    done

    case "$MENU" in
        1) do_install         ;;
        2) do_service_control ;;
        3) do_status          ;;
        4) do_uninstall       ;;
        5) echo "Goodbye."; exit 0 ;;
    esac

    echo
    read -rp "Press Enter to return to the menu..."
done
