# Production Deployment Guide

Target: Ubuntu 24.04 LTS, single server (64GB RAM, 24 cores, 1TB SSD)

## Prerequisites

- Ubuntu 24.04 LTS
- Root/sudo access
- Network access to ChirpStack MQTT broker
- OpenAI API key (for license plate recognition)

## 1. System Setup

### Install packages

```bash
sudo apt-get update && sudo apt-get install -y \
    postgresql-16 redis-server nginx \
    python3.11 python3.11-venv python3-pip \
    libpq-dev libopencv-dev python3-opencv cron
```

### Create application user

```bash
sudo useradd --system --home /opt/parking --shell /bin/false parking
sudo mkdir -p /opt/parking /data/snapshots /data/backups
sudo chown -R parking:parking /opt/parking /data
```

### Deploy application code

```bash
sudo cp -r . /opt/parking/
cd /opt/parking
sudo -u parking python3.11 -m venv venv
sudo -u parking venv/bin/pip install -r requirements.txt
```

## 2. PostgreSQL

```bash
sudo -u postgres psql -c "CREATE USER parking WITH PASSWORD 'YOUR_SECURE_PASSWORD';"
sudo -u postgres psql -c "CREATE DATABASE parking OWNER parking;"
sudo -u postgres psql -d parking < /opt/parking/db/schema.sql
```

Verify:
```bash
psql -U parking -d parking -c "SELECT COUNT(*) FROM occupancy_events;"
```

## 3. Redis

```bash
# Copy config (edit password first!)
sudo cp /opt/parking/config/redis.conf /etc/redis/redis.conf
# Edit /etc/redis/redis.conf: change CHANGEME_REDIS_PASSWORD
sudo systemctl restart redis-server
```

Key settings in `config/redis.conf`:
- `requirepass` -- Set a strong password
- `maxmemory 4gb` -- Hard memory limit
- `maxmemory-policy noeviction` -- Never silently drop data
- `appendonly yes` -- AOF persistence for durability

Verify:
```bash
redis-cli -a YOUR_REDIS_PASSWORD ping
```

## 4. Environment Configuration

```bash
sudo cp /opt/parking/.env.example /opt/parking/.env.production
sudo chmod 600 /opt/parking/.env.production
sudo chown parking:parking /opt/parking/.env.production
```

Edit `/opt/parking/.env.production`:
```bash
# Required
DATABASE_URL=postgresql://parking:YOUR_DB_PASSWORD@localhost/parking
REDIS_URL=redis://:YOUR_REDIS_PASSWORD@localhost:6379
MQTT_BROKER=YOUR_CHIRPSTACK_IP
MQTT_PORT=1883
CHIRPSTACK_API_TOKEN=YOUR_TOKEN
CHIRPSTACK_APP_ID=YOUR_APP_UUID
OPENAI_API_KEY=sk-...

# Optional
ENABLE_MQTT=1
ENABLE_CAMERA_CONTROL=true
SNAPSHOTS_DIR=/data/snapshots
```

## 5. Systemd Services

### Deploy service files

```bash
sudo cp /opt/parking/config/systemd/*.service /etc/systemd/system/
sudo systemctl daemon-reload
```

### Start all services

```bash
# API server (6 gunicorn workers)
sudo systemctl enable --now parking-api.service

# MQTT workers (4 instances)
for i in 1 2 3 4; do
    sudo systemctl enable --now "parking-mqtt-worker@${i}.service"
done

# Camera workers (one per camera, e.g., CAM_01, CAM_02)
sudo systemctl enable --now parking-camera-worker@CAM_01.service

# Inference workers (6 instances)
for i in 1 2 3 4 5 6; do
    sudo systemctl enable --now "parking-inference-worker@${i}.service"
done
```

### Verify

```bash
systemctl status parking-api
systemctl status parking-mqtt-worker@1
systemctl status parking-camera-worker@CAM_01
systemctl status parking-inference-worker@1
```

## 6. Nginx Reverse Proxy

```bash
cat > /etc/nginx/sites-available/parking << 'EOF'
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
EOF

sudo ln -sf /etc/nginx/sites-available/parking /etc/nginx/sites-enabled/parking
sudo rm -f /etc/nginx/sites-enabled/default
sudo nginx -t && sudo systemctl reload nginx
sudo systemctl enable nginx
```

## 7. Cron Jobs

```bash
# Daily snapshot cleanup (>90 days)
cat > /etc/cron.daily/parking-snapshot-cleanup << 'EOF'
#!/bin/bash
find /data/snapshots/ -type f -name "*.jpg" -mtime +90 -delete
find /data/snapshots/ -type d -empty -delete
EOF
chmod +x /etc/cron.daily/parking-snapshot-cleanup

# Daily PostgreSQL backup (30-day retention)
cat > /etc/cron.daily/parking-pg-backup << 'EOF'
#!/bin/bash
BACKUP_DIR=/data/backups
mkdir -p "$BACKUP_DIR"
pg_dump -U parking parking | gzip > "$BACKUP_DIR/pg_$(date +%F).sql.gz"
find "$BACKUP_DIR" -name "pg_*.sql.gz" -mtime +30 -delete
EOF
chmod +x /etc/cron.daily/parking-pg-backup
```

## 8. Firewall

```bash
sudo ufw allow 80/tcp    # HTTP
sudo ufw allow 443/tcp   # HTTPS (if adding TLS later)
sudo ufw enable
```

Redis (6379), PostgreSQL (5432), and the API (8000) are bound to localhost only.

## 9. Health Check

```bash
curl http://localhost/health
```

Expected:
```json
{
  "redis": "ok",
  "postgres": "ok",
  "mqtt": "ok",
  "stream_mqtt": 0,
  "stream_inference": 0,
  "stream_deadletter": 0
}
```

## 10. Monitoring

### Logs

```bash
# API server
journalctl -u parking-api -f

# Workers
journalctl -u parking-mqtt-worker@1 -f
journalctl -u parking-camera-worker@CAM_01 -f
journalctl -u parking-inference-worker@1 -f
```

### Redis queues

```bash
redis-cli -a PASSWORD XLEN parking:mqtt:events
redis-cli -a PASSWORD XLEN parking:inference:jobs
redis-cli -a PASSWORD XLEN parking:inference:deadletter
```

### Database

```bash
psql -U parking -d parking -c "SELECT COUNT(*) FROM occupancy_events WHERE ts > NOW() - INTERVAL '1 hour';"
psql -U parking -d parking -c "SELECT status, COUNT(*) FROM challan_events GROUP BY status;"
```

## Troubleshooting

| Symptom | Check | Fix |
|---------|-------|-----|
| API 503 | `curl localhost/health` | Restart redis/postgres, check `.env.production` |
| No MQTT events | `journalctl -u parking-api \| grep MQTT` | Verify `MQTT_BROKER` IP, check ChirpStack is running |
| Camera timeouts | `journalctl -u parking-camera-worker@CAM_01` | Verify camera IP in `cameras.yaml`, test RTSP manually |
| OCR failures | `redis-cli XLEN parking:inference:deadletter` | Check `OPENAI_API_KEY`, review dead-letter messages |
| Slots stuck | `redis-cli HGETALL parking:slot:state` | Check mqtt-worker logs, verify CAS script |
