# Production Deployment Guide

## Server Architecture

The system runs on two physical servers for resilience:

| | Application Server | Management Server |
|---|---|---|
| **Role** | Processing (API, workers, Redis, PostgreSQL) | Ingestion (ChirpStack, MQTT broker), backups, monitoring |
| **CPU** | Dual Intel Xeon Gold (48 cores) | Single Intel Xeon Gold (24 cores) |
| **RAM** | 128GB (64GB × 2, DDR4/5 5600MT/s) | 128GB (64GB × 2, DDR4/5 5600MT/s) |
| **OS Disk** | 960GB NVMe SSD × 2 (RAID 1) | 960GB NVMe SSD × 2 (RAID 1) |
| **Data Disk** | 10TB HDD × 2 (RAID 1) | 20TB HDD × 2 (RAID 1) |
| **Network** | 1Gb × 4 port + 10Gb × 2 port | 1Gb × 4 port + 10Gb × 2 port |
| **PSU** | Dual hot-plug platinum (redundant) | Dual hot-plug platinum (redundant) |
| **OS** | Ubuntu 24.04 LTS | Ubuntu 24.04 LTS |

### Why Two Servers?

ChirpStack (LoRa network server) and the MQTT broker run on the **management server**,
separate from the application. This means:

- **If the application server crashes:** ChirpStack stays up, LoRa gateways continue
  delivering sensor events, and MQTT queues messages. When the app server recovers,
  workers reconnect and process the backlog. No events are lost.
- **If the management server crashes:** The application loses its event source but
  remains operational for dashboard/API queries. LoRa gateways with store-and-forward
  buffer packets locally and replay them when ChirpStack returns. No events are lost.
- **Both down simultaneously:** LoRa gateways buffer packets via store-and-forward
  and replay the full backlog once ChirpStack is reachable again. No events are lost
  as long as the gateway buffer is not exhausted (typically hours to days of capacity).

> **Zero event loss guarantee:** The LoRa gateways act as the final safety net.
> Enterprise gateways (Kerlink, Multitech, RAK) support store-and-forward — when
> the network server (ChirpStack) is unreachable, the gateway stores all received
> packets in local flash storage and replays them in order once connectivity is
> restored. This means no server failure combination causes permanent event loss.
> Ensure store-and-forward is enabled in your gateway configuration.

### Network Topology

```
LoRa Sensors (3000)
    | (radio)
LoRa Gateways
    | (UDP/TCP, to management server IP)
Management Server
    ├── ChirpStack Network Server
    ├── MQTT Broker (Mosquitto, port 1883)
    └── ChirpStack PostgreSQL (ChirpStack's own DB)
         | (MQTT over network, to app server IP)
Application Server
    ├── API Server (FastAPI/Gunicorn, port 8000)
    ├── Redis (port 6379, localhost)
    ├── PostgreSQL (port 5432, localhost)
    ├── MQTT Workers (4x)
    ├── Camera Workers (1 per camera)
    ├── Inference Workers (6x)
    └── Nginx (port 80, reverse proxy)
```

## Prerequisites

### Application Server
- Ubuntu 24.04 LTS
- Root/sudo access
- Network access to management server (MQTT port 1883, gRPC port 8080)
- OpenAI API key (for license plate recognition)

### Management Server
- Ubuntu 24.04 LTS
- Root/sudo access
- ChirpStack v4 installed and configured
- MQTT broker (ChirpStack built-in or Mosquitto)
- Network access from LoRa gateways

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
MQTT_BROKER=MANAGEMENT_SERVER_IP    # Points to management server, NOT localhost
MQTT_PORT=1883
CHIRPSTACK_HOST=MANAGEMENT_SERVER_IP  # Points to management server
CHIRPSTACK_GRPC_PORT=8080
CHIRPSTACK_API_TOKEN=YOUR_TOKEN
CHIRPSTACK_APP_ID=YOUR_APP_UUID
OPENAI_API_KEY=sk-...

# Optional
ENABLE_MQTT=1
ENABLE_CAMERA_CONTROL=true
SNAPSHOTS_DIR=/data/snapshots
```

> **Note:** `MQTT_BROKER` and `CHIRPSTACK_HOST` point to the management server IP,
> not localhost. ChirpStack and the MQTT broker run on the management server for
> resilience — see [Server Architecture](#server-architecture).

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

# Nightly image snapshot sync to management server (rsync over 10Gb link)
cat > /etc/cron.daily/parking-snapshot-sync << 'EOF'
#!/bin/bash
# Syncs camera snapshots to management server for backup.
# Uses 10Gb link for fast transfer. Management server has 20TB RAID 1 storage.
# Only syncs new/changed files (incremental). Deletes from remote when local
# cleanup removes files older than 90 days.
rsync -az --delete \
    /data/snapshots/ \
    parking@MANAGEMENT_SERVER_IP:/data/snapshots-mirror/
EOF
chmod +x /etc/cron.daily/parking-snapshot-sync

# Daily PostgreSQL backup sync to management server
cat > /etc/cron.daily/parking-backup-sync << 'EOF'
#!/bin/bash
rsync -az /data/backups/ parking@MANAGEMENT_SERVER_IP:/data/backups-mirror/
EOF
chmod +x /etc/cron.daily/parking-backup-sync
```

> **Setup:** Configure passwordless SSH from the application server to the management
> server for the `parking` user: `ssh-keygen && ssh-copy-id parking@MANAGEMENT_SERVER_IP`.
> Create `/data/snapshots-mirror` and `/data/backups-mirror` on the management server.

## 8. Firewall

### Application Server

```bash
sudo ufw allow 80/tcp    # HTTP (Nginx)
sudo ufw allow 443/tcp   # HTTPS (if adding TLS later)
sudo ufw enable
```

Redis (6379), PostgreSQL (5432), and the API (8000) are bound to localhost only.

### Management Server

```bash
sudo ufw allow 1883/tcp                        # MQTT broker (from app server + ChirpStack)
sudo ufw allow 8080/tcp                        # ChirpStack gRPC API
sudo ufw allow from APP_SERVER_IP to any port 22  # SSH for rsync backups
sudo ufw enable
```

Restrict MQTT and gRPC ports to the application server IP if possible.

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

## Failure Scenarios & Recovery

| Failure | Impact | Recovery | Data Loss? |
|---------|--------|----------|------------|
| **App server crash** | Processing stops; dashboard down | systemd auto-restarts services in ~30-60s. Workers XAUTOCLAIM in-flight messages. | No — ChirpStack (on mgmt server) queues MQTT messages until workers reconnect |
| **Mgmt server crash** | No new sensor events ingested | App server stays operational for queries. LoRa gateways buffer packets via store-and-forward and replay on reconnect. Manual restart needed. | No — gateways hold all events until ChirpStack is back |
| **Both servers down** | Full outage | LoRa gateways buffer via store-and-forward. Boot mgmt server first (ChirpStack), then app server. Gateways replay buffered packets automatically. | No — gateways replay all buffered events on reconnect |
| **App server HDD failure (1 of 2)** | RAID 1 degrades, keeps running | Hot-swap failed drive, RAID rebuilds (~10-24hrs) | No |
| **App server both HDDs fail** | Snapshots lost on app server | Restore from mgmt server mirror (`/data/snapshots-mirror/`) | Only images since last nightly rsync |
| **Redis crash** | Workers stall, live state lost | Auto-restart + AOF replay (~30-60s). Max 1 sec data loss (appendfsync everysec). Slot state reconstructable from PostgreSQL. | Minimal |
| **PostgreSQL crash** | Events not persisted, API errors | Auto-restart + WAL recovery (~30-60s) | No (WAL ensures crash consistency) |
| **OpenAI API outage** | OCR pipeline stalls | Jobs dead-lettered after 3 retries. Process automatically when API returns. | No — jobs held in Redis stream |
| **Power failure** | Everything down | UPS-dependent. Full cold boot recovery: ~30-60s after power. | Redis: up to 1s. PG: none. |

## Backup Strategy

| What | Where | Schedule | Retention |
|------|-------|----------|-----------|
| PostgreSQL dump | App server `/data/backups/` | Daily | 30 days |
| PostgreSQL dump (mirror) | Mgmt server `/data/backups-mirror/` | Daily (rsync) | 30 days |
| Camera snapshots | App server `/data/snapshots/` | Real-time | 90 days |
| Camera snapshots (mirror) | Mgmt server `/data/snapshots-mirror/` | Daily (rsync) | 90 days |
| Redis AOF | App server (alongside Redis) | Continuous | Current state |

> **RAID is not backup.** RAID 1 protects against disk failure but not accidental
> deletion, corruption, or ransomware. The rsync mirrors on the management server
> provide an independent copy. For full disaster recovery, consider periodic offsite
> backup (cloud storage) of PostgreSQL dumps.

## Troubleshooting

| Symptom | Check | Fix |
|---------|-------|-----|
| API 503 | `curl localhost/health` | Restart redis/postgres, check `.env.production` |
| No MQTT events | `journalctl -u parking-api \| grep MQTT` | Verify `MQTT_BROKER` points to mgmt server IP, check ChirpStack is running on mgmt server |
| Camera timeouts | `journalctl -u parking-camera-worker@CAM_01` | Verify camera IP in `cameras.yaml`, test RTSP manually |
| OCR failures | `redis-cli XLEN parking:inference:deadletter` | Check `OPENAI_API_KEY`, review dead-letter messages |
| Slots stuck | `redis-cli HGETALL parking:slot:state` | Check mqtt-worker logs, verify CAS script |
| Rsync backup failing | `journalctl \| grep rsync` | Check SSH key auth to mgmt server, verify disk space on mgmt server |
| Mgmt server unreachable | `ping MANAGEMENT_SERVER_IP` | Check network, verify mgmt server is running, check firewall rules |
