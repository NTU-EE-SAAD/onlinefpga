#!/usr/bin/env bash
# Configure mks.ntuee.org HTTPS. Does not alter NICs, routes, SSH, NAT or firewall rules.
set -Eeuo pipefail
umask 077
portal_root=$(cd "$(dirname "$0")/.." && pwd)
portal_domain=mks.ntuee.org
portal_email=
portal_certificate=
portal_private_key=
while [[ $# -gt 0 ]]; do
  case "$1" in
    --email) portal_email=${2:?Missing email}; shift 2 ;;
    --certificate) portal_certificate=${2:?Missing certificate path}; shift 2 ;;
    --private-key) portal_private_key=${2:?Missing key path}; shift 2 ;;
    *) echo "Unknown option: $1" >&2; exit 2 ;;
  esac
done
if [[ $EUID -ne 0 ]]; then
  echo "Run: sudo bash $portal_root/scripts/install-production.sh" >&2
  exit 1
fi
if [[ "$portal_root" == *' '* || "$portal_root" == *'"'* ]]; then
  echo 'Project path must not contain spaces or quotes.' >&2; exit 1
fi
if [[ -n "$portal_certificate" && -z "$portal_private_key" || -z "$portal_certificate" && -n "$portal_private_key" ]]; then
  echo 'Supply both --certificate and --private-key.' >&2; exit 1
fi
portal_user=$(stat -c %U "$portal_root")
portal_uid=$(id -u "$portal_user")
portal_home=$(getent passwd "$portal_user" | cut -d: -f6)
portal_override="$portal_home/.config/systemd/user/onlinefpga-web.service.d/production.conf"
portal_site=/etc/nginx/sites-available/onlinefpga-mks
portal_enabled=/etc/nginx/sites-enabled/onlinefpga-mks
portal_backup=$(mktemp -d "$portal_root/instance/production-backup-XXXXXXXX")
chown "$portal_user:$portal_user" "$portal_backup"
for portal_item in .env instance/secret.key; do
  if [[ -f "$portal_root/$portal_item" ]]; then
    cp "$portal_root/$portal_item" "$portal_backup/$(basename "$portal_item")"
    chown "$portal_user:$portal_user" "$portal_backup/$(basename "$portal_item")"
  fi
done
if [[ -f "$portal_override" ]]; then cp "$portal_override" "$portal_backup/production.conf"; fi
if [[ -f "$portal_site" ]]; then cp "$portal_site" "$portal_backup/nginx-site.conf"; fi
portal_had_enabled=false
if [[ -e "$portal_enabled" || -L "$portal_enabled" ]]; then
  portal_had_enabled=true
  cp -a "$portal_enabled" "$portal_backup/nginx-enabled"
fi
portal_app_changed=false
portal_nginx_changed=false
portal_done=false
user_service() {
  runuser -u "$portal_user" -- env XDG_RUNTIME_DIR="/run/user/$portal_uid" \
    DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/$portal_uid/bus" systemctl --user "$@"
}
rollback() {
  portal_exit=$?
  if [[ "$portal_done" != true && $portal_exit -ne 0 ]]; then
    echo "Setup failed; restoring the website's previous configuration." >&2
    if [[ "$portal_app_changed" == true ]]; then
      if [[ -f "$portal_backup/.env" ]]; then
        install -o "$portal_user" -g "$portal_user" -m 600 "$portal_backup/.env" "$portal_root/.env"
      else rm -f "$portal_root/.env"; fi
      if [[ -f "$portal_backup/production.conf" ]]; then
        install -o "$portal_user" -g "$portal_user" -m 600 "$portal_backup/production.conf" "$portal_override"
      else rm -f "$portal_override"; fi
      user_service daemon-reload || true
      user_service restart onlinefpga-web onlinefpga-scheduler || true
    fi
    if [[ "$portal_nginx_changed" == true ]]; then
      rm -f "$portal_enabled"
      if [[ -f "$portal_backup/nginx-site.conf" ]]; then cp "$portal_backup/nginx-site.conf" "$portal_site";
      else rm -f "$portal_site"; fi
      if [[ "$portal_had_enabled" == true ]]; then cp -a "$portal_backup/nginx-enabled" "$portal_enabled"; fi
      nginx -t && systemctl reload nginx || true
    fi
    echo "Backup: $portal_backup" >&2
    echo 'If certificate validation failed, check public TCP 80/NAT and the Cloudflare origin connection.' >&2
  fi
}
trap rollback EXIT
exec 9>/run/lock/onlinefpga-production.lock
flock -n 9 || { echo 'Another deployment is running.' >&2; exit 1; }
user_service is-active --quiet onlinefpga-web onlinefpga-scheduler
apt-get update
DEBIAN_FRONTEND=noninteractive apt-get install -y nginx certbot ca-certificates
install -d -m 755 /var/lib/onlinefpga-acme/.well-known/acme-challenge

if [[ -z "$portal_certificate" ]]; then
  portal_certificate="/etc/letsencrypt/live/$portal_domain/fullchain.pem"
  portal_private_key="/etc/letsencrypt/live/$portal_domain/privkey.pem"
  if [[ ! -f "$portal_certificate" ]] || ! openssl x509 -in "$portal_certificate" -noout -checkend 0 >/dev/null; then
    portal_nginx_changed=true
    install -m 644 "$portal_root/deploy/nginx-mks-bootstrap.conf" "$portal_site"
    ln -sfn "$portal_site" "$portal_enabled"
    nginx -t
    systemctl enable --now nginx
    systemctl reload nginx
    portal_probe="onlinefpga-$(openssl rand -hex 12)"
    printf '%s' "$portal_probe" > "/var/lib/onlinefpga-acme/.well-known/acme-challenge/$portal_probe"
    chmod 644 "/var/lib/onlinefpga-acme/.well-known/acme-challenge/$portal_probe"
    portal_probe_response=$(curl --silent --show-error --fail --max-time 20 \
      "http://$portal_domain/.well-known/acme-challenge/$portal_probe") || {
      rm -f "/var/lib/onlinefpga-acme/.well-known/acme-challenge/$portal_probe"
      echo 'Public HTTP challenge is unreachable. Website settings were not changed.' >&2; exit 1;
    }
    rm -f "/var/lib/onlinefpga-acme/.well-known/acme-challenge/$portal_probe"
    [[ "$portal_probe_response" == "$portal_probe" ]] || { echo 'Domain reaches a different origin.' >&2; exit 1; }
    portal_contact=(--register-unsafely-without-email)
    if [[ -n "$portal_email" ]]; then portal_contact=(--email "$portal_email"); fi
    certbot certonly --non-interactive --agree-tos "${portal_contact[@]}" \
      --webroot -w /var/lib/onlinefpga-acme --cert-name "$portal_domain" -d "$portal_domain"
  fi
  install -d -m 755 /etc/letsencrypt/renewal-hooks/deploy
  printf '#!/bin/sh\n/usr/sbin/nginx -t && /bin/systemctl reload nginx\n' > /etc/letsencrypt/renewal-hooks/deploy/onlinefpga-nginx
  chmod 755 /etc/letsencrypt/renewal-hooks/deploy/onlinefpga-nginx
  systemctl enable --now certbot.timer
fi
openssl x509 -in "$portal_certificate" -noout -checkhost "$portal_domain" | grep -q 'does match'
openssl x509 -in "$portal_certificate" -noout -checkend 0 >/dev/null
portal_cert_pub=$(openssl x509 -in "$portal_certificate" -pubkey -noout | openssl pkey -pubin -outform DER | sha256sum)
portal_key_pub=$(openssl pkey -in "$portal_private_key" -pubout -outform DER | sha256sum)
[[ "$portal_cert_pub" == "$portal_key_pub" ]] || { echo 'Certificate and key do not match.' >&2; exit 1; }
portal_nginx_changed=true
python3 - "$portal_root/deploy/nginx-mks.conf" "$portal_site" "$portal_certificate" "$portal_private_key" <<'PY'
from pathlib import Path
import sys
source, target, cert, key = sys.argv[1:]
for path in (cert,key):
    if any(c in path for c in ['"','\n',';']): raise ValueError('Invalid certificate path')
text=Path(source).read_text().replace('@CERTIFICATE@','"'+cert+'"').replace('@PRIVATE_KEY@','"'+key+'"')
Path(target).write_text(text)
Path(target).chmod(0o644)
PY
ln -sfn "$portal_site" "$portal_enabled"
nginx -t
portal_app_changed=true
install -d -o "$portal_user" -g "$portal_user" -m 700 "$(dirname "$portal_override")"
cat > "$portal_override" <<EOF
[Service]
ExecStart=
ExecStart=$portal_root/.venv/bin/gunicorn --workers 2 --worker-class gthread --threads 32 --bind 127.0.0.1:8000 --timeout 90 portal:create_app()
EOF
chown "$portal_user:$portal_user" "$portal_override"
chmod 600 "$portal_override"
python3 - "$portal_root/.env" <<'PY'
from pathlib import Path
import os,sys
path=Path(sys.argv[1]); lines=path.read_text().splitlines() if path.exists() else []
keys={'COOKIE_SECURE':'true','TRUST_PROXY':'true'}
lines=[line for line in lines if line.split('=',1)[0].strip() not in keys]
lines += [key+'='+value for key,value in keys.items()]
fd=os.open(str(path),os.O_CREAT|os.O_WRONLY|os.O_TRUNC,0o600)
with os.fdopen(fd,'w') as stream: stream.write('\n'.join(lines)+'\n')
PY
chown "$portal_user:$portal_user" "$portal_root/.env"
chmod 600 "$portal_root/.env"
user_service daemon-reload
user_service restart onlinefpga-web onlinefpga-scheduler
systemctl enable --now nginx
systemctl reload nginx
portal_ready=false
for portal_try in {1..30}; do
  if curl --silent --fail http://127.0.0.1:8000/healthz >/dev/null; then portal_ready=true; break; fi
  sleep 1
done
[[ "$portal_ready" == true ]] || { echo 'Portal health check failed.' >&2; exit 1; }
# Origin CA certificates are Cloudflare-trusted rather than publicly trusted; this
# local check tests routing. The final public check validates the browser-facing TLS.
curl --silent --show-error --fail --insecure --noproxy '*' --resolve "$portal_domain:443:127.0.0.1" \
  "https://$portal_domain/healthz"
printf '\n'
curl --silent --show-error --fail --max-time 30 "https://$portal_domain/healthz"
printf '\n'
portal_done=true
printf 'Production HTTPS ready: https://%s\nBackup: %s\n' "$portal_domain" "$portal_backup"
