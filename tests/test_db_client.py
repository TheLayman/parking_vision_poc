"""Integration tests for db/client.py — 3 tests.

Requires a real Postgres database.  Set TEST_DATABASE_URL or the tests are
skipped automatically.  A minimal schema is bootstrapped by the pg_conn fixture
in conftest.py.

Run:
    TEST_DATABASE_URL=postgresql://parking:parking@localhost/parking_test \
        pytest tests/test_db_client.py -v
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone, timedelta

import pytest

from db.client import (
    insert_occupancy_event,
    insert_challan_event,
    query_occupancy_events,
)

pytestmark = pytest.mark.integration


# ── Tests ─────────────────────────────────────────────────────────────────────

class TestInsertOccupancyEvent:

    def test_14_happy_path_row_persisted(self, pg_conn):
        """insert_occupancy_event() inserts one row with correct field values."""
        ts = datetime.now(timezone.utc)
        slot_id = 9001  # use a high slot_id to avoid conflicts with real data

        insert_occupancy_event(
            slot_id=slot_id,
            event_type="OCCUPIED",
            device_eui="aabbccdd11223344",
            ts=ts,
            payload={"slot_name": "TEST_A1", "zone": "A"},
            conn=pg_conn,
        )
        pg_conn.commit()

        rows = list(
            pg_conn.execute(
                "SELECT slot_id, event_type, device_eui FROM occupancy_events "
                "WHERE slot_id = %s ORDER BY ts DESC LIMIT 1",
                (slot_id,),
            )
        )
        assert rows, "row must exist after insert"
        row = rows[0]
        assert row["slot_id"] == slot_id
        assert row["event_type"] == "OCCUPIED"
        assert row["device_eui"] == "aabbccdd11223344"

        # Cleanup
        pg_conn.execute("DELETE FROM occupancy_events WHERE slot_id = %s", (slot_id,))
        pg_conn.commit()


class TestInsertChallanEvent:

    def test_15_duplicate_challan_id_raises(self, pg_conn):
        """insert_challan_event() raises on duplicate challan_id (UNIQUE constraint)."""
        import psycopg

        challan_id = f"test-dup-{uuid.uuid4()}"
        slot_id = 9002

        insert_challan_event(
            challan_id=challan_id,
            slot_id=slot_id,
            license_plate="MH01AB1234",
            confidence=0.95,
            status="confirmed",
            conn=pg_conn,
        )
        pg_conn.commit()

        with pytest.raises(psycopg.errors.UniqueViolation):
            insert_challan_event(
                challan_id=challan_id,  # same challan_id
                slot_id=slot_id,
                license_plate="MH01AB1234",
                confidence=0.95,
                status="confirmed",
                conn=pg_conn,
            )
        pg_conn.rollback()

        # Original row must still be intact
        rows = list(
            pg_conn.execute(
                "SELECT challan_id FROM challan_events WHERE challan_id = %s",
                (challan_id,),
            )
        )
        assert len(rows) == 1, "first row must survive the duplicate attempt"

        # Cleanup
        pg_conn.execute("DELETE FROM challan_events WHERE challan_id = %s", (challan_id,))
        pg_conn.commit()


class TestQueryOccupancyEvents:

    def test_16_cutoff_filters_old_events(self, pg_conn):
        """query_occupancy_events(cutoff=…) returns only events after the cutoff."""
        slot_id = 9003
        now = datetime.now(timezone.utc)
        before = now - timedelta(hours=2)
        after = now - timedelta(minutes=1)

        # Insert 1 old event (before cutoff) and 2 recent events (after cutoff)
        for ts, etype in [
            (before, "OCCUPIED"),
            (after, "FREE"),
            (now, "OCCUPIED"),
        ]:
            insert_occupancy_event(
                slot_id=slot_id,
                event_type=etype,
                ts=ts,
                conn=pg_conn,
            )
        pg_conn.commit()

        cutoff = now - timedelta(hours=1)
        rows = query_occupancy_events(cutoff=cutoff, slot_id=slot_id)

        assert len(rows) == 2, "only events after the cutoff must be returned"
        assert all(r["ts"] >= cutoff for r in rows)

        # Cleanup
        pg_conn.execute("DELETE FROM occupancy_events WHERE slot_id = %s", (slot_id,))
        pg_conn.commit()
