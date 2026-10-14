"""
Employee Attendance & Analytics API - STARTER

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (a local .env is loaded for convenience)

This file was written quickly by a colleague who has left the company. The happy path
works, but nobody has reviewed it. Read PROBLEM_STATEMENT.docx for what is expected of you,
openapi.yaml for the contract and DATA_MODEL.md for what is stored in MongoDB.
"""
import os
import re
from contextlib import asynccontextmanager
from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel
from pymongo import MongoClient, errors

load_dotenv()

client = MongoClient(os.getenv("MONGO_URI", "mongodb://localhost:27017"))
db = client[os.getenv("MONGO_DB", "attendance_db")]

IST = timezone(timedelta(hours=5, minutes=30))
VALID_PRESENCE_STATUSES = {"PRESENT", "WFH", "ON_DUTY"}
VALID_STATUSES = {"PRESENT", "WFH", "ON_DUTY", "ABSENT", "LEAVE"}


def startup() -> None:
    _ensure_employee_indexes()


@asynccontextmanager
async def lifespan(_: FastAPI):
    startup()
    yield


app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan)


def dt_utc_to_epoch_ms(value: Optional[datetime]) -> Optional[int]:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return int(value.astimezone(timezone.utc).timestamp() * 1000)


def epoch_ms_to_dt(value: Optional[int | datetime]) -> Optional[datetime]:
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)
    return datetime.fromtimestamp(value / 1000, tz=timezone.utc)


def round_half_up(value: float, digits: int) -> float:
    q = Decimal("1").scaleb(-digits)
    return float(Decimal(str(value)).quantize(q, rounding=ROUND_HALF_UP))


def _ensure_employee_indexes() -> None:
    db.employees.create_index("emp_code", unique=True)
    db.employees.create_index("department")
    db.attendance_logs.create_index([("emp_code", 1), ("date", 1)], unique=True)
    db.attendance_logs.create_index([("date", -1), ("emp_code", 1)])
    db.attendance_logs.create_index("status")
    db.attendance_logs.create_index("punch_in")


def _serialize_employee(doc: dict) -> dict:
    out = {k: v for k, v in doc.items() if k != "_id"}
    if "created_at" in out and out["created_at"] is not None:
        out["created_at"] = dt_utc_to_epoch_ms(out["created_at"])
    return out


def _serialize_history(entry: dict) -> dict:
    out = {"at": dt_utc_to_epoch_ms(entry.get("at")), "by": entry.get("by"), "reason": entry.get("reason"), "changes": {}}
    for key, val in (entry.get("changes") or {}).items():
        out["changes"][key] = {
            "from": dt_utc_to_epoch_ms(val.get("from")) if isinstance(val.get("from"), datetime) else val.get("from"),
            "to": dt_utc_to_epoch_ms(val.get("to")) if isinstance(val.get("to"), datetime) else val.get("to"),
        }
    return out


def _serialize_attendance(doc: dict) -> dict:
    out = {k: v for k, v in doc.items() if k != "_id"}
    for key in ("punch_in", "punch_out"):
        if key in out:
            out[key] = dt_utc_to_epoch_ms(out[key]) if out[key] is not None else None
    if "history" in out:
        out["history"] = [_serialize_history(e) for e in out["history"]]
    return out


def _coerce_epoch_ms(value: Optional[int], *, default_now: bool = False) -> datetime:
    if value is None:
        if default_now:
            return datetime.now(timezone.utc)
        raise HTTPException(422, "timestamp is required")
    if not isinstance(value, int) or isinstance(value, bool):
        raise HTTPException(422, "timestamp must be an integer epoch milliseconds value")
    if value < 100000000000 or value > 4102444800000:
        raise HTTPException(422, "timestamp out of supported range")
    return datetime.fromtimestamp(value / 1000, tz=timezone.utc)


def _parse_date_str(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise HTTPException(422, f"invalid date: {value}") from exc


def _is_weekday(value: str) -> bool:
    return datetime.strptime(value, "%Y-%m-%d").weekday() < 5


def _attendance_date_for_punch_in(ts: datetime, shift_start: str, shift_end: str) -> str:
    local = ts.astimezone(IST)
    start_h, start_m = map(int, shift_start.split(":"))
    end_h, end_m = map(int, shift_end.split(":"))
    end_minutes = end_h * 60 + end_m
    start_minutes = start_h * 60 + start_m
    if end_minutes <= start_minutes:
        end_dt = local.replace(hour=end_h, minute=end_m, second=0, microsecond=0)
        if local < end_dt:
            return (local.date() - timedelta(days=1)).isoformat()
    return local.date().isoformat()


def compute_late_minutes(punch_in: datetime, shift_start: str, shift_end: Optional[str] = None) -> int:
    punch_local = punch_in.astimezone(IST) if punch_in.tzinfo else punch_in.replace(tzinfo=IST)
    h, m = map(int, shift_start.split(":"))
    shift_start_dt = punch_local.replace(hour=h, minute=m, second=0, microsecond=0)
    if shift_end and shift_end <= shift_start:
        end_h, end_m = map(int, shift_end.split(":"))
        end_dt = punch_local.replace(hour=end_h, minute=end_m, second=0, microsecond=0)
        if punch_local.time() < end_dt.time():
            shift_start_dt = shift_start_dt - timedelta(days=1)
    diff = punch_local - shift_start_dt
    minutes = int(diff.total_seconds() // 60)
    return minutes if minutes > 10 else 0


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    total_hours = (punch_out - punch_in).total_seconds() / 3600
    return float(round_half_up(total_hours, 2))


def compute_overtime(punch_out: datetime, shift_start: str, shift_end: str, record_date: str) -> int:
    if shift_end <= shift_start:
        end_dt = datetime.strptime(f"{record_date} {shift_end}", "%Y-%m-%d %H:%M").replace(tzinfo=IST) + timedelta(days=1)
    else:
        end_dt = datetime.strptime(f"{record_date} {shift_end}", "%Y-%m-%d %H:%M").replace(tzinfo=IST)
    diff = punch_out.astimezone(IST) - end_dt
    minutes = int(diff.total_seconds() // 60)
    return max(0, minutes) if minutes >= 30 else 0


def _recompute_derived(doc: dict, emp: dict) -> dict:
    status = (doc.get("status") or "PRESENT").upper()
    punch_in = doc.get("punch_in")
    punch_out = doc.get("punch_out")
    if status in {"ABSENT", "LEAVE"}:
        doc["punch_in"] = None
        doc["punch_out"] = None
        doc["work_hours"] = None
        doc["late_minutes"] = 0
        doc["overtime_minutes"] = 0
        doc["half_day"] = False
        return doc

    if punch_in is None:
        raise HTTPException(422, "presence status requires a punch_in")

    punch_in_dt = epoch_ms_to_dt(punch_in)
    punch_out_dt = epoch_ms_to_dt(punch_out) if punch_out is not None else None
    punch_in_date = _attendance_date_for_punch_in(punch_in_dt, emp["shift_start"], emp["shift_end"])
    if punch_in_date != doc.get("date"):
        raise HTTPException(422, "punch_in must stay on the record attendance date")

    doc["late_minutes"] = compute_late_minutes(punch_in_dt, emp["shift_start"], emp["shift_end"])

    if punch_out_dt is None:
        doc["work_hours"] = None
        doc["overtime_minutes"] = 0
        doc["half_day"] = False
        return doc

    if punch_out_dt <= punch_in_dt:
        raise HTTPException(422, "punch_out must be after punch_in")
    if (punch_out_dt - punch_in_dt).total_seconds() > 24 * 60 * 60:
        raise HTTPException(422, "punch_out must be within 24 hours of punch_in")

    doc["work_hours"] = compute_work_hours(punch_in_dt, punch_out_dt)
    doc["overtime_minutes"] = compute_overtime(punch_out_dt, emp["shift_start"], emp["shift_end"], doc["date"])
    doc["half_day"] = doc["work_hours"] < 4.5
    return doc


# --------------------------------------------------------------------------- #
# Models
# --------------------------------------------------------------------------- #
class EmployeeIn(BaseModel):
    emp_code: str
    name: str
    email: str
    department: str
    shift_start: str = "09:30"
    shift_end: str = "18:30"
    joined_on: str


class PunchInIn(BaseModel):
    emp_code: str
    punched_at: Optional[int] = None
    status: str = "PRESENT"


class PunchOutIn(BaseModel):
    emp_code: str
    punched_at: Optional[int] = None


# --------------------------------------------------------------------------- #
# Endpoints provided
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    try:
        client.admin.command("ping")
        return {"status": "ok"}
    except Exception:
        raise HTTPException(503, "MongoDB unavailable")


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    if not re.fullmatch(r"EMP\d{4,6}", body.emp_code):
        raise HTTPException(422, "emp_code must match EMP followed by 4-6 digits")
    if body.shift_start == body.shift_end:
        raise HTTPException(422, "shift_start must differ from shift_end")
    try:
        _parse_date_str(body.joined_on)
    except HTTPException:
        raise
    if not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", body.shift_start):
        raise HTTPException(422, "shift_start must be HH:MM 24-hour")
    if not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", body.shift_end):
        raise HTTPException(422, "shift_end must be HH:MM 24-hour")
    doc = body.model_dump()
    doc["created_at"] = datetime.now(timezone.utc)
    try:
        db.employees.insert_one(doc)
    except errors.DuplicateKeyError as exc:
        raise HTTPException(409, "emp_code already exists") from exc
    return _serialize_employee(doc)


@app.get("/employees")
def list_employees(department: Optional[str] = None, page: int = 1, page_size: int = 20):
    q = {}
    if department:
        q["department"] = department
    page = max(1, page)
    page_size = min(max(1, page_size), 100)
    skip = (page - 1) * page_size
    total = db.employees.count_documents(q)
    items = list(db.employees.find(q).sort("emp_code", 1).skip(skip).limit(page_size))
    return {"items": [_serialize_employee(doc) for doc in items], "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(404, "employee not found")

    status = str(body.status).upper()
    if status not in VALID_PRESENCE_STATUSES:
        raise HTTPException(422, "status must be PRESENT, WFH or ON_DUTY")

    ts = _coerce_epoch_ms(body.punched_at, default_now=True)
    d = _attendance_date_for_punch_in(ts, emp["shift_start"], emp["shift_end"])
    if db.attendance_logs.find_one({"emp_code": body.emp_code, "date": d}):
        raise HTTPException(409, "already punched in for this date")

    doc = {
        "emp_code": body.emp_code,
        "date": d,
        "status": status,
        "punch_in": ts,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": compute_late_minutes(ts, emp["shift_start"], emp["shift_end"]),
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }

    try:
        res = db.attendance_logs.insert_one(doc)
    except errors.DuplicateKeyError as exc:
        raise HTTPException(409, "already punched in for this date") from exc
    return _serialize_attendance({**doc, "_id": res.inserted_id})


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(404, "employee not found")

    ts = _coerce_epoch_ms(body.punched_at, default_now=True)
    record = db.attendance_logs.find_one({"emp_code": body.emp_code, "punch_out": None}, sort=[("punch_in", -1)])
    if not record:
        raise HTTPException(404, "no punch-in found")
    if record["punch_in"] is None:
        raise HTTPException(404, "no punch-in found")

    record_punch_in = epoch_ms_to_dt(record["punch_in"])
    if ts <= record_punch_in:
        raise HTTPException(422, "punch_out must be after punch_in")
    if (ts - record_punch_in).total_seconds() > 24 * 60 * 60:
        raise HTTPException(422, "punch_out must be within 24 hours of punch_in")

    final = deepcopy(record)
    final["punch_out"] = ts
    final["work_hours"] = compute_work_hours(record_punch_in, ts)
    final["overtime_minutes"] = compute_overtime(ts, emp["shift_start"], emp["shift_end"], record["date"])
    final["half_day"] = final["work_hours"] < 4.5

    result = db.attendance_logs.find_one_and_update(
        {"_id": record["_id"]},
        {"$set": {"punch_out": ts, "work_hours": final["work_hours"], "overtime_minutes": final["overtime_minutes"], "half_day": final["half_day"]}},
        return_document=True,
    )
    return _serialize_attendance(result)


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    page: int = 1,
    page_size: int = 20,
):
    q = {}
    if emp_code:
        q["emp_code"] = emp_code
    if date_from or date_to:
        q["date"] = {}
        if date_from:
            q["date"]["$gte"] = date_from
        if date_to:
            q["date"]["$lte"] = date_to
    if status:
        q["status"] = status

    if date_from and date_to and date_from > date_to:
        raise HTTPException(422, "date_from must be <= date_to")

    docs = list(db.attendance_logs.find(q).sort([("date", -1), ("emp_code", 1)]))
    total = len(docs)
    page = max(1, page)
    page_size = min(max(1, page_size), 100)
    page_docs = docs[(page - 1) * page_size : page * page_size]
    return {"items": [_serialize_attendance(doc) for doc in page_docs], "total": total, "page": page, "page_size": page_size}


@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(emp_code: str, date: str, body: dict):
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(404, "employee not found")
    doc = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if not doc:
        raise HTTPException(404, "attendance record not found")

    reason = body.get("reason")
    regularized_by = body.get("regularized_by")
    if not reason or not regularized_by:
        raise HTTPException(422, "reason and regularized_by are required")

    update = deepcopy(doc)
    if "status" in body and body["status"] is not None:
        update["status"] = str(body["status"]).upper()
    if "punch_in" in body and body["punch_in"] is not None:
        update["punch_in"] = epoch_ms_to_dt(body["punch_in"])
    if "punch_out" in body and body["punch_out"] is not None:
        update["punch_out"] = epoch_ms_to_dt(body["punch_out"])

    if update.get("status") in {"ABSENT", "LEAVE"}:
        if body.get("punch_in") is not None or body.get("punch_out") is not None:
            raise HTTPException(422, "ABSENT/LEAVE records must not have punch times")
        update["punch_in"] = None
        update["punch_out"] = None
    elif update.get("status") in VALID_PRESENCE_STATUSES:
        if update.get("punch_in") is None and doc.get("punch_in") is None:
            raise HTTPException(422, "presence status requires punch_in")
        if update.get("punch_in") is not None:
            date_check = _attendance_date_for_punch_in(update["punch_in"], emp["shift_start"], emp["shift_end"])
            if date_check != date:
                raise HTTPException(422, "punch_in must stay on the record attendance date")
        if update.get("punch_out") is not None and update.get("punch_in") is not None:
            if update["punch_out"] <= update["punch_in"]:
                raise HTTPException(422, "punch_out must be after punch_in")
            if (update["punch_out"] - update["punch_in"]).total_seconds() > 24 * 60 * 60:
                raise HTTPException(422, "punch_out must be within 24 hours of punch_in")

    if update.get("status") not in VALID_STATUSES:
        raise HTTPException(422, "invalid status")

    # Ignore read-only fields in request body
    _recompute_derived(update, emp)

    changes = {}
    for field in ["status", "punch_in", "punch_out", "work_hours", "late_minutes", "overtime_minutes", "half_day"]:
        old = doc.get(field)
        new = update.get(field)
        if old != new:
            changes[field] = {"from": old, "to": new}
    if not changes:
        raise HTTPException(422, "request changes nothing")

    history_entry = {
        "at": datetime.now(timezone.utc),
        "by": regularized_by,
        "reason": reason,
        "changes": changes,
    }
    update["history"] = (doc.get("history") or []) + [history_entry]

    db.attendance_logs.update_one({"_id": doc["_id"]}, {"$set": {
        "status": update["status"],
        "punch_in": update.get("punch_in"),
        "punch_out": update.get("punch_out"),
        "work_hours": update.get("work_hours"),
        "late_minutes": update.get("late_minutes"),
        "overtime_minutes": update.get("overtime_minutes"),
        "half_day": update.get("half_day"),
        "history": update["history"],
    }})
    final = db.attendance_logs.find_one({"_id": doc["_id"]})
    return _serialize_attendance(final)


# --------------------------------------------------------------------------- #
# Analytics
# --------------------------------------------------------------------------- #
@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str):
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(404, "employee not found")
    try:
        yy, mm = map(int, month.split("-"))
        first_day = date(yy, mm, 1)
        if mm == 12:
            next_month = date(yy + 1, 1, 1)
        else:
            next_month = date(yy, mm + 1, 1)
        last_day = next_month - timedelta(days=1)
    except Exception as exc:
        raise HTTPException(422, "invalid month") from exc

    joined = _parse_date_str(emp["joined_on"])
    working_days = 0
    for day_offset in range((last_day - first_day).days + 1):
        cur = first_day + timedelta(days=day_offset)
        if cur >= joined and cur.weekday() < 5:
            working_days += 1

    logs = list(db.attendance_logs.find({"emp_code": emp_code, "date": {"$gte": first_day.isoformat(), "$lte": last_day.isoformat()}}))
    present_days = 0.0
    leave_days = 0
    late_count = 0
    total_late_minutes = 0
    total_overtime_minutes = 0
    for log in logs:
        d = log["date"]
        if _is_weekday(d) and log.get("status") in VALID_PRESENCE_STATUSES:
            present_days += 0.5 if log.get("half_day") else 1.0
        if log.get("status") == "LEAVE":
            leave_days += 1
        if log.get("late_minutes", 0) > 0:
            late_count += 1
            total_late_minutes += int(log.get("late_minutes", 0))
        total_overtime_minutes += int(log.get("overtime_minutes", 0))

    attendance_pct = None if working_days == 0 else round_half_up((present_days / working_days) * 100, 4)
    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": present_days,
        "leave_days": leave_days,
        "late_count": late_count,
        "total_late_minutes": total_late_minutes,
        "total_overtime_minutes": total_overtime_minutes,
        "attendance_pct": attendance_pct,
    }


@app.get("/analytics/departments/summary")
def department_summary(month: str, department: Optional[str] = None):
    try:
        yy, mm = map(int, month.split("-"))
        first_day = date(yy, mm, 1)
        if mm == 12:
            next_month = date(yy + 1, 1, 1)
        else:
            next_month = date(yy, mm + 1, 1)
        last_day = next_month - timedelta(days=1)
    except Exception as exc:
        raise HTTPException(422, "invalid month") from exc

    q = {"joined_on": {"$lte": last_day.isoformat()}}
    if department:
        q["department"] = department
    employees = list(db.employees.find(q))
    by_dept = {}
    for emp in employees:
        by_dept.setdefault(emp["department"], []).append(emp["emp_code"])

    logs = list(db.attendance_logs.find({"date": {"$gte": first_day.isoformat(), "$lte": last_day.isoformat()}}))
    row_by_dept = {}
    for dept, emp_codes in by_dept.items():
        row_by_dept[dept] = {"department": dept, "headcount": len(emp_codes), "present_days": 0.0, "avg_work_hours": None, "late_count": 0, "total_late_minutes": 0, "leave_count": 0, "on_duty_count": 0, "hours_total": 0.0, "hours_count": 0}
    for log in logs:
        dept = db.employees.find_one({"emp_code": log["emp_code"]}, {"department": 1})
        if not dept:
            continue
        dept_name = dept["department"]
        if dept_name not in row_by_dept:
            row_by_dept[dept_name] = {"department": dept_name, "headcount": 0, "present_days": 0.0, "avg_work_hours": None, "late_count": 0, "total_late_minutes": 0, "leave_count": 0, "on_duty_count": 0, "hours_total": 0.0, "hours_count": 0}
        if _is_weekday(log["date"]) and log.get("status") in VALID_PRESENCE_STATUSES:
            row_by_dept[dept_name]["present_days"] += 0.5 if log.get("half_day") else 1.0
        if log.get("status") == "LEAVE":
            row_by_dept[dept_name]["leave_count"] += 1
        if log.get("status") == "ON_DUTY":
            row_by_dept[dept_name]["on_duty_count"] += 1
        if log.get("late_minutes", 0) > 0:
            row_by_dept[dept_name]["late_count"] += 1
            row_by_dept[dept_name]["total_late_minutes"] += int(log.get("late_minutes", 0))
        work_hours = log.get("work_hours")
        if log.get("status") in VALID_PRESENCE_STATUSES and work_hours is not None:
            row_by_dept[dept_name]["hours_total"] += float(work_hours)
            row_by_dept[dept_name]["hours_count"] += 1

    items = []
    for dept, row in sorted(row_by_dept.items()):
        if row["headcount"] == 0:
            continue
        if row["hours_count"]:
            row["avg_work_hours"] = round_half_up(row["hours_total"] / row["hours_count"], 2)
        else:
            row["avg_work_hours"] = None
        row.pop("hours_total", None)
        row.pop("hours_count", None)
        items.append({
            "department": row["department"],
            "headcount": row["headcount"],
            "present_days": row["present_days"],
            "avg_work_hours": row["avg_work_hours"],
            "late_count": row["late_count"],
            "total_late_minutes": row["total_late_minutes"],
            "leave_count": row["leave_count"],
            "on_duty_count": row["on_duty_count"],
        })

    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(month: str, limit: int = 10, department: Optional[str] = None):
    if not 1 <= limit <= 50:
        raise HTTPException(422, "limit must be between 1 and 50")
    try:
        yy, mm = map(int, month.split("-"))
        first_day = date(yy, mm, 1)
        if mm == 12:
            next_month = date(yy + 1, 1, 1)
        else:
            next_month = date(yy, mm + 1, 1)
        last_day = next_month - timedelta(days=1)
    except Exception as exc:
        raise HTTPException(422, "invalid month") from exc

    logs = list(db.attendance_logs.find({"date": {"$gte": first_day.isoformat(), "$lte": last_day.isoformat()}}))
    totals = {}
    for log in logs:
        if log.get("late_minutes", 0) <= 0:
            continue
        emp = db.employees.find_one({"emp_code": log["emp_code"]})
        if not emp:
            continue
        if department and emp["department"] != department:
            continue
        totals.setdefault(log["emp_code"], {"total_late_minutes": 0, "late_count": 0})
        totals[log["emp_code"]]["total_late_minutes"] += int(log.get("late_minutes", 0))
        totals[log["emp_code"]]["late_count"] += 1

    sorted_rows = sorted(totals.items(), key=lambda kv: (-kv[1]["total_late_minutes"], kv[0]))
    rows = []
    prev_total = None
    current_rank = 0
    for idx, (emp_code, payload) in enumerate(sorted_rows, start=1):
        total_late = payload["total_late_minutes"]
        if prev_total is None or total_late != prev_total:
            current_rank = idx
            prev_total = total_late
        emp = db.employees.find_one({"emp_code": emp_code})
        if emp is None:
            continue
        rows.append({
            "rank": current_rank,
            "emp_code": emp_code,
            "name": emp["name"],
            "department": emp["department"],
            "total_late_minutes": total_late,
            "late_count": payload["late_count"],
        })
    filtered = [row for row in rows if row["rank"] <= limit]
    return {"month": month, "items": filtered}


@app.get("/analytics/departments/{department}/trend")
def department_trend(department: str, from_: Optional[str] = Query(None, alias="from"), to: Optional[str] = None):
    dept_employees = list(db.employees.find({"department": department}))
    if not dept_employees:
        raise HTTPException(404, "department not found")
    start = _parse_date_str(from_)
    end = _parse_date_str(to)
    if end < start:
        raise HTTPException(422, "to must be >= from")
    if (end - start).days + 1 > 92:
        raise HTTPException(422, "date range must be <= 92 days")

    d = start
    items = []
    while d <= end:
        date_str = d.isoformat()
        headcount = sum(1 for emp in dept_employees if _parse_date_str(emp["joined_on"]) <= d)
        logs = list(db.attendance_logs.find({"date": date_str, "emp_code": {"$in": [emp["emp_code"] for emp in dept_employees]}}))
        present_count = 0.0
        late_count = 0
        for log in logs:
            if log.get("status") in VALID_PRESENCE_STATUSES:
                present_count += 0.5 if log.get("half_day") else 1.0
            if log.get("late_minutes", 0) > 0:
                late_count += 1
        attendance_rate = None
        if d.weekday() < 5:
            if headcount > 0:
                attendance_rate = round_half_up(present_count / headcount, 4)
                if present_count == 0 and (not logs):
                    attendance_rate = 0.0
        items.append({
            "date": date_str,
            "is_working_day": d.weekday() < 5,
            "headcount": headcount,
            "present_count": present_count,
            "late_count": late_count,
            "attendance_rate": attendance_rate,
            "moving_avg_7d": None,
        })
        d += timedelta(days=1)

    window = []
    for idx, row in enumerate(items):
        window = [it for it in items[max(0, idx - 6): idx + 1] if it["attendance_rate"] is not None]
        if window:
            row["moving_avg_7d"] = round_half_up(sum(it["attendance_rate"] for it in window) / len(window), 4)
        else:
            row["moving_avg_7d"] = None

    return {"department": department, "items": items}


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: str,
    emp_code: Optional[str] = None,
    month: Optional[str] = None,
    department: Optional[str] = None,
    limit: Optional[int] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    from_: Optional[str] = Query(None, alias="from"),
    to: Optional[str] = None,
    page: int = 1,
    page_size: int = 20,
):
    if endpoint == "attendance_list":
        q = {}
        if emp_code:
            q["emp_code"] = emp_code
        if date_from or date_to:
            q["date"] = {}
            if date_from:
                q["date"]["$gte"] = date_from
            if date_to:
                q["date"]["$lte"] = date_to
        if status:
            q["status"] = status
        plan = db.attendance_logs.find(q).sort([("date", -1), ("emp_code", 1)]).skip((page - 1) * page_size).limit(page_size).explain()
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": plan}
    if endpoint == "employee_monthly":
        if not emp_code or not month:
            raise HTTPException(422, "emp_code and month are required")
        first_day = date.fromisoformat(f"{month}-01")
        last_day = (first_day.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)
        q = {"emp_code": emp_code, "date": {"$gte": first_day.isoformat(), "$lte": last_day.isoformat()}}
        plan = db.attendance_logs.find(q).explain()
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": plan}
    if endpoint == "department_summary":
        if not month:
            raise HTTPException(422, "month is required")
        q = {} if not department else {"department": department}
        plan = db.employees.find(q).explain()
        return {"endpoint": endpoint, "collection": "employees", "explain": plan}
    if endpoint == "late_leaderboard":
        if not month:
            raise HTTPException(422, "month is required")
        plan = db.attendance_logs.find({"late_minutes": {"$gt": 0}}).explain()
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": plan}
    if endpoint == "department_trend":
        if not department or not from_ or not to:
            raise HTTPException(422, "department, from and to are required")
        emp_codes = [e["emp_code"] for e in db.employees.find({"department": department})]
        plan = db.attendance_logs.find({"emp_code": {"$in": emp_codes}, "date": {"$gte": from_, "$lte": to}}).explain()
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": plan}
    raise HTTPException(404, "endpoint not found")
