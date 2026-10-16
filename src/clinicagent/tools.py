"""Deterministic tool layer for the clinic agent.

Design rules (lesson from the Learning Lab: guards live in CODE, not in the prompt):
- every tool returns a plain dict: {"ok": True, ...} or {"ok": False, "error": <code>, "message": ...}
- booking is idempotent (idempotency_key) and re-validates the slot at write time
- the agent can only REQUEST a cancellation; a human approves or rejects it (not an agent tool)
- the clock is injectable, so evals are reproducible
"""
from __future__ import annotations

import re
import sqlite3
import threading
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any, Callable

from .db import connect

OPEN = time(9, 0)
CLOSE = time(18, 0)
LUNCH_START = time(13, 0)
LUNCH_END = time(14, 0)
GRID_MINUTES = 30
MIN_LEAD = timedelta(minutes=60)
MAX_DAYS_AHEAD = 60
MAX_SLOTS_RETURNED = 20  # a day has at most 16 slots, so nothing is hidden
CLOSED_WEEKDAYS = {6}  # Sunday

SERVICES: dict[str, int] = {  # service -> minutes
    "checkup": 30,
    "cleaning": 30,
    "filling": 60,
    "root_canal_consult": 30,
    "whitening_consult": 30,
}
ACTIVE = ("booked", "cancel_pending")
DEFAULT_FAQ = Path(__file__).resolve().parents[2] / "data" / "clinic_faq.md"

STOPWORDS = set(
    "what are your you the for and can how much does do is i my me should take there with have get "
    "from this that please would like a an of to in on at it be any about tell".split()
)


def _err(code: str, message: str, **extra: Any) -> dict:
    return {"ok": False, "error": code, "message": message, **extra}


def _fmt(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M")


def _norm_phone(phone: str) -> str:
    return re.sub(r"[^\d]", "", phone or "")


def _phone_key(phone: str) -> str:
    """Identity key for a phone number: its last 10 digits. The same person writes one number as
    "0300 1234567", "+92 300 1234567" or "0092-300-1234567"; all three must match. A clinic serves one
    country, so ignoring the country-code and trunk-zero prefix cannot confuse two patients in practice."""
    return _norm_phone(phone)[-10:]


START_FORMATS = ("%Y-%m-%dT%H:%M", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S")


def normalize_start(raw: str) -> str | None:
    """Accept '2026-10-15T10:30', '...T10:30:00', '2026-10-15 10:30' and return 'YYYY-MM-DDTHH:MM' (else None).
    Models often append seconds; rejecting that made one model retry the same failing call until max steps."""
    for fmt in START_FORMATS:
        try:
            return datetime.strptime((raw or "").strip(), fmt).strftime("%Y-%m-%dT%H:%M")
        except (TypeError, ValueError):
            continue
    return None


def _valid_name(name: str) -> bool:
    """Real names only: letters (any script, e.g. Urdu), spaces, dot, apostrophe, hyphen."""
    if not 2 <= len(name) <= 80 or not name[0].isalpha() or "--" in name:
        return False
    if sum(ch.isalpha() for ch in name) < 2:
        return False
    return all(ch.isalpha() or ch in " .'-" for ch in name)


def _tokens(text: str) -> set[str]:
    out = set()
    for w in re.findall(r"[a-z]+", text.lower()):
        if len(w) < 3 or w in STOPWORDS:
            continue
        if w.endswith("ies") and len(w) > 4:
            w = w[:-3] + "y"  # emergencies -> emergency
        elif w.endswith("s") and len(w) > 3:
            w = w[:-1]
        out.add(w)
    return out


class ClinicTools:
    def __init__(
        self,
        db_path: str = ":memory:",
        now: Callable[[], datetime] | None = None,
        faq_path: Path | str | None = None,
    ) -> None:
        self.conn = connect(db_path)
        self.version = 0  # bumped on every change to the calendar; the agent uses it to tell stale availability
        self._lock = threading.RLock()  # one writer at a time: check-then-insert must not interleave across threads
        self.now = now or datetime.now
        self.faq_sections = self._load_faq(Path(faq_path) if faq_path else DEFAULT_FAQ)

    # ------------------------------------------------------------------ helpers
    def _load_faq(self, path: Path) -> list[tuple[str, str]]:
        if not path.exists():
            return []
        sections: list[tuple[str, str]] = []
        title, body = None, []
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.startswith("## "):
                if title:
                    sections.append((title, " ".join(body).strip()))
                title, body = line[3:].strip(), []
            elif title and line.strip():
                body.append(line.strip())
        if title:
            sections.append((title, " ".join(body).strip()))
        return sections

    def _busy(self, day: date) -> list[tuple[datetime, datetime]]:
        rows = self.conn.execute(
            "SELECT start_ts, end_ts FROM appointments "
            "WHERE status IN ('booked','cancel_pending') AND substr(start_ts,1,10)=?",
            (day.isoformat(),),
        ).fetchall()
        return [(datetime.fromisoformat(r["start_ts"]), datetime.fromisoformat(r["end_ts"])) for r in rows]

    def _free_starts(self, day: date, service: str) -> list[datetime]:
        if day.weekday() in CLOSED_WEEKDAYS:
            return []
        dur = timedelta(minutes=SERVICES[service])
        now = self.now()
        busy = self._busy(day)
        lunch_s = datetime.combine(day, LUNCH_START)
        lunch_e = datetime.combine(day, LUNCH_END)
        close = datetime.combine(day, CLOSE)
        cur = datetime.combine(day, OPEN)
        out: list[datetime] = []
        while cur + dur <= close:
            end = cur + dur
            ok = cur >= now + MIN_LEAD
            ok = ok and not (cur < lunch_e and end > lunch_s)
            ok = ok and not any(cur < b_end and end > b_start for b_start, b_end in busy)
            if ok:
                out.append(cur)
            cur += timedelta(minutes=GRID_MINUTES)
        return out

    def _check_date(self, raw: str) -> tuple[date | None, dict | None]:
        try:
            day = date.fromisoformat(raw)
        except (TypeError, ValueError):
            return None, _err("invalid_date", "Date must be YYYY-MM-DD.")
        today = self.now().date()
        if day < today:
            return None, _err("date_in_past", "That date has already passed.")
        if (day - today).days > MAX_DAYS_AHEAD:
            return None, _err("date_too_far", f"We only book up to {MAX_DAYS_AHEAD} days ahead.")
        return day, None

    # -------------------------------------------------------------------- tools
    def check_availability(self, date: str, service: str, part_of_day: str | None = None) -> dict:
        if service not in SERVICES:
            return _err("unknown_service", "Unknown service.", valid_services=sorted(SERVICES))
        day, err = self._check_date(date)
        if err:
            return err
        if day.weekday() in CLOSED_WEEKDAYS:
            return _err("clinic_closed", "The clinic is closed on Sundays.", date=date)
        starts = self._free_starts(day, service)
        if part_of_day == "morning":
            starts = [s for s in starts if s.hour < 13]
        elif part_of_day == "afternoon":
            starts = [s for s in starts if s.hour >= 14]
        elif part_of_day not in (None, ""):
            return _err("invalid_part_of_day", "part_of_day must be 'morning' or 'afternoon'.")
        return {
            "ok": True,
            "date": date,
            "service": service,
            "duration_minutes": SERVICES[service],
            "slots": [s.strftime("%H:%M") for s in starts[:MAX_SLOTS_RETURNED]],
            "total_free": len(starts),
        }

    def book_appointment(
        self, patient_name: str, phone: str, service: str, start: str, idempotency_key: str
    ) -> dict:
        if not idempotency_key:
            return _err("missing_idempotency_key", "idempotency_key is required.")
        existing = self.conn.execute(
            "SELECT * FROM appointments WHERE idempotency_key=?", (idempotency_key,)
        ).fetchone()
        if existing:
            return {"ok": True, "idempotent_replay": True, **self._row_public(existing)}
        name = (patient_name or "").strip()
        if not _valid_name(name):
            return _err("invalid_name", "Please provide the patient's real full name (letters only).")
        digits = _norm_phone(phone)
        if not 10 <= len(digits) <= 15:
            return _err("invalid_phone", "Please provide a valid phone number.")
        if service not in SERVICES:
            return _err("unknown_service", "Unknown service.", valid_services=sorted(SERVICES))
        norm = normalize_start(start)
        if norm is None:
            return _err("invalid_start", "start must look like 2026-10-15T10:30 (date, T, 24-hour time).")
        start_dt = datetime.strptime(norm, "%Y-%m-%dT%H:%M")
        _, err = self._check_date(start_dt.date().isoformat())
        if err:
            return err
        free = self._free_starts(start_dt.date(), service)
        if start_dt not in free:
            alts = [s.strftime("%H:%M") for s in free[:3]]
            return _err("slot_unavailable", "That time is not available.", alternatives=alts)
        end_dt = start_dt + timedelta(minutes=SERVICES[service])
        try:
            cur = self.conn.execute(
                "INSERT INTO appointments(patient_name, phone, service, start_ts, end_ts, status, "
                "idempotency_key, created_at) VALUES (?,?,?,?,?, 'booked', ?, ?)",
                (name, digits, service, _fmt(start_dt), _fmt(end_dt), idempotency_key, _fmt(self.now())),
            )
            self.conn.commit()
        except sqlite3.IntegrityError:  # concurrent replay of the same key
            row = self.conn.execute(
                "SELECT * FROM appointments WHERE idempotency_key=?", (idempotency_key,)
            ).fetchone()
            return {"ok": True, "idempotent_replay": True, **self._row_public(row)}
        row = self.conn.execute("SELECT * FROM appointments WHERE id=?", (cur.lastrowid,)).fetchone()
        self.version += 1
        return {"ok": True, "idempotent_replay": False, **self._row_public(row)}

    def has_idempotency_key(self, key: str) -> bool:
        with self._lock:
            return self.conn.execute("SELECT 1 FROM appointments WHERE idempotency_key=?", (key,)).fetchone() is not None

    def cancel_appointment(self, appointment_id: int, phone: str, reason: str = "") -> dict:
        """Only REQUESTS a cancellation. Staff must approve (approve_cancellation)."""
        row = self.conn.execute("SELECT * FROM appointments WHERE id=?", (appointment_id,)).fetchone()
        # same message for unknown id and wrong phone: do not leak who is booked
        if not row or _phone_key(row["phone"]) != _phone_key(phone):
            return _err("not_found", "No appointment matches that id and phone number.")
        if row["status"] == "cancelled":
            return _err("already_cancelled", "This appointment is already cancelled.")
        if row["status"] == "cancel_pending":
            return {"ok": True, "status": "pending_approval", "appointment_id": row["id"], "already_pending": True}
        self.conn.execute("UPDATE appointments SET status='cancel_pending' WHERE id=?", (row["id"],))
        self.conn.commit()
        self.version += 1
        return {
            "ok": True,
            "status": "pending_approval",
            "appointment_id": row["id"],
            "message": "Cancellation requested. Clinic staff must confirm it.",
        }

    # words that all mean "money": a patient says "how much", the FAQ says "fees" and "costs"
    DURATION_WORDS = {"long", "duration", "minute", "take", "length"}
    MONEY_WORDS = {"price", "cost", "fee", "charge", "pay", "expensive", "cheap", "afford", "much"}

    def lookup_appointment(self, appointment_id: int, phone: str) -> dict:
        """Read-only status check. Same identity rule as cancel (id + phone), same error for 'no such id' and 'wrong
        phone', and only the appointment itself is returned (no name, no phone)."""
        row = self.conn.execute("SELECT * FROM appointments WHERE id=?", (appointment_id,)).fetchone()
        if not row or _phone_key(row["phone"]) != _phone_key(phone):
            return _err("not_found", "No appointment matches that id and phone number.")
        return {"ok": True, "appointment_id": row["id"], "service": row["service"], "start": row["start_ts"],
                "end": row["end_ts"], "status": row["status"]}

    def get_clinic_info(self, query: str) -> dict:
        """Keyword lookup in the FAQ. Several sections can fit a question ("how much is a cleaning" matches both the
        duration and the fee section), so close runners-up are returned too and the model picks the right one."""
        q = _tokens(query)
        if q & self.MONEY_WORDS or re.search(r"how\s+much", query or "", re.I):
            q |= {"price", "cost", "fee"}
        if q & self.DURATION_WORDS:
            q |= {"duration", "minute"}
        scored = []
        for title, body in self.faq_sections:
            tb = _tokens(title) | _tokens(body)
            score = 2 * len(q & _tokens(title)) + len(q & _tokens(body))
            if "price" in q and ("fee" in tb or "cost" in tb or "price" in tb):
                score += 1  # a section about money beats one that only names the same services
            scored.append((score, title, body))
        scored.sort(key=lambda x: -x[0])
        if not scored or scored[0][0] < 2:
            return _err("not_found", "No matching information in the clinic FAQ.")
        top = scored[0][0]
        out = {"ok": True, "topic": scored[0][1], "answer": scored[0][2]}
        close = [{"topic": t, "answer": b} for sc, t, b in scored[1:3] if sc >= 2 and sc * 2 >= top]
        if close:
            out["also_possibly_relevant"] = close
        return out

    # ------------------------------------------- human-side actions (NOT agent tools)
    def approve_cancellation(self, appointment_id: int) -> dict:
        return self._resolve(appointment_id, "cancelled")

    def reject_cancellation(self, appointment_id: int) -> dict:
        return self._resolve(appointment_id, "booked")

    def _resolve(self, appointment_id: int, new_status: str) -> dict:
        with self._lock:
            return self._resolve_locked(appointment_id, new_status)

    def _resolve_locked(self, appointment_id: int, new_status: str) -> dict:
        row = self.conn.execute("SELECT * FROM appointments WHERE id=?", (appointment_id,)).fetchone()
        if not row:
            return _err("not_found", "Unknown appointment.")
        if row["status"] != "cancel_pending":
            return _err("not_pending", "Appointment has no pending cancellation.")
        self.conn.execute("UPDATE appointments SET status=? WHERE id=?", (new_status, appointment_id))
        self.conn.commit()
        self.version += 1
        return {"ok": True, "appointment_id": appointment_id, "status": new_status}

    # ----------------------------------------------------------- state + dispatch
    @staticmethod
    def _row_public(row: sqlite3.Row) -> dict:
        return {
            "appointment_id": row["id"],
            "patient_name": row["patient_name"],
            "service": row["service"],
            "start": row["start_ts"],
            "end": row["end_ts"],
            "status": row["status"],
        }

    def state(self) -> list[dict]:
        """Full DB snapshot, used by the eval harness to score final state."""
        with self._lock:
            rows = self.conn.execute("SELECT * FROM appointments ORDER BY id").fetchall()
        return [self._row_public(r) for r in rows]

    def pending_cancellations(self) -> list[dict]:
        """What clinic staff must decide on (no phone numbers: staff match by appointment id)."""
        return [r for r in self.state() if r["status"] == "cancel_pending"]

    def seed(self, items: list[dict]) -> None:
        """Insert pre-existing appointments for eval scenarios (bypasses lead-time rules)."""
        for i, it in enumerate(items):
            start = datetime.fromisoformat(it["start"])
            end = start + timedelta(minutes=SERVICES[it["service"]])
            self.conn.execute(
                "INSERT INTO appointments(patient_name, phone, service, start_ts, end_ts, status, "
                "idempotency_key, created_at) VALUES (?,?,?,?,?,?,?,?)",
                (it["name"], _norm_phone(it["phone"]), it["service"], _fmt(start), _fmt(end),
                 it.get("status", "booked"), f"seed-{i}", _fmt(self.now())),
            )
        self.conn.commit()

    def dispatch(self, name: str, args: dict) -> dict:
        fn = {
            "check_availability": self.check_availability,
            "book_appointment": self.book_appointment,
            "cancel_appointment": self.cancel_appointment,
            "lookup_appointment": self.lookup_appointment,
            "get_clinic_info": self.get_clinic_info,
        }.get(name)
        if fn is None:
            return _err("unknown_tool", f"No such tool: {name}")
        try:
            with self._lock:
                return fn(**args)
        except TypeError as e:
            return _err("invalid_arguments", str(e))


# OpenAI-format function schemas (used by the agent loop in A3.2)
TOOL_SCHEMAS: list[dict] = [
    {
        "type": "function",
        "function": {
            "name": "check_availability",
            "description": "List free appointment start times for a date and service. Always call before booking.",
            "parameters": {
                "type": "object",
                "properties": {
                    "date": {"type": "string", "description": "YYYY-MM-DD"},
                    "service": {"type": "string", "enum": sorted(SERVICES)},
                    "part_of_day": {"type": "string", "enum": ["morning", "afternoon"]},
                },
                "required": ["date", "service"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "book_appointment",
            "description": "Book a slot that check_availability returned. Needs the patient's name and phone.",
            "parameters": {
                "type": "object",
                "properties": {
                    "patient_name": {"type": "string"},
                    "phone": {"type": "string"},
                    "service": {"type": "string", "enum": sorted(SERVICES)},
                    "start": {"type": "string", "description": "YYYY-MM-DDTHH:MM"},
                    "idempotency_key": {"type": "string", "description": "Unique per booking intent; reuse on retry."},
                },
                "required": ["patient_name", "phone", "service", "start", "idempotency_key"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "lookup_appointment",
            "description": "Check the date, time and status of an existing appointment. Needs the appointment id and the phone number used to book it.",
            "parameters": {
                "type": "object",
                "properties": {"appointment_id": {"type": "integer"}, "phone": {"type": "string"}},
                "required": ["appointment_id", "phone"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "cancel_appointment",
            "description": "Request cancellation. Staff must approve; never tell the patient it is already cancelled.",
            "parameters": {
                "type": "object",
                "properties": {
                    "appointment_id": {"type": "integer"},
                    "phone": {"type": "string"},
                    "reason": {"type": "string"},
                },
                "required": ["appointment_id", "phone"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_clinic_info",
            "description": "Look up hours, location, fees, payment, policies, emergencies from the clinic FAQ.",
            "parameters": {
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
            },
        },
    },
]
