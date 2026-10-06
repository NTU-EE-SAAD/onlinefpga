#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
portal_root=$(pwd)
if [[ "$portal_root" == *" "* ]]; then
  echo "systemd 安裝路徑不可含空白。" >&2
  exit 1
fi
if [[ ! -x .venv/bin/flask ]]; then
  echo "請先建立 .venv 與安裝相依套件。" >&2
  exit 1
fi
mkdir -p instance "${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
chmod 700 instance
if [[ -f .env ]]; then set -a; source .env; set +a; fi
.venv/bin/flask --app portal init-db
for portal_service in web scheduler; do
  sed "s|@PROJECT_ROOT@|$portal_root|g" "deploy/onlinefpga-$portal_service.service" > "${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user/onlinefpga-$portal_service.service"
done
systemctl --user daemon-reload
systemctl --user enable --now onlinefpga-web.service onlinefpga-scheduler.service
systemctl --user --no-pager status onlinefpga-web.service onlinefpga-scheduler.service
