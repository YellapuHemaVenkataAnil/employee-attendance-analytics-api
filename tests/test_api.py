"""Comprehensive automated test suite for Employee Attendance & Analytics API."""
import os
from datetime import datetime, timezone
import pytest
from fastapi.testclient import TestClient

os.environ["MONGO_URI"] = os.getenv("MONGO_URI", "mongodb://localhost:27017")
os.environ["MONGO_DB"] = "test_attendance_db"

from app.main import app, db, init_db, IST

test_client = TestClient(app)

@pytest.fixture(autouse=True, scope="module")
def setup_teardown():
    db.employees.delete_many({})
    db.attendance_logs.delete_many({})
    init_db()
    yield
    db.employees.delete_many({})
    db.attendance_logs.delete_many({})

def test_health():
    res = test_client.get("/health")
    assert res.status_code == 200
    assert res.json() == {"status": "ok"}

def test_create_and_list_employees():
    emp1 = {
        "emp_code": "EMP0001",
        "name": "Asha Rao",
        "email": "asha@example.com",
        "department": "Engineering",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-01-05"
    }
    res = test_client.post("/employees", json=emp1)
    assert res.status_code == 201
    data = res.json()
    assert data["emp_code"] == "EMP0001"
    assert isinstance(data["created_at"], int)
    assert "_id" not in data

    # 409 on duplicate emp_code
    res_dup = test_client.post("/employees", json=emp1)
    assert res_dup.status_code == 409

    # 422 on invalid shift
    invalid_shift = dict(emp1)
    invalid_shift["emp_code"] = "EMP0099"
    invalid_shift["shift_end"] = "09:30"
    assert test_client.post("/employees", json=invalid_shift).status_code == 422

    # Mid-month joiner EMP0002
    emp2 = {
        "emp_code": "EMP0002",
        "name": "Bikram Sen",
        "email": "bikram@example.com",
        "department": "Engineering",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-07-13"
    }
    assert test_client.post("/employees", json=emp2).status_code == 201

    # EMP0003 in Sales
    emp3 = {
        "emp_code": "EMP0003",
        "name": "Neha Iyer",
        "email": "neha@example.com",
        "department": "Sales",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-02-01"
    }
    assert test_client.post("/employees", json=emp3).status_code == 201

    # List employees with department filter
    res_list = test_client.get("/employees?department=Engineering")
    assert res_list.status_code == 200
    res_json = res_list.json()
    assert res_json["total"] == 2
    assert len(res_json["items"]) == 2
    assert res_json["items"][0]["emp_code"] == "EMP0001"
    assert res_json["items"][1]["emp_code"] == "EMP0002"

def test_punch_in_and_race():
    dt_in = datetime(2026, 7, 6, 9, 28, 0, tzinfo=IST)
    ts_in = int(dt_in.timestamp()) * 1000

    req = {"emp_code": "EMP0001", "punched_at": ts_in, "status": "PRESENT"}
    res = test_client.post("/attendance/punch-in", json=req)
    assert res.status_code == 201
    data = res.json()
    assert data["date"] == "2026-07-06"
    assert data["late_minutes"] == 0
    assert data["punch_out"] is None
    assert data["work_hours"] is None
    assert "id" not in data

    # Duplicate punch-in for same date yields 409
    res_dup = test_client.post("/attendance/punch-in", json=req)
    assert res_dup.status_code == 409

    # Late punch-in: 10:05 IST -> 35 min late
    dt_late = datetime(2026, 7, 6, 10, 5, 0, tzinfo=IST)
    ts_late = int(dt_late.timestamp()) * 1000
    res_late = test_client.post("/attendance/punch-in", json={"emp_code": "EMP0003", "punched_at": ts_late})
    assert res_late.status_code == 201
    assert res_late.json()["late_minutes"] == 35

    # Grace period exact boundary test: 09:40:00 -> 0; 09:40:01 -> 10
    dt_grace_ok = datetime(2026, 7, 14, 9, 40, 0, tzinfo=IST)
    ts_grace_ok = int(dt_grace_ok.timestamp()) * 1000
    res_grace = test_client.post("/attendance/punch-in", json={"emp_code": "EMP0002", "punched_at": ts_grace_ok})
    assert res_grace.status_code == 201
    assert res_grace.json()["late_minutes"] == 0

def test_punch_out_and_race():
    # EMP0001 punches out at 18:35 IST (9.12 hours, 5 min OT -> 0 overtime)
    dt_out = datetime(2026, 7, 6, 18, 35, 0, tzinfo=IST)
    ts_out = int(dt_out.timestamp()) * 1000

    res = test_client.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": ts_out})
    assert res.status_code == 200
    data = res.json()
    assert data["work_hours"] == 9.12
    assert data["overtime_minutes"] == 0
    assert data["half_day"] is False
    assert data["punch_out"] == ts_out

    # Duplicate punch-out yields 409
    res_dup = test_client.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": ts_out})
    assert res_dup.status_code == 409

    # EMP0003 punches out at 19:10 IST (40 min OT >= 30 -> 40 min overtime)
    dt_out3 = datetime(2026, 7, 6, 19, 10, 0, tzinfo=IST)
    ts_out3 = int(dt_out3.timestamp()) * 1000
    res3 = test_client.post("/attendance/punch-out", json={"emp_code": "EMP0003", "punched_at": ts_out3})
    assert res3.status_code == 200
    assert res3.json()["overtime_minutes"] == 40
    assert res3.json()["work_hours"] == 9.08

def test_overnight_shift():
    emp5 = {
        "emp_code": "EMP0005",
        "name": "David Miller",
        "email": "david@example.com",
        "department": "Support",
        "shift_start": "22:00",
        "shift_end": "06:00",
        "joined_on": "2026-03-10"
    }
    assert test_client.post("/employees", json=emp5).status_code == 201

    # Punch in 21:55 IST on 2026-07-06
    dt_night_in = datetime(2026, 7, 6, 21, 55, 0, tzinfo=IST)
    ts_night_in = int(dt_night_in.timestamp()) * 1000
    res_in = test_client.post("/attendance/punch-in", json={"emp_code": "EMP0005", "punched_at": ts_night_in})
    assert res_in.status_code == 201
    assert res_in.json()["date"] == "2026-07-06"

    # Punch out 06:40 IST on 2026-07-07 (next calendar day)
    dt_night_out = datetime(2026, 7, 7, 6, 40, 0, tzinfo=IST)
    ts_night_out = int(dt_night_out.timestamp()) * 1000
    res_out = test_client.post("/attendance/punch-out", json={"emp_code": "EMP0005", "punched_at": ts_night_out})
    assert res_out.status_code == 200
    data = res_out.json()
    assert data["date"] == "2026-07-06"
    assert data["work_hours"] == 8.75
    assert data["overtime_minutes"] == 40

def test_regularize_patch():
    # EMP0001 punches in on 2026-07-07 at 10:05 IST, out at 18:30 IST
    dt_in = datetime(2026, 7, 7, 10, 5, 0, tzinfo=IST)
    test_client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": int(dt_in.timestamp()) * 1000})
    dt_out = datetime(2026, 7, 7, 18, 30, 0, tzinfo=IST)
    test_client.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": int(dt_out.timestamp()) * 1000})

    # HR regularizes punch-in to 09:28 IST
    dt_new_in = datetime(2026, 7, 7, 9, 28, 0, tzinfo=IST)
    patch_req = {
        "punch_in": int(dt_new_in.timestamp()) * 1000,
        "reason": "biometric glitch",
        "regularized_by": "hr.admin"
    }
    res_patch = test_client.patch("/attendance/EMP0001/2026-07-07", json=patch_req)
    assert res_patch.status_code == 200
    data = res_patch.json()
    assert data["late_minutes"] == 0
    assert data["work_hours"] == 9.03
    assert len(data["history"]) == 1
    assert data["history"][0]["by"] == "hr.admin"
    assert data["history"][0]["changes"]["late_minutes"] == {"from": 35, "to": 0}

def test_explain_endpoints():
    endpoints = [
        ("attendance_list", {}),
        ("employee_monthly", {"emp_code": "EMP0001", "month": "2026-07"}),
        ("department_summary", {"month": "2026-07"}),
        ("late_leaderboard", {"month": "2026-07"}),
        ("department_trend", {"department": "Engineering", "from": "2026-07-06", "to": "2026-07-14"})
    ]
    for ep, params in endpoints:
        res = test_client.get(f"/admin/explain/{ep}", params=params)
        assert res.status_code == 200, f"Explain failed on {ep}: {res.text}"
        data = res.json()
        assert data["endpoint"] == ep
        assert "collection" in data
        assert "explain" in data
