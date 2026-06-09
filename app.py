"""FastAPI backend + static dashboard for the Monthly Expense Tracker.

Run: uvicorn app:app --reload   then open http://127.0.0.1:8000
"""
import threading
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import db
import gmail_sync

BASE = Path(__file__).parent
app = FastAPI(title="Monthly Expense Tracker")

db.init_db()

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


def _run_sync(days: int):
    try:
        result = gmail_sync.sync(newer_than_days=days, progress=_progress)
        with _sync_lock:
            _sync_state.update(running=False, result=result, phase="Done")
    except Exception as e:
        with _sync_lock:
            _sync_state.update(running=False, error=str(e), phase="Error")


# ---------- models ----------
class SyncRequest(BaseModel):
    days: int = 90


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
    threading.Thread(target=_run_sync, args=(req.days,), daemon=True).start()
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
    """Normalize query params into (start, end). `from`/`to` win over `month`."""
    if frm and to:
        return frm, to
    if month:
        return month, month
    raise HTTPException(status_code=400, detail="Provide ?month= or ?from=&to=")


@app.patch("/transactions/{txn_id}")
def patch_txn(txn_id: int, patch: TxnPatch):
    db.update_txn(txn_id, category=patch.category, txn_type=patch.txn_type)
    return {"ok": True}


@app.get("/accounts")
def accounts():
    return db.get_accounts()


@app.get("/months")
def months():
    return db.months_with_data()


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
