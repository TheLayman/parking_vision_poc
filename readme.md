# Unauthorized Parking Detection

Real-time parking enforcement system using LoRaWAN sensors, PTZ cameras, and AI-powered license plate recognition.

## Architecture

```
LoRa Sensors -> ChirpStack MQTT -> Redis Streams -> Workers -> PostgreSQL -> Dashboard
```

**Components:**
- **API Server** -- FastAPI with SSE real-time updates
- **MQTT Workers** (4x) -- Process sensor uplinks, manage slot state
- **Camera Workers** (1 per camera) -- PTZ control + RTSP frame capture
- **Inference Workers** (6x) -- OpenAI Vision OCR + challan decision logic

**Scale:** 3,000 sensors, 30-40 cameras, single server (64GB RAM, 24 cores)

## Quick Start (Local Development)

```bash
# Prerequisites: Redis, PostgreSQL, Python 3.11+
brew install redis postgresql@16
brew services start redis && brew services start postgresql@16

# Setup
createdb parking && psql parking < db/schema.sql
cp .env.example .env  # Edit with your settings
pip install -r requirements.txt

# Seed test data (3000 slots, 48h history)
python3 scripts/emulate.py --seed-only

# Run
ENABLE_MQTT=0 DATABASE_URL=postgresql://localhost/parking \
  python3 -m uvicorn webapp.server:app --reload --port 8000

# Live simulation (separate terminal)
DATABASE_URL=postgresql://localhost/parking python3 scripts/emulate.py --live-only
```

Open http://localhost:8000

## Documentation

| Document | Description |
|----------|-------------|
| [Deployment Guide](docs/deployment.md) | Production setup on Ubuntu, systemd services, nginx |
| [ChirpStack Integration](docs/chirpstack-integration.md) | MQTT setup, device registration, downlink commands |
| [Configuration Reference](docs/configuration.md) | All environment variables, camera config, slot metadata |

## Testing

```bash
python3 -m pytest tests/ -v  # 23 tests (20 pass, 3 need live Postgres)
```

## License

Proprietary -- Internal use only.
