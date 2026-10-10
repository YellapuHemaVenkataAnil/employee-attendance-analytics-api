# Employee Attendance & Analytics API

A FastAPI + MongoDB REST API implementing an **Employee Attendance & Analytics** system as specified in the SDT candidate assessment.

---

## Problem Statement Summary

Build a production-quality REST API that manages:

- **Employee records** — create and list employees with shift schedules.
- **Daily attendance punch-in / punch-out** — with IST timezone awareness, overnight-shift support, and idempotent concurrency safety.
- **Derived metrics** — late minutes (R2), overtime minutes (R3), work hours (R4), half-day flag (R5).
- **Analytics** — monthly department summaries (R6, R7), punctuality leaderboard (R8), and regularization audit trail.

The full business rules (R1–R10) are documented in `openapi.yaml`. The data model is in `DATA_MODEL.md`.

---

## Tech Stack

| Layer | Technology |
|---|---|
| Runtime | Python 3.11+ |
| Framework | FastAPI 0.110+ |
| Server | Uvicorn (ASGI) |
| Database | MongoDB 6.0+ (tested on 7.0 & Atlas M0) |
| ODM / Driver | PyMongo 4.7+ |
| Validation | Pydantic v2 |
| Config | python-dotenv |

---

## Repository Structure

```
.
├── app/
│   ├── __init__.py
│   └── main.py            # All application code (single-file per spec)
├── tests/
│   └── test_api.py        # Pytest test suite
├── sample_data/
│   ├── employees.json     # Sample employee documents (MongoDB Extended JSON)
│   └── attendance_logs.json
├── sample_seed.py         # Loads sample_data/ into MongoDB
├── openapi.yaml           # Full API contract & business rules (R1–R10)
├── DATA_MODEL.md          # MongoDB collection schemas & sample documents
├── DECISIONS.md           # Design decisions & trade-offs
├── REVIEW.md              # Defect review — 10 bugs found & fixed in starter
├── requirements.txt       # Python dependencies
└── README.md              # This file
```

---

## Local Setup

### 1. Prerequisites

- Python 3.11+
- MongoDB 6.0+ running locally **or** a free [Atlas M0](https://www.mongodb.com/atlas) cluster

### 2. Clone & install

```bash
git clone <repo-url>
cd candidate_kit

python -m venv .venv
# Windows
.venv\Scripts\activate
# macOS / Linux
source .venv/bin/activate

pip install -r requirements.txt
```

### 3. Configure environment

Create a `.env` file in the project root (never committed):

```env
MONGO_URI=mongodb://localhost:27017
MONGO_DB=attendance_db
```

> **Atlas example:** `MONGO_URI=mongodb+srv://<user>:<pass>@cluster0.xxxxx.mongodb.net`

Alternatively, start a local MongoDB container:

```bash
docker run -d -p 27017:27017 --name mongo7 mongo:7
```

### 4. Seed sample data

```bash
python sample_seed.py
```

### 5. Run the API

```bash
uvicorn app.main:app --port 8000 --reload
```

The interactive docs are available at **http://localhost:8000/docs**.

---

## API Endpoints

| Method | Path | Description |
|---|---|---|
| `GET` | `/health` | Liveness + DB connectivity check |
| `POST` | `/employees` | Create a new employee |
| `GET` | `/employees` | List employees (paginated, filterable by department) |
| `POST` | `/attendance/punch-in` | Record punch-in (PRESENT / WFH / ON_DUTY) |
| `POST` | `/attendance/punch-out` | Record punch-out; computes work_hours, overtime_minutes, half_day |
| `GET` | `/attendance` | List attendance logs (paginated, filterable by employee/date/status) |
| `PATCH` | `/attendance/{emp_code}/{date}` | Regularize (manually correct) one day's record with full audit trail |
| `GET` | `/analytics/employees/{emp_code}/monthly` | Monthly attendance summary for one employee |
| `GET` | `/analytics/departments/summary` | Department-level monthly headcount & metrics (cross-collection aggregation) |
| `GET` | `/analytics/leaderboard/late` | Late-comers leaderboard with standard competition ranking |
| `GET` | `/analytics/departments/{department}/trend` | Daily attendance trend with 7-day moving average and gap-filling |
| `GET` | `/admin/explain/{endpoint}` | MongoDB `explain(executionStats)` for an endpoint's main query/pipeline |

Full request/response schemas are in `openapi.yaml`.

---

## Key Design Decisions

See [`DECISIONS.md`](DECISIONS.md) for detailed rationale. Highlights:

1. **Indexes** — Unique compound index on `(emp_code, date)` enforces one record per employee per day; covered index on `(date, late_minutes, emp_code)` powers the leaderboard without a `COLLSCAN`.
2. **Race conditions** — Duplicate punch-in is caught via MongoDB's unique index + `DuplicateKeyError` → HTTP 409, not an application-level check-then-insert.
3. **IST date boundaries** — All attendance dates are IST calendar dates; overnight shifts (where `shift_end ≤ shift_start`) use the shift-start day as the attendance date.
4. **Stored derived values are authoritative** — `work_hours`, `late_minutes`, `overtime_minutes`, and `half_day` are written at punch-in/out time and read directly by analytics (not recomputed).
5. **Scalability** — For 100× data: incremental rollups via MongoDB change streams or `$merge` at punch-out, and sharding `attendance_logs` on `(date, emp_code)`.

---

## Defects Found & Fixed

See [`REVIEW.md`](REVIEW.md) for the full defect table. Twelve bugs were identified and corrected:

| # | Area | Issue |
|---|---|---|
| 1 | `compute_late_minutes` | Off-by-one grace period & missing IST/overnight handling |
| 2 | `compute_work_hours` | Banker's rounding instead of round-half-up (R4) |
| 3 | `compute_overtime` | Missing 30-min threshold; broken overnight shift end |
| 4 | `GET /health` | Always returned 200 even when MongoDB was down |
| 5 | `POST /employees` | Check-then-insert race; missing field validation |
| 6 | `GET /employees` | Off-by-one pagination (`page` was 0-indexed); wrong total count |
| 7 | `POST /attendance/punch-in` | 500 on unknown `emp_code`; wrong IST date; duplicate race |
| 8 | `GET /attendance` | In-memory sort (OOM risk); missing secondary sort; invalid date range accepted |
| 9 | Startup | Zero indexes created; all queries were `COLLSCAN` |
| 10 | Datetime handling | Naive vs UTC confusion across punch-in/out pipeline |
| 11 | `serialize_instant` | `int(ts)*1000` dropped sub-second precision; fixed to `int(ts*1000)` |
| 12 | `build_trend_pipeline` | `$documents` inside `$unionWith.pipeline` is invalid in MongoDB 6.0 → HTTP 500 |

---

## Running Tests

```bash
pytest tests/ -v
```

Tests require a live MongoDB instance configured via `.env`.

---

## Submission Checklist

- [x] `app/main.py` — all application logic in one file
- [x] `requirements.txt` — pinned dependency versions
- [x] `REVIEW.md` — 10 defects documented with location, symptom, and fix
- [x] `DECISIONS.md` — 5 design decisions explained
- [x] `README.md` — setup, structure, and context (this file)
- [x] No `.env`, secrets, or Dockerfile committed
- [x] All indexes created at startup (idempotent)
- [x] `MONGO_URI` and `MONGO_DB` read from environment variables
