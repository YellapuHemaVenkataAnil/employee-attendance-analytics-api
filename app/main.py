"""
Employee Attendance & Analytics API

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (a local .env is loaded for convenience)
"""
import calendar
import os
import re
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from enum import Enum
from typing import Any, Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, Path
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field, field_validator
from pymongo import MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError, OperationFailure

load_dotenv()

MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB = os.getenv("MONGO_DB", "attendance_db")

client = MongoClient(MONGO_URI, tz_aware=True)
db = client[MONGO_DB]

IST = timezone(timedelta(hours=5, minutes=30))


# --------------------------------------------------------------------------- #
# Database Initialization & Idempotent Indexes
# --------------------------------------------------------------------------- #
def init_db():
    """Create all required indexes idempotently at startup."""
    def safe_create_index(coll, keys, **kwargs):
        try:
            coll.create_index(keys, **kwargs)
        except OperationFailure as err:
            if getattr(err, "code", None) == 86:
                for idx in coll.list_indexes():
                    if list(idx["key"].items()) == list(keys):
                        coll.drop_index(idx["name"])
                        break
                coll.create_index(keys, **kwargs)
            else:
                raise

    # employees indexes
    safe_create_index(db.employees, [("emp_code", 1)], unique=True)
    safe_create_index(db.employees, [("department", 1), ("joined_on", 1)])
    safe_create_index(db.employees, [("joined_on", 1)])

    # attendance_logs indexes
    safe_create_index(
        db.attendance_logs, [("emp_code", 1), ("date", 1)], unique=True
    )
    safe_create_index(db.attendance_logs, [("date", -1), ("emp_code", 1)])
    safe_create_index(db.attendance_logs, [("emp_code", 1), ("punch_in", -1)])
    safe_create_index(
        db.attendance_logs, [("date", 1), ("late_minutes", 1), ("emp_code", 1)]
    )
    safe_create_index(
        db.attendance_logs, [("status", 1), ("date", -1), ("emp_code", 1)]
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield


app = FastAPI(
    title="Employee Attendance & Analytics API",
    version="2.0.0",
    lifespan=lifespan
)


# --------------------------------------------------------------------------- #
# Enums & Pydantic Schemas
# --------------------------------------------------------------------------- #
class Status(str, Enum):
    PRESENT = "PRESENT"
    ABSENT = "ABSENT"
    LEAVE = "LEAVE"
    WFH = "WFH"
    ON_DUTY = "ON_DUTY"


class PresenceStatus(str, Enum):
    PRESENT = "PRESENT"
    WFH = "WFH"
    ON_DUTY = "ON_DUTY"


class EmployeeIn(BaseModel):
    emp_code: str = Field(..., pattern=r"^EMP\d{4,6}$")
    name: str = Field(..., min_length=1, max_length=100)
    email: str = Field(
        ..., max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$"
    )
    department: str = Field(..., min_length=1, max_length=50)
    shift_start: str = Field("09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    shift_end: str = Field("18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    joined_on: str = Field(
        ..., pattern=r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$"
    )

    @field_validator("shift_end")
    @classmethod
    def validate_shift_times(cls, v: str, info):
        shift_start = info.data.get("shift_start")
        if shift_start and v == shift_start:
            raise ValueError("shift_start must differ from shift_end")
        return v

    @field_validator("joined_on")
    @classmethod
    def validate_joined_date(cls, v: str):
        try:
            date.fromisoformat(v)
        except ValueError:
            raise ValueError("Invalid calendar date for joined_on")
        return v


class PunchInIn(BaseModel):
    emp_code: str
    punched_at: Optional[int] = Field(None, ge=100000000000, le=4102444800000)
    status: PresenceStatus = PresenceStatus.PRESENT


class PunchOutIn(BaseModel):
    emp_code: str
    punched_at: Optional[int] = Field(None, ge=100000000000, le=4102444800000)


class RegularizeRequest(BaseModel):
    status: Optional[Status] = None
    punch_in: Optional[int] = Field(None, ge=100000000000, le=4102444800000)
    punch_out: Optional[int] = Field(None, ge=100000000000, le=4102444800000)
    reason: str = Field(..., min_length=5, max_length=200)
    regularized_by: str = Field(..., min_length=1, max_length=50)


# --------------------------------------------------------------------------- #
# Helpers & Business Logic (R1 - R8)
# --------------------------------------------------------------------------- #
def round_half_up(
    val: Optional[float | Decimal | int], decimals: int
) -> Optional[float]:
    """R8: Decimal round half-up."""
    if val is None:
        return None
    d = Decimal(str(val))
    target = Decimal("10") ** (-decimals)
    return float(d.quantize(target, rounding=ROUND_HALF_UP))


def serialize_instant(dt_or_ms: Any) -> Optional[int]:
    """Convert BSON datetime or int epoch ms to integer epoch ms."""
    if dt_or_ms is None:
        return None
    if isinstance(dt_or_ms, int):
        return dt_or_ms
    if isinstance(dt_or_ms, datetime):
        if dt_or_ms.tzinfo is None:
            dt_or_ms = dt_or_ms.replace(tzinfo=timezone.utc)
        # Multiply first, then int-truncate to preserve millisecond precision.
        # int(ts) * 1000 is WRONG — it drops any sub-second component of the
        # BSON datetime before multiplying, producing a result that is always
        # a multiple of 1000 ms and thus off by up to 999 ms.
        return int(dt_or_ms.timestamp() * 1000)
    return None


def serialize_history(history: list) -> list:
    """Format history list ensuring epoch milliseconds inside changes."""
    res = []
    for h in history or []:
        entry = {
            "at": serialize_instant(h.get("at")),
            "by": h.get("by"),
            "reason": h.get("reason"),
            "changes": {}
        }
        changes = h.get("changes", {})
        for k, v in changes.items():
            if k in ("punch_in", "punch_out"):
                entry["changes"][k] = {
                    "from": serialize_instant(v.get("from")),
                    "to": serialize_instant(v.get("to"))
                }
            else:
                entry["changes"][k] = {
                    "from": v.get("from"),
                    "to": v.get("to")
                }
        res.append(entry)
    return res


def format_attendance_doc(doc: dict) -> dict:
    """Formats an attendance document strictly to openapi contract."""
    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": serialize_instant(doc.get("punch_in")),
        "punch_out": serialize_instant(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes", 0),
        "overtime_minutes": doc.get("overtime_minutes", 0),
        "half_day": doc.get("half_day", False),
        "history": serialize_history(doc.get("history", []))
    }


def calculate_shift_and_date(
    punch_in_ms: int, shift_start: str, shift_end: str
) -> tuple[str, datetime, datetime]:
    """
    R1: Shift times and attendance dates are IST (UTC+05:30).
    A record's date is the IST calendar date of its punch_in instant.
    Overnight shifts: earlier than shift_end belongs to previous day.
    """
    ts_sec = punch_in_ms // 1000
    dt_utc = datetime.fromtimestamp(ts_sec, tz=timezone.utc)
    dt_ist = dt_utc.astimezone(IST)

    sh_h, sh_m = map(int, shift_start.split(":"))
    eh_h, eh_m = map(int, shift_end.split(":"))

    is_overnight = (eh_h, eh_m) <= (sh_h, sh_m)

    punch_time = (dt_ist.hour, dt_ist.minute, dt_ist.second)
    shift_end_time = (eh_h, eh_m, 0)
    punch_date = dt_ist.date()

    if is_overnight and punch_time < shift_end_time:
        att_date = punch_date - timedelta(days=1)
    else:
        att_date = punch_date

    shift_start_dt = datetime(
        att_date.year, att_date.month, att_date.day, sh_h, sh_m, 0, tzinfo=IST
    )
    if is_overnight:
        shift_end_dt = datetime(
            att_date.year, att_date.month, att_date.day, eh_h, eh_m, 0, tzinfo=IST
        ) + timedelta(days=1)
    else:
        shift_end_dt = datetime(
            att_date.year,
            att_date.month,
            att_date.day,
            eh_h,
            eh_m,
            0,
            tzinfo=IST)

    return att_date.isoformat(), shift_start_dt, shift_end_dt


def calculate_late_minutes(punch_in_ms: int, shift_start_dt: datetime) -> int:
    """R2: Grace period 10 minutes. Floor whole minutes elapsed."""
    punch_in_dt = datetime.fromtimestamp(punch_in_ms // 1000, tz=timezone.utc)
    diff_sec = (punch_in_dt - shift_start_dt).total_seconds()
    if diff_sec > 600:
        return int(diff_sec // 60)
    return 0


def calculate_overtime_minutes(
    punch_out_ms: int, shift_end_dt: datetime
) -> int:
    """R3: Overtime = whole minutes >= 30; otherwise 0."""
    punch_out_dt = datetime.fromtimestamp(
        punch_out_ms // 1000, tz=timezone.utc
    )
    diff_sec = (punch_out_dt - shift_end_dt).total_seconds()
    minutes = int(diff_sec // 60)
    return minutes if minutes >= 30 else 0


def calculate_work_hours(punch_in_ms: int, punch_out_ms: int) -> float:
    """R4: Work hours = seconds / 3600, rounded to 2 decimals half-up."""
    diff_sec = Decimal((punch_out_ms // 1000) - (punch_in_ms // 1000))
    hours = diff_sec / Decimal(3600)
    return float(hours.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def calculate_half_day(work_hours: Optional[float]) -> bool:
    """R5: Half day = work_hours < 4.50."""
    if work_hours is None:
        return False
    return work_hours < 4.50


def get_month_range(month_str: str) -> tuple[str, str, int]:
    year, m = map(int, month_str.split("-"))
    num_days = calendar.monthrange(year, m)[1]
    return f"{month_str}-01", f"{month_str}-{num_days:02d}", num_days


# --------------------------------------------------------------------------- #
# Reusable Aggregation Pipeline Builders (Used by Endpoints & Explain)
# --------------------------------------------------------------------------- #
def build_attendance_query(
    emp_code: Optional[str],
    date_from: Optional[str],
    date_to: Optional[str],
    status_val: Optional[str]
) -> dict:
    q = {}
    if emp_code:
        q["emp_code"] = emp_code
    if status_val:
        q["status"] = status_val
    if date_from or date_to:
        q["date"] = {}
        if date_from:
            q["date"]["$gte"] = date_from
        if date_to:
            q["date"]["$lte"] = date_to
    return q


def build_monthly_pipeline(
    emp_code: str, start_date: str, end_date: str
) -> list[dict]:
    return [
        {
            "$match": {
                "emp_code": emp_code,
                "date": {"$gte": start_date, "$lte": end_date}
            }
        },
        {
            "$group": {
                "_id": None,
                "leave_days": {
                    "$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}
                },
                "late_count": {
                    "$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}
                },
                "total_late_minutes": {
                    "$sum": {"$ifNull": ["$late_minutes", 0]}
                },
                "total_overtime_minutes": {
                    "$sum": {"$ifNull": ["$overtime_minutes", 0]}
                },
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {
                                        "$in": [
                                            "$status",
                                            ["PRESENT", "WFH", "ON_DUTY"]
                                        ]
                                    },
                                    {
                                        "$in": [
                                            {
                                                "$dayOfWeek": {
                                                    "$dateFromString": {
                                                        "dateString": "$date"
                                                    }
                                                }
                                            },
                                            [2, 3, 4, 5, 6]
                                        ]
                                    }
                                ]
                            },
                            {
                                "$cond": [
                                    {"$eq": ["$half_day", True]}, 0.5, 1.0
                                ]
                            },
                            0.0
                        ]
                    }
                }
            }
        }
    ]


def build_summary_pipeline(
    start_date: str, end_date: str, department: Optional[str]
) -> list[dict]:
    match_stage = {"joined_on": {"$lte": end_date}}
    if department:
        match_stage["department"] = department

    return [
        {"$match": match_stage},
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"emp": "$emp_code"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$emp_code", "$$emp"]},
                                    {"$gte": ["$date", start_date]},
                                    {"$lte": ["$date", end_date]}
                                ]
                            }
                        }
                    }
                ],
                "as": "logs"
            }
        },
        {"$unwind": {"path": "$logs", "preserveNullAndEmptyArrays": True}},
        {
            "$group": {
                "_id": "$department",
                "emp_codes": {"$addToSet": "$emp_code"},
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$ne": ["$logs", None]},
                                    {
                                        "$in": [
                                            "$logs.status",
                                            ["PRESENT", "WFH", "ON_DUTY"]
                                        ]
                                    },
                                    {
                                        "$in": [
                                            {
                                                "$dayOfWeek": {
                                                    "$dateFromString": {
                                                        "dateString": (
                                                            "$logs.date"
                                                        )
                                                    }
                                                }
                                            },
                                            [2, 3, 4, 5, 6]
                                        ]
                                    }
                                ]
                            },
                            {
                                "$cond": [
                                    {"$eq": ["$logs.half_day", True]},
                                    0.5,
                                    1.0
                                ]
                            },
                            0.0
                        ]
                    }
                },
                "avg_work_hours": {
                    "$avg": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$ne": ["$logs", None]},
                                    {
                                        "$in": [
                                            "$logs.status",
                                            ["PRESENT", "WFH", "ON_DUTY"]
                                        ]
                                    },
                                    {"$ne": ["$logs.work_hours", None]}
                                ]
                            },
                            "$logs.work_hours",
                            "$$REMOVE"
                        ]
                    }
                },
                "late_count": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$ne": ["$logs", None]},
                                    {"$gt": ["$logs.late_minutes", 0]}
                                ]
                            },
                            1,
                            0
                        ]
                    }
                },
                "total_late_minutes": {
                    "$sum": {
                        "$cond": [
                            {"$ne": ["$logs", None]},
                            {"$ifNull": ["$logs.late_minutes", 0]},
                            0
                        ]
                    }
                },
                "leave_count": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$ne": ["$logs", None]},
                                    {"$eq": ["$logs.status", "LEAVE"]}
                                ]
                            },
                            1,
                            0
                        ]
                    }
                },
                "on_duty_count": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$ne": ["$logs", None]},
                                    {"$eq": ["$logs.status", "ON_DUTY"]}
                                ]
                            },
                            1,
                            0
                        ]
                    }
                }
            }
        },
        {
            "$project": {
                "_id": 0,
                "department": "$_id",
                "headcount": {"$size": "$emp_codes"},
                "present_days": 1,
                "avg_work_hours": 1,
                "late_count": 1,
                "total_late_minutes": 1,
                "leave_count": 1,
                "on_duty_count": 1
            }
        },
        {"$sort": {"department": 1}}
    ]


def build_leaderboard_pipeline(
    start_date: str,
    end_date: str,
    limit: int,
    department: Optional[str]
) -> list[dict]:
    pipeline = [
        {
            "$match": {
                "date": {"$gte": start_date, "$lte": end_date},
                "late_minutes": {"$gt": 0}
            }
        },
        {
            "$group": {
                "_id": "$emp_code",
                "total_late_minutes": {"$sum": "$late_minutes"},
                "late_count": {"$sum": 1}
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "localField": "_id",
                "foreignField": "emp_code",
                "as": "emp"
            }
        },
        {"$unwind": "$emp"}
    ]

    if department:
        pipeline.append({"$match": {"emp.department": department}})

    pipeline.extend([
        {
            "$setWindowFields": {
                "sortBy": {"total_late_minutes": -1},
                "output": {"rank": {"$rank": {}}}
            }
        },
        {"$match": {"rank": {"$lte": limit}}},
        {"$sort": {"total_late_minutes": -1, "_id": 1}},
        {
            "$project": {
                "_id": 0,
                "rank": 1,
                "emp_code": "$_id",
                "name": "$emp.name",
                "department": "$emp.department",
                "total_late_minutes": 1,
                "late_count": 1
            }
        }
    ])
    return pipeline


def build_trend_pipeline(
    dept_emps: list[str],
    from_date: str,
    to_date: str,
    department: str,
    start_dt: datetime,
    end_dt: datetime
) -> list[dict]:
    """
    Build the department trend aggregation pipeline.

    Gap-filling strategy: `$densify` fills every calendar day in [start_dt, end_dt).
    For days with no attendance records the densified documents have no
    `present_count` / `late_count`; a subsequent `$project` replaces those
    with 0 via `$ifNull`.

    `$documents` as a top-level pipeline stage (MongoDB 6.0+) is valid inside
    `$unionWith.pipeline` only when the `coll` key is omitted — but that
    requires the parent stage to be a top-level pipeline.  To stay compatible
    with all supported MongoDB 6.0+ deployments we simply rely on `$densify`
    alone: even if the initial `$match` returns zero documents `$densify` will
    still generate one row per day as long as we supply `bounds`.

    attendance_rate is intentionally left un-rounded inside the pipeline so
    that its raw value flows into `$avg` for `moving_avg_7d` without
    intermediate banker's-rounding bias.  Python post-processing applies
    round_half_up(4) to both fields before returning them to the client.
    """
    return [
        # Stage 1 – filter attendance logs to this department and date range.
        {
            "$match": {
                "emp_code": {"$in": dept_emps},
                "date": {"$gte": from_date, "$lte": to_date}
            }
        },
        # Stage 2 – aggregate per calendar-date string.
        {
            "$group": {
                "_id": "$date",
                "present_count": {
                    "$sum": {
                        "$cond": [
                            {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                            {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1.0]},
                            0.0
                        ]
                    }
                },
                "late_count": {
                    "$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}
                }
            }
        },
        # Stage 3 – promote `_id` to `date` string + derive a real BSON Date
        #           for `$densify` (which operates on datetime fields).
        {
            "$project": {
                "_id": 0,
                "date": "$_id",
                "day_date": {"$dateFromString": {"dateString": "$_id"}},
                "present_count": 1,
                "late_count": 1
            }
        },
        # Stage 4 – gap-fill: one document per calendar day in [start_dt, end_dt).
        #           Days with no records will have null present_count / late_count.
        {
            "$densify": {
                "field": "day_date",
                "range": {
                    "step": 1,
                    "unit": "day",
                    "bounds": [start_dt, end_dt]
                }
            }
        },
        # Stage 5 – rebuild the date string for densified rows (which have no
        #           `date` field) and fill missing counters with 0.
        {
            "$project": {
                "date": {
                    "$dateToString": {"format": "%Y-%m-%d", "date": "$day_date"}
                },
                "day_date": 1,
                "present_count": {"$ifNull": ["$present_count", 0.0]},
                "late_count": {"$ifNull": ["$late_count", 0]},
                "is_working_day": {
                    # MongoDB $dayOfWeek: 1=Sun, 2=Mon … 6=Fri, 7=Sat
                    "$in": [{"$dayOfWeek": "$day_date"}, [2, 3, 4, 5, 6]]
                }
            }
        },
        # Stage 6 – per-day headcount: employees in this department who had
        #           joined on or before this calendar day (R9).
        {
            "$lookup": {
                "from": "employees",
                "let": {"current_day": "$date"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$department", department]},
                                    {"$lte": ["$joined_on", "$$current_day"]}
                                ]
                            }
                        }
                    },
                    {"$count": "count"}
                ],
                "as": "hc"
            }
        },
        # Stage 7 – flatten headcount array.
        {
            "$project": {
                "date": 1,
                "day_date": 1,
                "is_working_day": 1,
                "present_count": 1,
                "late_count": 1,
                "headcount": {"$ifNull": [{"$arrayElemAt": ["$hc.count", 0]}, 0]}
            }
        },
        # Stage 8 – compute raw attendance_rate (NOT rounded here; Python
        #           post-processes with round_half_up so the unbiased ratio
        #           flows cleanly into $avg for moving_avg_7d).
        #           Rule: only working days with headcount > 0 get a rate;
        #           everything else is null.  A working day with no records
        #           has present_count=0, so rate=0.0.
        {
            "$project": {
                "date": 1,
                "day_date": 1,
                "is_working_day": 1,
                "headcount": 1,
                "present_count": 1,
                "late_count": 1,
                "attendance_rate": {
                    "$cond": [
                        {
                            "$and": [
                                "$is_working_day",
                                {"$gt": ["$headcount", 0]}
                            ]
                        },
                        {"$divide": ["$present_count", "$headcount"]},
                        None
                    ]
                }
            }
        },
        # Stage 9 – 7-day moving average of non-null attendance_rate values.
        #           MongoDB $avg naturally ignores null values in window
        #           functions, so no explicit filter is needed.
        #           The window is bounded to the requested range (documents
        #           before `from` are never in scope), so early rows use a
        #           shorter window as specified.
        {
            "$setWindowFields": {
                "sortBy": {"day_date": 1},
                "output": {
                    "moving_avg_7d": {
                        "$avg": "$attendance_rate",
                        "window": {"documents": [-6, "current"]}
                    }
                }
            }
        },
        {"$sort": {"day_date": 1}},
        {"$project": {"_id": 0, "day_date": 0}}
    ]


# --------------------------------------------------------------------------- #
# Endpoints: System & Employees
# --------------------------------------------------------------------------- #
@app.get("/", include_in_schema=False)
def root():
    """Redirect root URL to interactive API docs."""
    return RedirectResponse(url="/docs")


@app.get("/health")
def health():
    try:
        client.admin.command("ping")
        return {"status": "ok"}
    except Exception:
        raise HTTPException(status_code=503, detail="Database unavailable")


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    now_ts = int(datetime.now(timezone.utc).timestamp())
    now_dt = datetime.fromtimestamp(now_ts, tz=timezone.utc)

    doc = body.model_dump()
    doc["created_at"] = now_dt

    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(status_code=409, detail="emp_code already exists")

    doc.pop("_id", None)
    doc["created_at"] = now_ts * 1000
    return doc


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100)
):
    q = {}
    if department:
        q["department"] = department

    skip = (page - 1) * page_size
    total = db.employees.count_documents(q)
    cursor = (
        db.employees.find(q, {"_id": 0})
        .sort("emp_code", 1)
        .skip(skip)
        .limit(page_size)
    )

    items = []
    for emp in cursor:
        emp["created_at"] = serialize_instant(emp.get("created_at"))
        items.append(emp)

    return {
        "items": items,
        "total": total,
        "page": page,
        "page_size": page_size
    }


# --------------------------------------------------------------------------- #
# Endpoints: Attendance CRUD
# --------------------------------------------------------------------------- #
@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    if body.punched_at is not None:
        punch_in_ms = (body.punched_at // 1000) * 1000
    else:
        punch_in_ms = int(datetime.now(timezone.utc).timestamp()) * 1000

    punch_in_dt = datetime.fromtimestamp(punch_in_ms // 1000, tz=timezone.utc)
    att_date, shift_start_dt, _ = calculate_shift_and_date(
        punch_in_ms, emp["shift_start"], emp["shift_end"]
    )
    late_minutes = calculate_late_minutes(punch_in_ms, shift_start_dt)

    doc = {
        "emp_code": body.emp_code,
        "date": att_date,
        "status": body.status.value,
        "punch_in": punch_in_dt,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": late_minutes,
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }

    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(
            status_code=409, detail="already punched in for this date"
        )

    return format_attendance_doc(doc)


@app.post("/attendance/punch-out", status_code=200)
def punch_out(body: PunchOutIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    if body.punched_at is not None:
        punch_out_ms = (body.punched_at // 1000) * 1000
    else:
        punch_out_ms = int(datetime.now(timezone.utc).timestamp()) * 1000

    punch_out_dt = datetime.fromtimestamp(
        punch_out_ms // 1000, tz=timezone.utc
    )

    rec = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$lte": punch_out_dt}},
        sort=[("punch_in", -1)]
    )

    if not rec:
        raise HTTPException(
            status_code=404, detail="No matching punch-in record found"
        )

    if rec.get("punch_out") is not None:
        raise HTTPException(
            status_code=409, detail="Record is already punched out"
        )

    punch_in_ms = serialize_instant(rec["punch_in"])
    if punch_out_ms <= punch_in_ms:
        raise HTTPException(
            status_code=422, detail="punch_out must be strictly after punch_in"
        )

    if (punch_out_ms - punch_in_ms) > 24 * 3600 * 1000:
        raise HTTPException(
            status_code=422,
            detail="punch_out cannot be more than 24 hours after punch_in"
        )

    work_hours = calculate_work_hours(punch_in_ms, punch_out_ms)
    _, _, shift_end_dt = calculate_shift_and_date(
        punch_in_ms, emp["shift_start"], emp["shift_end"]
    )
    overtime_minutes = calculate_overtime_minutes(punch_out_ms, shift_end_dt)
    half_day = calculate_half_day(work_hours)

    updated_rec = db.attendance_logs.find_one_and_update(
        {"_id": rec["_id"], "punch_out": None},
        {
            "$set": {
                "punch_out": punch_out_dt,
                "work_hours": work_hours,
                "overtime_minutes": overtime_minutes,
                "half_day": half_day,
            }
        },
        return_document=ReturnDocument.AFTER
    )

    if not updated_rec:
        raise HTTPException(
            status_code=409, detail="Record was punched out concurrently"
        )

    return format_attendance_doc(updated_rec)


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[Status] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100)
):
    if date_from and date_to and date_from > date_to:
        raise HTTPException(
            status_code=422, detail="date_from cannot be greater than date_to"
        )

    status_val = status.value if status else None
    q = build_attendance_query(emp_code, date_from, date_to, status_val)

    total = db.attendance_logs.count_documents(q)
    skip = (page - 1) * page_size
    cursor = (
        db.attendance_logs.find(q)
        .sort([("date", -1), ("emp_code", 1)])
        .skip(skip)
        .limit(page_size)
    )

    items = [format_attendance_doc(doc) for doc in cursor]
    return {
        "items": items,
        "total": total,
        "page": page,
        "page_size": page_size
    }


@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(
    emp_code: str = Path(...),
    date: str = Path(
        ..., pattern=r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$"
    ),
    body: RegularizeRequest = ...
):
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    rec = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if not rec:
        raise HTTPException(
            status_code=404, detail="Attendance record not found for this date"
        )

    if body.status in (Status.ABSENT, Status.LEAVE) and (
        body.punch_in is not None or body.punch_out is not None
    ):
        raise HTTPException(
            status_code=422,
            detail="Punch times cannot be supplied when status is ABSENT or LEAVE")

    old_status = rec["status"]
    final_status = body.status.value if body.status is not None else old_status

    if final_status in ("ABSENT", "LEAVE"):
        final_punch_in_ms = None
        final_punch_out_ms = None
        final_work_hours = None
        final_late_minutes = 0
        final_overtime_minutes = 0
        final_half_day = False
    else:
        if body.punch_in is not None:
            final_punch_in_ms = (body.punch_in // 1000) * 1000
        else:
            final_punch_in_ms = serialize_instant(rec.get("punch_in"))

        if final_punch_in_ms is None:
            raise HTTPException(
                status_code=422, detail="Presence status requires a punch_in"
            )

        calc_date, shift_start_dt, shift_end_dt = calculate_shift_and_date(
            final_punch_in_ms, emp["shift_start"], emp["shift_end"]
        )
        if calc_date != date:
            raise HTTPException(
                status_code=422,
                detail=f"punch_in belongs to date {calc_date}, not {date}"
            )

        if body.punch_out is not None:
            final_punch_out_ms = (body.punch_out // 1000) * 1000
        else:
            final_punch_out_ms = serialize_instant(rec.get("punch_out"))

        if final_punch_out_ms is not None:
            if final_punch_out_ms <= final_punch_in_ms:
                raise HTTPException(
                    status_code=422,
                    detail="punch_out must be strictly after punch_in"
                )
            if (final_punch_out_ms - final_punch_in_ms) > 24 * 3600 * 1000:
                raise HTTPException(
                    status_code=422,
                    detail="punch_out cannot be more than 24 hours after punch_in")

            final_work_hours = calculate_work_hours(
                final_punch_in_ms, final_punch_out_ms
            )
            final_overtime_minutes = calculate_overtime_minutes(
                final_punch_out_ms, shift_end_dt
            )
            final_half_day = calculate_half_day(final_work_hours)
        else:
            final_work_hours = None
            final_overtime_minutes = 0
            final_half_day = False

        final_late_minutes = calculate_late_minutes(
            final_punch_in_ms, shift_start_dt
        )

    old_punch_in_ms = serialize_instant(rec.get("punch_in"))
    old_punch_out_ms = serialize_instant(rec.get("punch_out"))
    old_work_hours = rec.get("work_hours")
    old_late_minutes = rec.get("late_minutes", 0)
    old_overtime_minutes = rec.get("overtime_minutes", 0)
    old_half_day = rec.get("half_day", False)

    changes_db = {}
    if final_status != old_status:
        changes_db["status"] = {"from": old_status, "to": final_status}
    if final_punch_in_ms != old_punch_in_ms:
        changes_db["punch_in"] = {
            "from": (
                datetime.fromtimestamp(
                    old_punch_in_ms // 1000,
                    tz=timezone.utc) if old_punch_in_ms else None),
            "to": (
                datetime.fromtimestamp(
                    final_punch_in_ms // 1000,
                    tz=timezone.utc) if final_punch_in_ms else None)}
    if final_punch_out_ms != old_punch_out_ms:
        changes_db["punch_out"] = {
            "from": (
                datetime.fromtimestamp(
                    old_punch_out_ms // 1000,
                    tz=timezone.utc) if old_punch_out_ms else None),
            "to": (
                datetime.fromtimestamp(
                    final_punch_out_ms // 1000,
                    tz=timezone.utc) if final_punch_out_ms else None)}
    if final_work_hours != old_work_hours:
        changes_db["work_hours"] = {
            "from": old_work_hours, "to": final_work_hours
        }
    if final_late_minutes != old_late_minutes:
        changes_db["late_minutes"] = {
            "from": old_late_minutes, "to": final_late_minutes
        }
    if final_overtime_minutes != old_overtime_minutes:
        changes_db["overtime_minutes"] = {
            "from": old_overtime_minutes, "to": final_overtime_minutes
        }
    if final_half_day != old_half_day:
        changes_db["half_day"] = {
            "from": old_half_day, "to": final_half_day
        }

    if not changes_db:
        raise HTTPException(
            status_code=422, detail="Request changes nothing"
        )

    now_ts = int(datetime.now(timezone.utc).timestamp())
    now_dt = datetime.fromtimestamp(now_ts, tz=timezone.utc)

    history_entry = {
        "at": now_dt,
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes_db
    }

    update_fields = {
        "status": final_status,
        "punch_in": (
            datetime.fromtimestamp(final_punch_in_ms // 1000, tz=timezone.utc)
            if final_punch_in_ms else None
        ),
        "punch_out": (
            datetime.fromtimestamp(final_punch_out_ms // 1000, tz=timezone.utc)
            if final_punch_out_ms else None
        ),
        "work_hours": final_work_hours,
        "late_minutes": final_late_minutes,
        "overtime_minutes": final_overtime_minutes,
        "half_day": final_half_day,
    }

    existing_history = rec.get("history", [])
    history_len = len(existing_history)

    updated_rec = db.attendance_logs.find_one_and_update(
        {
            "_id": rec["_id"],
            f"history.{history_len}": {"$exists": False}
        },
        {
            "$set": update_fields,
            "$push": {"history": history_entry}
        },
        return_document=ReturnDocument.AFTER
    )

    if not updated_rec:
        raise HTTPException(
            status_code=409,
            detail="Concurrent update conflict on regularization"
        )

    return format_attendance_doc(updated_rec)


# --------------------------------------------------------------------------- #
# Endpoints: Analytics
# --------------------------------------------------------------------------- #
@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(
    emp_code: str = Path(...),
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$")
):
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    start_date, end_date, _ = get_month_range(month)

    emp_joined = emp.get("joined_on", "1970-01-01")
    if emp_joined > end_date:
        working_days = 0
    else:
        effective_start = max(emp_joined, start_date)
        cur = date.fromisoformat(effective_start)
        last_d = date.fromisoformat(end_date)
        working_days = 0
        while cur <= last_d:
            if cur.weekday() < 5:
                working_days += 1
            cur += timedelta(days=1)

    pipeline = build_monthly_pipeline(emp_code, start_date, end_date)
    agg_res = list(db.attendance_logs.aggregate(pipeline))

    if agg_res:
        data = agg_res[0]
        present_days = float(data["present_days"])
        leave_days = int(data["leave_days"])
        late_count = int(data["late_count"])
        total_late = int(data["total_late_minutes"])
        total_ot = int(data["total_overtime_minutes"])
    else:
        present_days = 0.0
        leave_days = 0
        late_count = 0
        total_late = 0
        total_ot = 0

    if working_days == 0:
        attendance_pct = None
    else:
        attendance_pct = round_half_up((present_days / working_days) * 100, 2)

    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": present_days,
        "leave_days": leave_days,
        "late_count": late_count,
        "total_late_minutes": total_late,
        "total_overtime_minutes": total_ot,
        "attendance_pct": attendance_pct
    }


@app.get("/analytics/departments/summary")
def department_summary(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None
):
    start_date, end_date, _ = get_month_range(month)
    pipeline = build_summary_pipeline(start_date, end_date, department)

    items = list(db.employees.aggregate(pipeline))
    for item in items:
        if item.get("avg_work_hours") is not None:
            item["avg_work_hours"] = round_half_up(item["avg_work_hours"], 2)

    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = None
):
    start_date, end_date, _ = get_month_range(month)
    pipeline = build_leaderboard_pipeline(
        start_date, end_date, limit, department
    )
    items = list(db.attendance_logs.aggregate(pipeline))
    return {"month": month, "items": items}


@app.get("/analytics/departments/{department}/trend")
def department_trend(department: str = Path(...),
                     from_date: str = Query(...,
                                            alias="from",
                                            pattern=r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$"),
                     to_date: str = Query(...,
                                          alias="to",
                                          pattern=r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$")):
    dept_exists = db.employees.find_one({"department": department})
    if not dept_exists:
        raise HTTPException(status_code=404, detail="Department not found")

    d_from = date.fromisoformat(from_date)
    d_to = date.fromisoformat(to_date)
    if d_to < d_from:
        raise HTTPException(
            status_code=422, detail="'to' date must be on or after 'from' date"
        )

    num_days = (d_to - d_from).days + 1
    if num_days > 92:
        raise HTTPException(
            status_code=422, detail="Date range cannot exceed 92 days"
        )

    dept_emps = [
        e["emp_code"]
        for e in db.employees.find({"department": department}, {"emp_code": 1})
    ]

    start_dt = datetime(
        d_from.year,
        d_from.month,
        d_from.day,
        tzinfo=timezone.utc)
    end_dt = datetime(
        d_to.year, d_to.month, d_to.day, tzinfo=timezone.utc
    ) + timedelta(days=1)

    pipeline = build_trend_pipeline(
        dept_emps, from_date, to_date, department, start_dt, end_dt
    )

    items = list(db.attendance_logs.aggregate(pipeline))
    for it in items:
        if it.get("attendance_rate") is not None:
            it["attendance_rate"] = round_half_up(it["attendance_rate"], 4)
        if it.get("moving_avg_7d") is not None:
            it["moving_avg_7d"] = round_half_up(it["moving_avg_7d"], 4)

    return {"department": department, "items": items}


# --------------------------------------------------------------------------- #
# Endpoints: Admin Explain
# --------------------------------------------------------------------------- #
def sanitize_explain(obj: Any) -> Any:
    """Helper to ensure explain document is cleanly JSON serializable."""
    if isinstance(obj, dict):
        return {k: sanitize_explain(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [sanitize_explain(v) for v in obj]
    if isinstance(obj, datetime):
        return obj.isoformat()
    return obj


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: str = Path(
        ...,
        enum=[
            "attendance_list",
            "employee_monthly",
            "department_summary",
            "late_leaderboard",
            "department_trend"
        ]
    ),
    emp_code: Optional[str] = None,
    month: Optional[str] = None,
    department: Optional[str] = None,
    limit: int = 10,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status_filter: Optional[str] = Query(None, alias="status"),
    from_date: Optional[str] = Query(None, alias="from"),
    to_date: Optional[str] = Query(None, alias="to"),
    page: int = 1,
    page_size: int = 20
):
    month_regex = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")
    date_regex = re.compile(r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$")

    if endpoint == "attendance_list":
        q = build_attendance_query(emp_code, date_from, date_to, status_filter)
        cursor = (
            db.attendance_logs.find(q)
            .sort([("date", -1), ("emp_code", 1)])
            .skip((page - 1) * page_size)
            .limit(page_size)
        )
        raw_explain = cursor.explain()
        return {
            "endpoint": endpoint,
            "collection": "attendance_logs",
            "explain": sanitize_explain(raw_explain)
        }

    elif endpoint == "employee_monthly":
        if not emp_code or not month:
            raise HTTPException(
                status_code=422, detail="'emp_code' and 'month' are required"
            )
        if not month_regex.match(month):
            raise HTTPException(status_code=422, detail="Invalid month format")

        start_date, end_date, _ = get_month_range(month)
        pipeline = build_monthly_pipeline(emp_code, start_date, end_date)
        raw_explain = db.command(
            "explain",
            {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}},
            verbosity="executionStats"
        )
        return {
            "endpoint": endpoint,
            "collection": "attendance_logs",
            "explain": sanitize_explain(raw_explain)
        }

    elif endpoint == "department_summary":
        if not month:
            raise HTTPException(status_code=422, detail="'month' is required")
        if not month_regex.match(month):
            raise HTTPException(status_code=422, detail="Invalid month format")

        start_date, end_date, _ = get_month_range(month)
        pipeline = build_summary_pipeline(start_date, end_date, department)
        raw_explain = db.command(
            "explain",
            {"aggregate": "employees", "pipeline": pipeline, "cursor": {}},
            verbosity="executionStats"
        )
        return {
            "endpoint": endpoint,
            "collection": "employees",
            "explain": sanitize_explain(raw_explain)
        }

    elif endpoint == "late_leaderboard":
        if not month:
            raise HTTPException(status_code=422, detail="'month' is required")
        if not month_regex.match(month):
            raise HTTPException(status_code=422, detail="Invalid month format")

        start_date, end_date, _ = get_month_range(month)
        pipeline = build_leaderboard_pipeline(
            start_date, end_date, limit, department
        )
        raw_explain = db.command(
            "explain",
            {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}},
            verbosity="executionStats"
        )
        return {
            "endpoint": endpoint,
            "collection": "attendance_logs",
            "explain": sanitize_explain(raw_explain)
        }

    elif endpoint == "department_trend":
        if not department or not from_date or not to_date:
            raise HTTPException(
                status_code=422,
                detail="'department', 'from' and 'to' are required"
            )
        if not date_regex.match(from_date) or not date_regex.match(to_date):
            raise HTTPException(status_code=422, detail="Invalid date format")

        d_from = date.fromisoformat(from_date)
        d_to = date.fromisoformat(to_date)
        if d_to < d_from:
            raise HTTPException(
                status_code=422,
                detail="'to' date must be on or after 'from' date"
            )
        if (d_to - d_from).days + 1 > 92:
            raise HTTPException(
                status_code=422, detail="Date range cannot exceed 92 days"
            )

        dept_emps = [
            e["emp_code"]
            for e in db.employees.find({"department": department}, {"emp_code": 1})
        ]
        start_dt = datetime(
            d_from.year, d_from.month, d_from.day, tzinfo=timezone.utc
        )
        end_dt = datetime(
            d_to.year, d_to.month, d_to.day, tzinfo=timezone.utc
        ) + timedelta(days=1)

        pipeline = build_trend_pipeline(
            dept_emps, from_date, to_date, department, start_dt, end_dt
        )
        raw_explain = db.command(
            "explain",
            {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}},
            verbosity="executionStats"
        )
        return {
            "endpoint": endpoint,
            "collection": "attendance_logs",
            "explain": sanitize_explain(raw_explain)
        }
