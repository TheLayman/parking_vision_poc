# POC Deployment Guide

## Architecture

Single device runs all services. 150 LoRaWAN parking sensors, 1 gateway, ~50m radius.

```
┌─────────────────────────────────────────────┐
│  POC Device                                 │
│                                             │
│  ChirpStack ─MQTT─> API Server (port 8000)  │
│  (port 8080)        ├── MQTT Workers (2x)   │
│                     ├── Redis (port 6379)    │
│  Mosquitto          └── PostgreSQL (5432)    │
│  (port 1883)                                │
│                     Nginx (port 80)          │
└─────────────────────────────────────────────┘
```

## Prerequisites

- Ubuntu 24.04 LTS (or similar Debian-based)
- Python 3.11+
- Network access from LoRa gateway

## 1. System Setup

```bash
sudo apt-get update && sudo apt-get install -y \
    postgresql-16 redis-server nginx \
    python3.11 python3.11-venv python3-pip \
    libpq-dev cron
```

## 2. Application Code

```bash
sudo useradd --system --home /opt/parking --shell /bin/false parking
sudo mkdir -p /opt/parking
sudo cp -r . /opt/parking/
cd /opt/parking
sudo -u parking python3.11 -m venv venv
sudo -u parking venv/bin/pip install -r requirements.txt
```

## 3. PostgreSQL

```bash
sudo -u postgres psql -c "CREATE USER parking WITH PASSWORD 'YOUR_PASSWORD';"
sudo -u postgres psql -c "CREATE DATABASE parking OWNER parking;"
sudo -u postgres psql -d parking < /opt/parking/db/schema.sql
```

## 4. Redis

```bash
sudo cp /opt/parking/config/redis.conf /etc/redis/redis.conf
# Edit: change CHANGEME_REDIS_PASSWORD
sudo systemctl restart redis-server
```

Key settings: `maxmemory 4gb`, `maxmemory-policy noeviction`, `appendonly yes`.

## 5. Environment

```bash
sudo tee /opt/parking/.env.production << 'EOF'
DATABASE_URL=postgresql://parking:YOUR_DB_PASSWORD@localhost/parking
REDIS_URL=redis://:YOUR_REDIS_PASSWORD@localhost:6379
ENABLE_MQTT=1
MQTT_BROKER=localhost
MQTT_PORT=1883
MQTT_TOPIC=application/+/device/+/event/up
EOF
sudo chmod 600 /opt/parking/.env.production
sudo chown parking:parking /opt/parking/.env.production
```

## 6. Systemd Services

```bash
sudo cp /opt/parking/config/systemd/parking-api.service /etc/systemd/system/
sudo cp /opt/parking/config/systemd/parking-mqtt-worker@.service /etc/systemd/system/
sudo systemctl daemon-reload

# API server
sudo systemctl enable --now parking-api.service

# MQTT workers (2 is enough for 150 sensors)
sudo systemctl enable --now parking-mqtt-worker@1.service
sudo systemctl enable --now parking-mqtt-worker@2.service
```

## 7. Nginx

```bash
cat | sudo tee /etc/nginx/sites-available/parking << 'EOF'
server {
    listen 80;
    server_name _;

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_buffering off;
        proxy_read_timeout 3600s;
    }
}
EOF

sudo ln -sf /etc/nginx/sites-available/parking /etc/nginx/sites-enabled/parking
sudo rm -f /etc/nginx/sites-enabled/default
sudo nginx -t && sudo systemctl reload nginx
```

## 8. Seed Test Data (Optional)

```bash
cd /opt/parking
sudo -u parking DATABASE_URL=postgresql://parking:YOUR_DB_PASSWORD@localhost/parking \
    venv/bin/python3 scripts/emulate.py --seed-only
```

## 9. Verify

```bash
curl http://localhost/health
# Should return: {"redis":"ok","postgres":"ok","mqtt":"ok",...}
```

Open `http://<device-ip>` in a browser.

## 10. Cron Jobs

```bash
# Daily PostgreSQL backup
cat | sudo tee /etc/cron.daily/parking-pg-backup << 'EOF'
#!/bin/bash
pg_dump -U parking parking | gzip > /opt/parking/backups/pg_$(date +%F).sql.gz
find /opt/parking/backups -name "pg_*.sql.gz" -mtime +30 -delete
EOF
sudo chmod +x /etc/cron.daily/parking-pg-backup
sudo mkdir -p /opt/parking/backups
```

## Monitoring

```bash
# Service status
systemctl status parking-api parking-mqtt-worker@1 parking-mqtt-worker@2

# Logs
journalctl -u parking-api -f
journalctl -u parking-mqtt-worker@1 -f

# Redis queue depth (should be near 0)
redis-cli XLEN parking:mqtt:events

# Sensor health
redis-cli HGETALL parking:sensor:lastseen

# Recent events
psql -U parking -d parking -c "SELECT COUNT(*) FROM occupancy_events WHERE ts > NOW() - INTERVAL '1 hour';"
```

## Troubleshooting

| Symptom | Check | Fix |
|---------|-------|-----|
| API 503 | `curl localhost/health` | Restart redis/postgres, check `.env.production` |
| No MQTT events | `mosquitto_sub -h localhost -t '#' -v` | Verify ChirpStack is running, gateway is online |
| Slots stuck | `redis-cli HGETALL parking:slot:state` | Check mqtt-worker logs: `journalctl -u parking-mqtt-worker@1` |
| Sensors offline | `redis-cli HGETALL parking:sensor:lastseen` | Check gateway connectivity, sensor battery |
