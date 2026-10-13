#!/usr/bin/env bash
set -euo pipefail

if [[ $EUID -eq 0 ]]; then
  echo 'Run this script as your normal Pi account, NOT with sudo.' >&2
  exit 1
fi

project_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
username="$(id -un)"
groupname="$(id -gn)"
python_bin="$project_dir/.venv/bin/python"
app="$project_dir/campi.py"

if [[ ! -x "$python_bin" || ! -f "$app" ]]; then
  echo 'Missing .venv/bin/python or campi.py. Follow INSTALL.md first.' >&2
  exit 1
fi

if systemctl is-active --quiet campi.service; then
  echo 'Stopping an existing CamPi service before replacing its unit file...'
  sudo systemctl stop campi.service
fi

# Here-doc is intentionally expanded to absolute account-specific paths,
# keeping the checked-in source free from a specific username.
sudo tee /etc/systemd/system/campi.service >/dev/null <<UNIT
[Unit]
Description=CamPi video capture UI
After=local-fs.target

[Service]
Type=simple
User=$username
Group=$groupname
WorkingDirectory=$project_dir
Environment=HOME=$HOME
Environment=PYTHONUNBUFFERED=1
Environment=PATH=$project_dir/.venv/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
EnvironmentFile=-/etc/campi/campi.env
ExecStart=$python_bin $app
Restart=on-failure
RestartSec=3

[Install]
WantedBy=multi-user.target
UNIT

sudo systemctl daemon-reload
sudo systemctl enable --now campi.service
systemctl status campi.service --no-pager || true
