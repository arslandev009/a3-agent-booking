"""HTTP API for the clinic agent.

    uvicorn clinicagent.api:build_app --factory --port 8000

Public:   POST /chat           {message, session_id?} -> {session_id, reply}
          GET  /health
Staff:    GET  /approvals                       pending cancellations      (header X-Staff-Key)
          POST /approvals/{id}/approve | /reject                           (header X-Staff-Key)

Design notes
  * Each session has its own agent (own conversation); all sessions share one ClinicTools (one calendar).
  * Session ids are random server-side tokens. An unknown or expired id silently starts a new session.
  * Staff endpoints are disabled unless STAFF_API_KEY is set. The model can never reach them (G6).
  * Message text is never logged (it contains names and phone numbers); only ids, step counts, guards, latency.
  * SQLite + an in-process lock means ONE worker process. Scale-out needs a real database (see README).
  * GET / serves the demo UI (src/clinicagent/web/index.html), same origin as the API.
  * Errors never leak internals: transient LLM failures become 503 "busy", anything else a generic 500.
"""
from __future__ import annotations

import hmac
import logging
import os
import threading
import time
import uuid
from collections import OrderedDict, defaultdict, deque
from typing import Any, Callable

from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from .agent import ClinicAgent
from .judge import Judge
from .tools import ClinicTools

log = logging.getLogger("clinicagent.api")

BUSY_MESSAGE = "The assistant is busy right now. Please try again in a moment."
DEMO_LIMIT_MESSAGE = ("This free live demo has reached its limit for today, so the assistant is paused until tomorrow (UTC). "
                      "Please watch the demo video instead.")


class ChatIn(BaseModel):
    message: str = Field(min_length=1, max_length=1000)
    session_id: str | None = Field(default=None, max_length=64)


class ChatOut(BaseModel):
    session_id: str
    reply: str
    debug: dict | None = None


class _Session:
    def __init__(self, agent: ClinicAgent) -> None:
        self.agent = agent
        self.lock = threading.Lock()  # one message at a time per conversation
        self.last_used = time.monotonic()


class _RateLimiter:
    """Sliding window per client key, in memory (fine for one process)."""

    def __init__(self, limit: int, window_s: float = 60.0) -> None:
        self.limit, self.window = limit, window_s
        self.hits: dict[str, deque] = defaultdict(deque)
        self.lock = threading.Lock()

    def allow(self, key: str) -> bool:
        if self.limit <= 0:
            return True
        now = time.monotonic()
        with self.lock:
            q = self.hits[key]
            while q and now - q[0] > self.window:
                q.popleft()
            if len(q) >= self.limit:
                return False
            q.append(now)
            return True


class _DailyCap:
    """Messages per UTC day: one global budget plus an optional per-client budget (0 = off). In memory only: it resets
    when the process restarts, so it is a safety net for a small free host, not an accounting system (see README)."""

    def __init__(self, global_cap: int, per_client_cap: int = 0, today: Callable[[], str] | None = None) -> None:
        self.global_cap, self.per_client_cap = global_cap, per_client_cap
        self._today = today or (lambda: time.strftime("%Y-%m-%d", time.gmtime()))
        self.day = self._today()
        self.total = 0
        self.by_client: dict[str, int] = defaultdict(int)
        self.lock = threading.Lock()

    def allow(self, client: str) -> bool:
        if self.global_cap <= 0 and self.per_client_cap <= 0:
            return True
        with self.lock:
            day = self._today()
            if day != self.day:
                self.day, self.total = day, 0
                self.by_client.clear()
            if self.global_cap > 0 and self.total >= self.global_cap:
                return False
            if self.per_client_cap > 0 and self.by_client[client] >= self.per_client_cap:
                return False
            self.total += 1
            self.by_client[client] += 1
            return True


def client_ip(request: Request, proxy_hops: int) -> str:
    """The caller's address. Behind N trusted proxies the real client is the Nth entry from the RIGHT of
    X-Forwarded-For (entries further left can be forged by the caller). proxy_hops=0 trusts only the socket address."""
    direct = request.client.host if request.client else "unknown"
    if proxy_hops <= 0:
        return direct
    parts = [p.strip() for p in request.headers.get("x-forwarded-for", "").split(",") if p.strip()]
    return parts[-proxy_hops] if len(parts) >= proxy_hops else direct


def create_app(
    llm: Any,
    tools: ClinicTools,
    *,
    staff_key: str | None = None,
    use_judge: bool = True,
    debug: bool = False,
    max_sessions: int = 500,
    session_ttl_s: float = 3600.0,
    rate_limit_per_min: int = 20,
    agent_factory: Callable[[Any, ClinicTools], ClinicAgent] | None = None,
    cors_origins: list[str] | None = None,
    daily_cap: int = 0,
    daily_cap_per_client: int = 0,
    proxy_hops: int = 0,
    demo_video_url: str | None = None,
    today: Callable[[], str] | None = None,
) -> FastAPI:
    app = FastAPI(title="BrightSmile Dental assistant (sample data only)", version="0.1.0")
    if cors_origins:  # e.g. the static UI hosted on another domain; never "*" together with staff endpoints
        app.add_middleware(CORSMiddleware, allow_origins=cors_origins, allow_methods=["GET", "POST"],
                           allow_headers=["Content-Type", "X-Staff-Key"])
    sessions: OrderedDict[str, _Session] = OrderedDict()
    sessions_lock = threading.Lock()
    limiter = _RateLimiter(rate_limit_per_min)
    cap = _DailyCap(daily_cap, daily_cap_per_client, today)
    limit_message = DEMO_LIMIT_MESSAGE + (f" {demo_video_url}" if demo_video_url else "")

    def make_agent() -> ClinicAgent:
        if agent_factory:
            return agent_factory(llm, tools)
        return ClinicAgent(llm, tools, judge=Judge(llm) if use_judge else None)

    def get_session(session_id: str | None) -> tuple[str, _Session]:
        now = time.monotonic()
        with sessions_lock:
            for sid in [k for k, v in sessions.items() if now - v.last_used > session_ttl_s]:
                del sessions[sid]
            if session_id and session_id in sessions:
                sessions.move_to_end(session_id)
                return session_id, sessions[session_id]
            sid = uuid.uuid4().hex
            sessions[sid] = _Session(make_agent())
            while len(sessions) > max_sessions:
                sessions.popitem(last=False)
            return sid, sessions[sid]

    def require_staff(x_staff_key: str | None = Header(default=None)) -> None:
        if not staff_key:
            raise HTTPException(503, "Staff access is not configured.")
        if not x_staff_key or not hmac.compare_digest(x_staff_key.encode(), staff_key.encode()):
            raise HTTPException(401, "Invalid staff key.")

    @app.get("/", include_in_schema=False)
    def home() -> FileResponse:
        return FileResponse(Path(__file__).parent / "web" / "index.html", media_type="text/html")

    @app.get("/health")
    def health() -> dict:
        out = {"status": "ok", "sessions": len(sessions), "judge": use_judge,
               "demo_paused": bool(cap.global_cap) and cap.total >= cap.global_cap}
        if hasattr(llm, "status"):  # provider failover: which providers are ready or cooling down (names only)
            out["llm"] = llm.status()
        return out

    @app.post("/chat", response_model=ChatOut, response_model_exclude_none=True)
    def chat(body: ChatIn, request: Request) -> ChatOut:
        client = client_ip(request, proxy_hops)
        if not limiter.allow(client):
            raise HTTPException(429, "Too many messages. Please slow down.")
        if not cap.allow(client):  # checked before any model call, so a paused demo spends no provider quota
            raise HTTPException(503, limit_message)
        sid, sess = get_session(body.session_id)
        started = time.perf_counter()
        with sess.lock:
            sess.last_used = time.monotonic()
            try:
                turn = sess.agent.reply(body.message)
            except Exception as exc:  # noqa: BLE001
                if ClinicAgent._retryable(exc):
                    log.warning("session=%s llm_unavailable=%s", sid[:8], type(exc).__name__)
                    raise HTTPException(503, BUSY_MESSAGE) from None
                log.exception("session=%s agent_error", sid[:8])
                raise HTTPException(500, "Something went wrong. Please try again.") from None
        log.info("session=%s steps=%d guards=%s tools=%s ms=%d", sid[:8], turn.steps, turn.guards,
                 [c.name for c in turn.tool_calls], (time.perf_counter() - started) * 1000)
        out = ChatOut(session_id=sid, reply=turn.reply)
        if debug:
            out.debug = {"guards": turn.guards, "tool_calls": [{"name": c.name, "ok": c.result.get("ok")}
                                                               for c in turn.tool_calls]}
        return out

    @app.get("/approvals", dependencies=[Depends(require_staff)])
    def approvals() -> dict:
        return {"pending": tools.pending_cancellations()}

    def _resolve(appointment_id: int, approve: bool) -> dict:
        result = tools.approve_cancellation(appointment_id) if approve else tools.reject_cancellation(appointment_id)
        if not result.get("ok"):
            raise HTTPException(404 if result.get("error") == "not_found" else 409, result.get("message", "Failed."))
        log.info("staff %s appointment=%d", "approved" if approve else "rejected", appointment_id)
        return result

    @app.post("/approvals/{appointment_id}/approve", dependencies=[Depends(require_staff)])
    def approve(appointment_id: int) -> dict:
        return _resolve(appointment_id, True)

    @app.post("/approvals/{appointment_id}/reject", dependencies=[Depends(require_staff)])
    def reject(appointment_id: int) -> dict:
        return _resolve(appointment_id, False)

    return app


def build_app() -> FastAPI:
    """Production entry point: configuration from environment variables.

    LLM_PROVIDER / provider keys   read by llm-client (.env at the ai-studio root, or real env vars in Docker)
    LLM_PROVIDERS      ordered list, e.g. "groq,gemini": automatic failover when a provider's quota is used up (see failover.py)
    CLINIC_DB          SQLite path (default clinic.db)
    STAFF_API_KEY      enables the /approvals endpoints
    CLINIC_JUDGE       on|off (default on)
    CLINIC_DEBUG       1 adds the tool/guard names used (no message text, no arguments) to /chat; feeds the UI trace panel
    CLINIC_DAILY_CAP   max chat messages per UTC day for the whole server (0 = unlimited); the demo pauses after that
    CLINIC_DAILY_CAP_PER_CLIENT  max chat messages per UTC day per client address (0 = unlimited)
    CLINIC_PROXY_HOPS  number of trusted reverse proxies in front (Render: 1) so limits use the real client address
    CLINIC_DEMO_VIDEO_URL  link added to the "demo paused" message
    CLINIC_CORS_ORIGINS  comma-separated origins allowed to call the API from a browser (only if the UI is hosted elsewhere)
    """
    from llm_client.client import LLM  # imported here so tests and tools do not need the package

    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(name)s %(message)s")
    providers = [p.strip() for p in os.getenv("LLM_PROVIDERS", "").split(",") if p.strip()]
    if len(providers) > 1:  # e.g. "groq,gemini": when one runs out of quota the next one answers
        from .failover import FailoverLLM
        llm = FailoverLLM([LLM(p) for p in providers])
    else:
        llm = LLM((providers[0] if providers else None) or os.getenv("LLM_PROVIDER") or None)
    tools = ClinicTools(os.getenv("CLINIC_DB", "clinic.db"))
    return create_app(
        llm,
        tools,
        staff_key=os.getenv("STAFF_API_KEY"),
        use_judge=os.getenv("CLINIC_JUDGE", "on").lower() != "off",
        debug=os.getenv("CLINIC_DEBUG") == "1",
        rate_limit_per_min=int(os.getenv("CLINIC_RATE_LIMIT", "20")),
        daily_cap=int(os.getenv("CLINIC_DAILY_CAP", "0")),
        daily_cap_per_client=int(os.getenv("CLINIC_DAILY_CAP_PER_CLIENT", "0")),
        proxy_hops=int(os.getenv("CLINIC_PROXY_HOPS", "0")),
        demo_video_url=os.getenv("CLINIC_DEMO_VIDEO_URL") or None,
        cors_origins=[o.strip() for o in os.getenv("CLINIC_CORS_ORIGINS", "").split(",") if o.strip()] or None,
    )
