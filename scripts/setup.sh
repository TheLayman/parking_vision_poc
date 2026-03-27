#!/usr/bin/env bash
# One-time setup script for Smart Parking Enforcement (Ubuntu 22.04 LTS)
# Run as root (or sudo) on the production server.
# Requires brief internet access for package installation.

set -euo pipefail

APP_DIR="/opt/parking"
APP_USER="parking"
DATA_DIR="/data"
SNAPSHOTS_DIR="/data/snapshots"
BACKUPS_DIR="/data/backups"
PYTHON_BIN="python3.11"

echo "=== Smart Parking Enforcement — Production Setup ==="

# ── System packages ───────────────────────────────────────────────────────────
apt-get update
apt-get install -y \
    postgresql-16 \
    redis-server \
    nginx \
    python3.11 \
    python3.11-venv \
    python3-pip \
    libpq-dev \
    libopencv-dev \
    python3-opencv \
    cron

# ── App user ──────────────────────────────────────────────────────────────────
id "$APP_USER" &>/dev/null || useradd --system --home "$APP_DIR" --shell /bin/false "$APP_USER"

# ── Data directories ──────────────────────────────────────────────────────────
mkdir -p "$SNAPSHOTS_DIR" "$BACKUPS_DIR"
chown -R "$APP_USER:$APP_USER" "$DATA_DIR"

# ── Python virtualenv ─────────────────────────────────────────────────────────
if [ ! -d "$APP_DIR/venv" ]; then
    "$PYTHON_BIN" -m venv "$APP_DIR/venv"
fi
"$APP_DIR/venv/bin/pip" install --upgrade pip
"$APP_DIR/venv/bin/pip" install -r "$APP_DIR/requirements.txt"

# ── PostgreSQL ────────────────────────────────────────────────────────────────
# Create parking database and user (skip if already exists)
sudo -u postgres psql -tc "SELECT 1 FROM pg_roles WHERE rolname='parking'" | grep -q 1 || \
    sudo -u postgres psql -c "CREATE USER parking WITH PASSWORD 'CHANGEME_DB_PASSWORD';"
sudo -u postgres psql -tc "SELECT 1 FROM pg_database WHERE datname='parking'" | grep -q 1 || \
    sudo -u postgres psql -c "CREATE DATABASE parking OWNER parking;"
sudo -u postgres psql -d parking -c "GRANT ALL PRIVILEGES ON DATABASE parking TO parking;"

# Run schema
sudo -u postgres psql -d parking < "$APP_DIR/db/schema.sql"

# ── Redis ─────────────────────────────────────────────────────────────────────
cp "$APP_DIR/config/redis.conf" /etc/redis/redis.conf
systemctl restart redis-server
systemctl enable redis-server

# ── Systemd services ──────────────────────────────────────────────────────────
cp "$APP_DIR/config/systemd/"*.service /etc/systemd/system/
systemctl daemon-reload

# Enable API
systemctl enable --now parking-api.service

# Enable MQTT workers (4 instances)
for i in 1 2 3 4; do
    systemctl enable --now "parking-mqtt-worker@${i}.service"
done

# Enable inference workers (6 instances)
for i in 1 2 3 4 5 6; do
    systemctl enable --now "parking-inference-worker@${i}.service"
done

# Camera workers: enable one per camera (edit CAM_IDs to match your deployment)
# for CAM_ID in CAM_01 CAM_02 CAM_03; do
#     systemctl enable --now "parking-camera-worker@${CAM_ID}.service"
# done
echo "Camera workers: enable manually with:"
echo "  systemctl enable --now parking-camera-worker@CAM_01.service"

# ── nginx reverse proxy ───────────────────────────────────────────────────────
cat > /etc/nginx/sites-available/parking << 'NGINX'
server {
    listen 80;
    server_name _;

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;

        # SSE support
        proxy_buffering off;
        proxy_read_timeout 3600s;
        proxy_send_timeout 3600s;
    }
}
NGINX

ln -sf /etc/nginx/sites-available/parking /etc/nginx/sites-enabled/parking
rm -f /etc/nginx/sites-enabled/default
nginx -t && systemctl reload nginx
systemctl enable nginx

# ── Cron jobs ─────────────────────────────────────────────────────────────────
cat > /etc/cron.daily/parking-snapshot-cleanup << 'CRON'
#!/bin/sh
find /data/snapshots/ -type f -name "*.jpg" -mtime +90 -delete
find /data/snapshots/ -type d -empty -delete
CRON
chmod +x /etc/cron.daily/parking-snapshot-cleanup

cat > /etc/cron.daily/parking-pg-backup << 'CRON'
#!/bin/sh
BACKUP_DIR=/data/backups
mkdir -p "$BACKUP_DIR"
pg_dump -U parking parking | gzip > "$BACKUP_DIR/pg_$(date +%F).sql.gz"
find "$BACKUP_DIR" -name "pg_*.sql.gz" -mtime +30 -delete
CRON
chmod +x /etc/cron.daily/parking-pg-backup

echo ""
echo "=== Setup complete ==="
echo "Next steps:"
echo "  1. Edit /opt/parking/.env.production (set passwords, OpenAI key)"
echo "  2. Edit /opt/parking/config/cameras.yaml (set camera IPs and credentials)"
echo "  3. Enable camera workers: systemctl enable --now parking-camera-worker@CAM_01.service"
echo "  4. Check status: systemctl status 'parking-*'"
