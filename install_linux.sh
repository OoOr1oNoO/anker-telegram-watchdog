#!/usr/bin/env bash
# Installs anker_ups as a systemd service on a Linux VM (Ubuntu / Oracle Linux, x86_64 or ARM).
# Usage (from the folder with anker_ups.py, translations.py and .env):  sudo bash install_linux.sh
# Re-run it to update the script or .env; it restarts the service.
set -euo pipefail

APP_DIR=/opt/anker-ups
APP_USER=anker
SRC_DIR="$(cd "$(dirname "$0")" && pwd)"

[ "$(id -u)" -eq 0 ] || { echo "Run with sudo"; exit 1; }
for f in anker_ups.py translations.py; do
    [ -f "$SRC_DIR/$f" ] || { echo "$f not found next to this script"; exit 1; }
done
[ -f "$SRC_DIR/.env" ] || { echo ".env not found next to this script"; exit 1; }

command -v curl >/dev/null || { (apt-get update && apt-get install -y curl) || dnf install -y curl; }

if ! command -v uv >/dev/null && [ ! -x /usr/local/bin/uv ]; then
    curl -LsSf https://astral.sh/uv/install.sh | env UV_INSTALL_DIR=/usr/local/bin UV_NO_MODIFY_PATH=1 sh
fi
UV=$(command -v uv || echo /usr/local/bin/uv)
# SELinux (Oracle Linux): the uv installer moves files from /tmp and keeps the user_tmp_t
# label, which systemd may not execute (status=203/EXEC)
command -v restorecon >/dev/null && restorecon -F /usr/local/bin/uv /usr/local/bin/uvx 2>/dev/null || true

id "$APP_USER" >/dev/null 2>&1 || useradd --system --home-dir "$APP_DIR" --shell /usr/sbin/nologin "$APP_USER"
mkdir -p "$APP_DIR"
install -m 0644 "$SRC_DIR/anker_ups.py" "$SRC_DIR/translations.py" "$APP_DIR/"
install -m 0600 "$SRC_DIR/.env" "$APP_DIR/.env"
chown -R "$APP_USER:$APP_USER" "$APP_DIR"

# uv keeps Python and packages inside the app folder, so the service user needs no home
UV_ENV="UV_CACHE_DIR=$APP_DIR/.cache/uv UV_PYTHON_INSTALL_DIR=$APP_DIR/.python"
echo "Preparing Python environment (first run downloads Python, takes a minute)..."
cd "$APP_DIR"  # uv probes the current dir; the caller's home may be unreadable for $APP_USER
sudo -u "$APP_USER" env $UV_ENV "$UV" run --script "$APP_DIR/anker_ups.py" --help >/dev/null

cat > /etc/systemd/system/anker-ups.service <<EOF
[Unit]
Description=Anker SOLIX C1000X watchdog (Telegram alerts)
After=network-online.target
Wants=network-online.target

[Service]
User=$APP_USER
WorkingDirectory=$APP_DIR
Environment=UV_CACHE_DIR=$APP_DIR/.cache/uv
Environment=UV_PYTHON_INSTALL_DIR=$APP_DIR/.python
Environment=PYTHONUNBUFFERED=1
ExecStart=$UV run --script $APP_DIR/anker_ups.py
Restart=always
RestartSec=30

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable anker-ups >/dev/null
systemctl restart anker-ups
sleep 5
systemctl --no-pager --lines=15 status anker-ups || true
echo
echo "Logs:    journalctl -u anker-ups -f"
echo "Restart: sudo systemctl restart anker-ups"
