from pathlib import Path

import pytest
from bson import json_util
from fastapi.testclient import TestClient

import app.main as main


@pytest.fixture
def api_client():
    main.db.attendance_logs.delete_many({})
    main.db.employees.delete_many({})
    main.startup()
    with TestClient(main.app) as client:
        yield client


def _seed_samples():
    base = Path(__file__).resolve().parents[1] / "sample_data"
    for name in ("employees", "attendance_logs"):
        docs = json_util.loads(
            (base / f"{name}.json").read_text(),
            json_options=json_util.JSONOptions(tz_aware=True),
        )
        main.db[name].delete_many({})
        main.db[name].insert_many(docs)


def test_health_and_basic_attendance_flow(api_client):
    r = api_client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}

    payload = {
        "emp_code": "EMP0001",
        "name": "Asha Rao",
        "email": "asha@example.com",
        "department": "Engineering",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-01-05",
    }
    r = api_client.post("/employees", json=payload)
    assert r.status_code == 201
    assert r.json()["emp_code"] == "EMP0001"

    punch_in_ts = 1783312500000
    r = api_client.post(
        "/attendance/punch-in",
        json={"emp_code": "EMP0001", "punched_at": punch_in_ts, "status": "PRESENT"},
    )
    assert r.status_code == 201
    body = r.json()
    assert body["date"] == "2026-07-06"
    assert body["late_minutes"] == 35

    punch_out_ts = 1783343100000
    r = api_client.post(
        "/attendance/punch-out",
        json={"emp_code": "EMP0001", "punched_at": punch_out_ts},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["work_hours"] == 8.5
    assert body["overtime_minutes"] == 0
    assert body["half_day"] is False

    r = api_client.get("/attendance")
    assert r.status_code == 200
    assert r.json()["total"] == 1

    r = api_client.get("/analytics/employees/EMP0001/monthly", params={"month": "2026-07"})
    assert r.status_code == 200
    body = r.json()
    assert body["emp_code"] == "EMP0001"
    assert body["late_count"] >= 1
    assert body["total_late_minutes"] >= 35


def test_seeded_sample_contract_endpoints(api_client):
    _seed_samples()

    r = api_client.get("/employees")
    assert r.status_code == 200
    assert len(r.json()["items"]) >= 1

    r = api_client.get("/analytics/departments/summary", params={"month": "2026-07"})
    assert r.status_code == 200
    summary = r.json()
    assert "items" in summary
    assert len(summary["items"]) >= 1

    r = api_client.get("/analytics/leaderboard/late", params={"month": "2026-07", "limit": 10})
    assert r.status_code == 200
    board = r.json()
    assert "items" in board
    assert board["month"] == "2026-07"

    r = api_client.get(
        "/analytics/departments/Engineering/trend",
        params={"from": "2026-07-01", "to": "2026-07-07"},
    )
    assert r.status_code == 200
    trend = r.json()
    assert trend["department"] == "Engineering"
    assert len(trend["items"]) == 7


def test_duplicate_punch_in_is_rejected(api_client):
    payload = {
        "emp_code": "EMP0001",
        "name": "Asha Rao",
        "email": "asha@example.com",
        "department": "Engineering",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-01-05",
    }
    api_client.post("/employees", json=payload)

    punch_in_ts = 1783312500000
    r = api_client.post(
        "/attendance/punch-in",
        json={"emp_code": "EMP0001", "punched_at": punch_in_ts, "status": "PRESENT"},
    )
    assert r.status_code == 201

    r = api_client.post(
        "/attendance/punch-in",
        json={"emp_code": "EMP0001", "punched_at": punch_in_ts, "status": "PRESENT"},
    )
    assert r.status_code == 409
    assert r.json()["detail"] == "already punched in for this date"
