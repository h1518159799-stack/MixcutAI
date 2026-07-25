#!/usr/bin/env bash
set -euo pipefail

export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y python3-venv python3-pip ufw unattended-upgrades ca-certificates curl sqlite3 \
  debian-keyring debian-archive-keyring apt-transport-https gnupg
if ! command -v caddy >/dev/null 2>&1; then
  curl -1sLf https://dl.cloudsmith.io/public/caddy/stable/gpg.key | \
    gpg --dearmor --yes -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
  curl -1sLf https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt | \
    tee /etc/apt/sources.list.d/caddy-stable.list >/dev/null
  chmod o+r /usr/share/keyrings/caddy-stable-archive-keyring.gpg
  chmod o+r /etc/apt/sources.list.d/caddy-stable.list
  apt-get update
  apt-get install -y caddy
fi

if ! id mixcut >/dev/null 2>&1; then
  useradd --system --home /var/lib/mixcut --shell /usr/sbin/nologin mixcut
fi

install -d -o root -g root -m 0755 /opt/mixcut/server /opt/mixcut/venv
install -d -o mixcut -g mixcut -m 0750 /var/lib/mixcut
install -d -o root -g mixcut -m 0750 /etc/mixcut
install -d -o caddy -g caddy -m 0755 /srv/mixcut/updates /var/log/caddy

cp /root/mixcut-deploy/license_server.py /opt/mixcut/server/
cp /root/mixcut-deploy/mixcut_admin.py /opt/mixcut/server/
cp /root/mixcut-deploy/requirements.txt /opt/mixcut/server/
cp -a /root/mixcut-deploy/templates /opt/mixcut/server/
cp -a /root/mixcut-deploy/static /opt/mixcut/server/
install -o root -g mixcut -m 0640 /root/mixcut-deploy/license_private_key.pem /etc/mixcut/license_private_key.pem
if [[ ! -s /etc/mixcut/key_pepper ]]; then
  umask 0077
  python3 - <<'PY' > /etc/mixcut/key_pepper
import secrets
print(secrets.token_urlsafe(48))
PY
  chown root:mixcut /etc/mixcut/key_pepper
  chmod 0640 /etc/mixcut/key_pepper
fi
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

python3 -m venv /opt/mixcut/venv
/opt/mixcut/venv/bin/pip install --disable-pip-version-check --no-cache-dir -r /opt/mixcut/server/requirements.txt
chown -R root:root /opt/mixcut/server /opt/mixcut/venv
chmod -R a+rX /opt/mixcut/server /opt/mixcut/venv
chmod 0755 /opt/mixcut/server /opt/mixcut/venv

install -o root -g root -m 0644 /root/mixcut-deploy/mixcut-license.service /etc/systemd/system/mixcut-license.service
install -o root -g root -m 0644 /root/mixcut-deploy/Caddyfile /etc/caddy/Caddyfile
install -o caddy -g caddy -m 0644 /root/mixcut-deploy/update.json /srv/mixcut/updates/update.json
install -o caddy -g caddy -m 0644 /root/MixcutAI.exe /srv/mixcut/updates/MixcutAI.exe

if ! swapon --show | grep -q .; then
  fallocate -l 2G /swapfile
  chmod 0600 /swapfile
  mkswap /swapfile
  swapon /swapfile
  grep -q '^/swapfile ' /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab
fi

cat >/etc/ssh/sshd_config.d/60-mixcut-hardening.conf <<'EOF'
PasswordAuthentication no
KbdInteractiveAuthentication no
PermitRootLogin prohibit-password
PubkeyAuthentication yes
EOF
sshd -t
systemctl reload ssh

ufw allow OpenSSH
ufw allow 80/tcp
ufw allow 443/tcp
ufw --force enable

systemctl daemon-reload
systemctl enable --now mixcut-license.service
caddy validate --config /etc/caddy/Caddyfile
systemctl enable --now caddy
systemctl restart caddy

cat >/etc/systemd/system/mixcut-backup.service <<'EOF'
[Unit]
Description=Backup Mixcut licensing database

[Service]
Type=oneshot
ExecStart=/bin/bash -c 'install -d -m 0700 /var/backups/mixcut && sqlite3 /var/lib/mixcut/licenses.db ".backup /var/backups/mixcut/licenses-$(date +%%Y%%m%%d-%%H%%M%%S).db" && find /var/backups/mixcut -type f -mtime +30 -delete'
EOF

cat >/etc/systemd/system/mixcut-backup.timer <<'EOF'
[Unit]
Description=Daily Mixcut licensing database backup

[Timer]
OnCalendar=*-*-* 03:20:00
Persistent=true

[Install]
WantedBy=timers.target
EOF

systemctl daemon-reload
systemctl enable --now mixcut-backup.timer

echo 'INSTALL_OK'
systemctl --no-pager --full status mixcut-license.service | head -20
systemctl --no-pager --full status caddy | head -20
