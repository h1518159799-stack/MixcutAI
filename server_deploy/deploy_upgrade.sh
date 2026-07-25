#!/usr/bin/env bash
set -euo pipefail

STAMP="$(date +%Y%m%d-%H%M%S)"
NEW="/root/mixcut-cloud-${STAMP}"
BACKUP_DIR="/var/backups/mixcut"
CODE_BACKUP="/opt/mixcut/server-${STAMP}.bak"
DB_BACKUP="${BACKUP_DIR}/licenses-before-cloud-${STAMP}.db"

mkdir -p "$NEW" "$BACKUP_DIR"
tar -xzf /root/mixcut-cloud-new.tgz -C "$NEW"
caddy validate --config "$NEW/Caddyfile"
sqlite3 /var/lib/mixcut/licenses.db ".backup '$DB_BACKUP'"
chmod 0600 "$DB_BACKUP"
cp -a /opt/mixcut/server "$CODE_BACKUP"

if [[ ! -s /etc/mixcut/admin_session_secret ]]; then
  umask 0077
  python3 - <<'PY' > /etc/mixcut/admin_session_secret
import secrets
print(secrets.token_hex(48))
PY
  chown root:mixcut /etc/mixcut/admin_session_secret
  chmod 0640 /etc/mixcut/admin_session_secret
fi
umask 0022

systemctl stop mixcut-license
install -o root -g root -m 0644 "$NEW/license_server.py" /opt/mixcut/server/license_server.py
install -o root -g root -m 0644 "$NEW/mixcut_admin.py" /opt/mixcut/server/mixcut_admin.py
install -o root -g root -m 0644 "$NEW/requirements.txt" /opt/mixcut/server/requirements.txt
cp -a "$NEW/templates" /opt/mixcut/server/
cp -a "$NEW/static" /opt/mixcut/server/
chown -R root:root /opt/mixcut/server
chmod -R a+rX /opt/mixcut/server
install -o root -g root -m 0644 "$NEW/mixcut-license.service" /etc/systemd/system/mixcut-license.service
install -o root -g root -m 0644 "$NEW/Caddyfile" /etc/caddy/Caddyfile

systemctl daemon-reload
systemctl start mixcut-license
for _ in $(seq 1 15); do
  if curl -fsS http://127.0.0.1:8000/health >/dev/null; then break; fi
  sleep 1
done
curl -fsS http://127.0.0.1:8000/health >/dev/null

install -o mixcut -g mixcut -m 0600 /root/mixcut-admin-password.txt /var/lib/mixcut/.admin-password.tmp
runuser -u mixcut -- /opt/mixcut/venv/bin/python /opt/mixcut/server/mixcut_admin.py \
  admin-user --username admin --password-file /var/lib/mixcut/.admin-password.tmp
shred -u /var/lib/mixcut/.admin-password.tmp /root/mixcut-admin-password.txt
chmod 0640 /var/lib/mixcut/licenses.db*

systemctl reload caddy
systemctl is-active --quiet mixcut-license
systemctl is-active --quiet caddy
echo "UPGRADE_OK"
echo "DB_BACKUP=$DB_BACKUP"
echo "CODE_BACKUP=$CODE_BACKUP"

