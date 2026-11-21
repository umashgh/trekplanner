#!/usr/bin/env bash
#
# TrekPlanner — full server setup
#
# Usage:
#   1. Clone the repo and cd into it
#   2. Place data_dump.json in the project root (if restoring data)
#   3. Run: bash setup.sh
#
set -euo pipefail

APP_DIR="$(cd "$(dirname "$0")" && pwd)"
VENV_DIR="$APP_DIR/.venv"
PORT="${TREKPLANNER_PORT:-8200}"
SERVICE_NAME="trekplanner"

echo "=== TrekPlanner Setup ==="
echo "App directory: $APP_DIR"
echo ""

# ── 1. System dependencies ──────────────────────────────────────────────────

echo "--- Checking Python 3 ---"
if ! command -v python3 &>/dev/null; then
    echo "ERROR: python3 not found. Install Python 3.10+ first."
    exit 1
fi

PYTHON_VERSION=$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
echo "Found Python $PYTHON_VERSION"

if ! python3 -c 'import sys; assert sys.version_info >= (3, 10)' 2>/dev/null; then
    echo "ERROR: Python 3.10+ required, found $PYTHON_VERSION"
    exit 1
fi

# ── 2. Virtual environment ──────────────────────────────────────────────────

echo ""
echo "--- Setting up virtual environment ---"
if [ ! -d "$VENV_DIR" ]; then
    python3 -m venv "$VENV_DIR"
    echo "Created venv at $VENV_DIR"
else
    echo "Venv already exists at $VENV_DIR"
fi

source "$VENV_DIR/bin/activate"
pip install --upgrade pip --quiet
pip install -r "$APP_DIR/requirements.txt" --quiet
echo "Dependencies installed."

# ── 3. Environment file ────────────────────────────────────────────────────

echo ""
echo "--- Checking .env ---"
if [ ! -f "$APP_DIR/.env" ]; then
    SECRET_KEY=$(python3 -c "from django.core.management.utils import get_random_secret_key; print(get_random_secret_key())")
    cat > "$APP_DIR/.env" <<EOF
DJANGO_DEBUG=0
DJANGO_SECRET_KEY=$SECRET_KEY
DJANGO_ALLOWED_HOSTS=localhost,127.0.0.1
EOF
    echo "Created .env with a fresh secret key."
    echo "ACTION REQUIRED: Edit .env and set DJANGO_ALLOWED_HOSTS to your domain."
else
    echo ".env already exists — skipping."
fi

# ── 4. Create directories ──────────────────────────────────────────────────

echo ""
echo "--- Creating directories ---"
mkdir -p "$APP_DIR/logs"
echo "logs/ ready."

# ── 5. Database migrations ─────────────────────────────────────────────────

echo ""
echo "--- Running migrations ---"
python3 "$APP_DIR/manage.py" migrate --no-input

# ── 6. Load data dump (if present) ─────────────────────────────────────────

echo ""
if [ -f "$APP_DIR/data_dump.json" ]; then
    echo "--- Loading data from data_dump.json ---"
    python3 "$APP_DIR/manage.py" loaddata "$APP_DIR/data_dump.json"
    echo "Data loaded."
else
    echo "--- No data_dump.json found — starting with empty database ---"
    echo "To import data later: python manage.py loaddata data_dump.json"
fi

# ── 7. Collect static files ────────────────────────────────────────────────

echo ""
echo "--- Collecting static files ---"
python3 "$APP_DIR/manage.py" collectstatic --no-input --clear --quiet
echo "Static files collected."

# ── 8. Create superuser (interactive) ──────────────────────────────────────

echo ""
if ! python3 "$APP_DIR/manage.py" shell -c "from django.contrib.auth.models import User; exit(0 if User.objects.filter(is_superuser=True).exists() else 1)" 2>/dev/null; then
    echo "--- No superuser found ---"
    read -rp "Create one now? [Y/n] " CREATE_SUPER
    if [[ "${CREATE_SUPER:-Y}" =~ ^[Yy]$ ]]; then
        python3 "$APP_DIR/manage.py" createsuperuser
    else
        echo "Skipped. Create one later: python manage.py createsuperuser"
    fi
else
    echo "--- Superuser already exists — skipping ---"
fi

# ── 9. Systemd service ─────────────────────────────────────────────────────

echo ""
echo "--- Setting up systemd user service ---"
SYSTEMD_DIR="$HOME/.config/systemd/user"
SERVICE_FILE="$SYSTEMD_DIR/$SERVICE_NAME.service"
mkdir -p "$SYSTEMD_DIR"

cat > "$SERVICE_FILE" <<EOF
[Unit]
Description=TrekPlanner — Which hike next? (gunicorn)
After=network.target

[Service]
Type=simple
WorkingDirectory=$APP_DIR

Environment="PATH=$VENV_DIR/bin:/usr/local/bin:/usr/bin:/bin"
Environment="VIRTUAL_ENV=$VENV_DIR"
Environment="DJANGO_SETTINGS_MODULE=trekplanner.settings"
Environment="PYTHONUNBUFFERED=1"

ExecStart=$VENV_DIR/bin/gunicorn \\
    --workers 2 \\
    --bind 127.0.0.1:$PORT \\
    --timeout 120 \\
    --access-logfile $APP_DIR/logs/gunicorn-access.log \\
    --error-logfile $APP_DIR/logs/gunicorn-error.log \\
    trekplanner.wsgi:application

Restart=always
RestartSec=10
StartLimitInterval=0

StandardOutput=journal
StandardError=journal
SyslogIdentifier=trekplanner

[Install]
WantedBy=default.target
EOF

systemctl --user daemon-reload
systemctl --user enable "$SERVICE_NAME"
systemctl --user restart "$SERVICE_NAME"

echo "Service installed and started on port $PORT."
echo ""

# ── 10. Verify ──────────────────────────────────────────────────────────────

sleep 2
if systemctl --user is-active --quiet "$SERVICE_NAME"; then
    echo "=== Setup complete ==="
    echo ""
    echo "  App running on http://127.0.0.1:$PORT"
    echo "  Admin panel: http://127.0.0.1:$PORT/admin/"
    echo ""
    echo "  Manage service:"
    echo "    systemctl --user status $SERVICE_NAME"
    echo "    systemctl --user restart $SERVICE_NAME"
    echo "    journalctl --user -u $SERVICE_NAME -f"
    echo ""
    echo "  To expose via Cloudflare Tunnel, add to your config.yaml:"
    echo "    - hostname: treks.yourdomain.com"
    echo "      service: http://127.0.0.1:$PORT"
else
    echo "WARNING: Service failed to start. Check logs:"
    echo "  journalctl --user -u $SERVICE_NAME --no-pager -n 30"
fi
