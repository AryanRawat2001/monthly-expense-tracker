"""FastAPI backend + static dashboard for the Monthly Expense Tracker.

Run: uvicorn app:app --reload   then open http://127.0.0.1:8000
"""
import re
import threading
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

import db
import gmail_sync

VALID_TXN_TYPES = {"purchase", "refund", "card_payment", "transfer"}
_MONTH_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")

# ---------- local-only security guard ----------
# This is a single-user app holding financial data, served without auth on
# loopback. Two browser-borne attacks still reach loopback servers:
#   1. DNS rebinding: attacker.com resolves to 127.0.0.1, the victim's browser
#      happily reads our JSON — but sends Host: attacker.com. Rejecting foreign
#      Host headers kills this.
#   2. Cross-site request forgery: any webpage can fire a no-body POST at
#      http://127.0.0.1:8000/... (triggering syncs / paid AI runs). Browsers
#      attach an Origin header to cross-site POSTs — reject non-local origins.
_LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1", "[::1]"}
_UNSAFE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


def _hostname(host_header: str) -> str:
    h = (host_header or "").strip().lower()
    if h.startswith("["):                 # [::1]:8000
        return h.split("]", 1)[0] + "]"
    return h.split(":", 1)[0]

BASE = Path(__file__).parent
app = FastAPI(title="Monthly Expense Tracker")

db.init_db()


@app.middleware("http")
async def local_only_guard(request, call_next):
    if _hostname(request.headers.get("host", "")) not in _LOCAL_HOSTS:
        return JSONResponse(status_code=421, content={"detail": "Local access only"})

    if request.method in _UNSAFE_METHODS:
        origin = request.headers.get("origin")
        if origin:  # absent for curl/scripts; browsers always send it on POST
            if (urlsplit(origin).hostname or "").lower() not in _LOCAL_HOSTS:
                return JSONResponse(status_code=403,
                                    content={"detail": "Cross-origin request blocked"})

    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    response.headers.setdefault("X-Frame-Options", "DENY")
    # Defense-in-depth against injected markup: no external connections or
    # images, no framing. Chart.js (jsdelivr) + Google Fonts stay allowed;
    # 'unsafe-inline' is required by the dashboard's inline script/handlers.
    response.headers.setdefault(
        "Content-Security-Policy",
        "default-src 'self'; "
        "script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
        "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
        "font-src https://fonts.gstatic.com; "
        "img-src 'self' data:; "
        "connect-src 'self'; "
        "object-src 'none'; base-uri 'none'; form-action 'self'; "
        "frame-ancestors 'none'",
    )
    return response

# In-memory sync state for the progress bar. Single-user local app, so a module
# global guarded by a lock is sufficient.
# `feed` holds recent per-item results (newest last) for the live UI feed.
_sync_state = {
    "running": False, "done": 0, "total": 0, "phase": "idle",
    "result": None, "error": None, "feed": [], "cancelled": False,
}
_sync_lock = threading.Lock()
_cancel_event = threading.Event()


def _progress(done, total, phase, item=None):
    """Shared progress callback. `item` (optional dict) is appended to the live feed."""
    with _sync_lock:
        _sync_state.update(done=done, total=total, phase=phase)
        if item is not None:
            _sync_state["feed"].append(item)
            _sync_state["feed"] = _sync_state["feed"][-100:]  # cap


def _start_job(phase: str):
    """Reset state for a new background job. Returns False if one is already running."""
    with _sync_lock:
        if _sync_state["running"]:
            return False
        _cancel_event.clear()
        _sync_state.update(running=True, done=0, total=0, phase=phase,
                           result=None, error=None, feed=[], cancelled=False)
    return True


def _run_sync(days: int, max_messages: int):
    try:
        result = gmail_sync.sync(newer_than_days=days, max_messages=max_messages,
                                 progress=_progress)
        with _sync_lock:
            _sync_state.update(running=False, result=result, phase="Done")
    except Exception as e:
        with _sync_lock:
            _sync_state.update(running=False, error=str(e), phase="Error")


# ---------- models ----------
class SyncRequest(BaseModel):
    days: int = Field(90, ge=1, le=3650)
    max_messages: int = Field(2000, ge=1, le=20000)


class TxnPatch(BaseModel):
    category: str | None = None
    txn_type: str | None = None


class CategoryRule(BaseModel):
    pattern: str
    category: str
    priority: int = 5


class TransferRule(BaseModel):
    pattern: str
    note: str = ""


class Budget(BaseModel):
    scope: str            # category | account | total
    scope_value: str | None = None
    month_limit: float


# ---------- sync (background + progress) ----------
@app.post("/sync")
def run_sync(req: SyncRequest):
    if not _start_job("Starting…"):
        raise HTTPException(status_code=409, detail="A job is already in progress")
    threading.Thread(target=_run_sync, args=(req.days, req.max_messages),
                     daemon=True).start()
    return {"started": True}


@app.get("/sync/status")
def sync_status():
    with _sync_lock:
        return dict(_sync_state)


@app.post("/sync/stop")
def sync_stop():
    """Request cancellation. The running job stops after its current item."""
    _cancel_event.set()
    with _sync_lock:
        _sync_state["phase"] = "Stopping…"
    return {"stopping": True}


# ---------- data ----------
# Accept either a single ?month=YYYY-MM or a ?from=YYYY-MM&to=YYYY-MM range.
@app.get("/summary")
def summary(month: str | None = None,
            frm: str | None = Query(None, alias="from"),
            to: str | None = None):
    start, end = _resolve_range(month, frm, to)
    data = db.summary_for_range(start, end)
    data["trend"] = db.monthly_trend()
    return data


@app.get("/transactions")
def transactions(month: str | None = None,
                 frm: str | None = Query(None, alias="from"),
                 to: str | None = None):
    start, end = _resolve_range(month, frm, to)
    return db.transactions_for_range(start, end)


def _resolve_range(month, frm, to):
    """Normalize query params into (start, end). `from`/`to` win over `month`.
    Formats are validated because a malformed month (e.g. 2026-7) would silently
    return an empty result instead of an error."""
    if frm and to:
        pair = (frm, to)
    elif month:
        pair = (month, month)
    else:
        raise HTTPException(status_code=400, detail="Provide ?month= or ?from=&to=")
    for v in pair:
        if not _MONTH_RE.match(v):
            raise HTTPException(status_code=400,
                                detail=f"Invalid month {v!r} — expected YYYY-MM")
    return pair


@app.patch("/transactions/{txn_id}")
def patch_txn(txn_id: int, patch: TxnPatch):
    # An unknown txn_type would silently drop the row from EVERY total
    # (spend counts purchase/refund; "excluded" counts card_payment/transfer).
    if patch.txn_type is not None and patch.txn_type not in VALID_TXN_TYPES:
        raise HTTPException(status_code=400,
                            detail=f"txn_type must be one of {sorted(VALID_TXN_TYPES)}")
    category = patch.category
    if category is not None:
        category = category.strip()
        if not category or len(category) > 60:
            raise HTTPException(status_code=400, detail="Invalid category")
    db.update_txn(txn_id, category=category, txn_type=patch.txn_type)
    return {"ok": True}


@app.get("/accounts")
def accounts():
    return db.get_accounts()


@app.get("/months")
def months():
    return db.months_with_data()


@app.get("/insights")
def insights(month: str | None = None):
    """Estimator + analysis for one month (defaults to the newest with data):
    month-end projection, MoM movers, top merchants, recurring, duplicates."""
    if month is None:
        have = db.months_with_data()
        month = have[0] if have else None
    if month is None or not _MONTH_RE.match(month):
        raise HTTPException(status_code=400,
                            detail="Provide ?month=YYYY-MM (no data yet)" if month is None
                            else f"Invalid month {month!r} — expected YYYY-MM")
    return db.insights_for_month(month)


@app.get("/ai-usage")
def ai_usage():
    return db.ai_usage_total()


# ---------- rules ----------
@app.get("/rules")
def get_rules():
    return db.get_category_rules()


@app.post("/rules")
def add_rule(rule: CategoryRule):
    db.add_category_rule(rule.pattern, rule.category, rule.priority)
    return {"ok": True}


@app.delete("/rules/{rule_id}")
def del_rule(rule_id: int):
    db.delete_category_rule(rule_id)
    return {"ok": True}


@app.get("/transfer-rules")
def get_transfer_rules():
    return db.get_transfer_rules()


@app.post("/transfer-rules")
def add_transfer_rule(rule: TransferRule):
    db.add_transfer_rule(rule.pattern, rule.note)
    return {"ok": True}


@app.delete("/transfer-rules/{rule_id}")
def del_transfer_rule(rule_id: int):
    db.delete_transfer_rule(rule_id)
    return {"ok": True}


# ---------- income/ignore rules ----------
# Incoming credits matching these are IGNORED (salary/self-transfer/family) — not a
# deduction. Everything else coming in is treated as a payback that reduces spend.
class IncomeRule(BaseModel):
    pattern: str
    note: str = ""


@app.get("/income-rules")
def get_income_rules():
    return db.get_income_rules()


@app.post("/income-rules")
def add_income_rule(rule: IncomeRule):
    db.add_income_rule(rule.pattern, rule.note)
    return {"ok": True}


@app.delete("/income-rules/{rule_id}")
def del_income_rule(rule_id: int):
    db.delete_income_rule(rule_id)
    return {"ok": True}


# ---------- budgets ----------
@app.get("/budgets")
def budgets():
    return db.get_budgets()


@app.post("/budgets")
def add_budget(b: Budget):
    if b.month_limit <= 0:
        raise HTTPException(status_code=400, detail="Monthly limit must be greater than 0")
    if b.scope not in ("total", "category", "account"):
        raise HTTPException(status_code=400, detail="Invalid scope")
    if b.scope in ("category", "account") and not (b.scope_value or "").strip():
        raise HTTPException(status_code=400, detail=f"{b.scope} budget needs a value")
    db.add_budget(b.scope, (b.scope_value or "").strip() or None, b.month_limit)
    return {"ok": True}


@app.delete("/budgets/{budget_id}")
def del_budget(budget_id: int):
    db.delete_budget(budget_id)
    return {"ok": True}


# ---------- unparsed ----------
@app.get("/debug/unparsed")
def unparsed():
    return db.get_unparsed()


@app.post("/unparsed/{email_id}/retry-llm")
def retry_unparsed_llm(email_id: str):
    """On-demand AI extraction for a single unparsed email."""
    try:
        return gmail_sync.retry_with_llm(email_id)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"AI retry failed: {e}")


def _run_retry_all():
    try:
        result = gmail_sync.retry_all_unparsed(progress=_progress, cancel=_cancel_event)
        with _sync_lock:
            _sync_state.update(running=False, result=result,
                               phase="Stopped" if _cancel_event.is_set() else "Done",
                               cancelled=_cancel_event.is_set())
    except Exception as e:
        with _sync_lock:
            _sync_state.update(running=False, error=str(e), phase="Error")


@app.post("/unparsed/retry-all-llm")
def retry_all_unparsed_llm():
    """On-demand AI extraction over ALL unparsed emails (background + progress)."""
    if not _start_job("Starting AI…"):
        raise HTTPException(status_code=409, detail="A job is already in progress")
    threading.Thread(target=_run_retry_all, daemon=True).start()
    return {"started": True}


def _run_categorize():
    try:
        result = gmail_sync.categorize_unparsed_with_llm(progress=_progress)
        with _sync_lock:
            _sync_state.update(running=False, result=result, phase="Done")
    except Exception as e:
        with _sync_lock:
            _sync_state.update(running=False, error=str(e), phase="Error")


@app.post("/categorize-llm")
def categorize_llm():
    """On-demand AI categorization of all Uncategorized transactions (background)."""
    if not _start_job("Starting AI…"):
        raise HTTPException(status_code=409, detail="A job is already in progress")
    threading.Thread(target=_run_categorize, daemon=True).start()
    return {"started": True}


# ---------- frontend ----------
@app.get("/")
def index():
    return FileResponse(BASE / "static" / "index.html")


app.mount("/static", StaticFiles(directory=BASE / "static"), name="static")
