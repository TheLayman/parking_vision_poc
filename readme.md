# Smart Parking Dashboard

Real-time parking occupancy dashboard using LoRaWAN BMM350 magnetometer sensors.

## Architecture

```
LoRa Sensors (150) -> Gateway -> ChirpStack MQTT -> Redis Streams -> MQTT Workers -> PostgreSQL -> Dashboard
```

**Components:**
- **API Server** -- FastAPI with SSE real-time updates
- **MQTT Workers** (2x) -- Process sensor uplinks, manage slot state, track sensor health

**POC Scale:** 150 sensors, 1 gateway, single device

## Quick Start (Local Development)

```bash
# Prerequisites: Redis, PostgreSQL, Python 3.11+
brew install redis postgresql@16
brew services start redis && brew services start postgresql@16

# Setup
createdb parking && psql parking < db/schema.sql
pip install -r requirements.txt

# Seed test data
python3 scripts/emulate.py --seed-only

# Run
ENABLE_MQTT=0 DATABASE_URL=postgresql://localhost/parking \
  python3 -m uvicorn webapp.server:app --reload --port 8000
```

Open http://localhost:8000

## Documentation

| Document | Description |
|----------|-------------|
| [Deployment Guide](docs/deployment.md) | POC setup, systemd services, nginx |
| [ChirpStack Integration](docs/chirpstack-integration.md) | MQTT setup, device registration, sensor reference |
| [Configuration Reference](docs/configuration.md) | Environment variables, Redis keys, API endpoints |

## Testing

```bash
python3 -m pytest tests/ -v  # 8 unit tests + 2 integration (need live Postgres)
```

## License

Proprietary -- Internal use only.
