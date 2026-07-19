"""API behavior via TestClient: endpoints, validation, background-job machinery,
and the local-only security middleware."""
import threading
import time

import pytest
from fastapi.testclient import TestClient

import app as app_module
import db
import gmail_sync


@pytest.fixture
def client(fresh_db):
    # base_url gives Host: 127.0.0.1 so the localhost-only middleware allows it.
    with TestClient(app_module.app, base_url="http://127.0.0.1") as c:
        yield c


def _ins(email_id, date, amount, txn_type="purchase", direction="debit"):
    db.insert_txn({
        "txn_date": date, "posted_at": date, "amount": amount,
        "direction": direction, "txn_type": txn_type,
        "account_id": db.account_id_for_last4("1234"),
        "merchant_raw": "SHOP", "merchant_clean": "SHOP", "category": "Shopping",
        "source_email_id": email_id, "parsed_by": "regex",
        "confidence": 1.0, "raw_snippet": "x",
    })


def test_index_serves_dashboard(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "Expense" in r.text


def test_summary_month_and_range(client):
    _ins("a1", "2026-05-01", 100)
    _ins("a2", "2026-06-01", 50)
    one = client.get("/summary", params={"month": "2026-05"}).json()
    assert one["total_spend"] == 100.0
    rng = client.get("/summary", params={"from": "2026-05", "to": "2026-06"}).json()
    assert rng["total_spend"] == 150.0
    assert "trend" in rng


def test_summary_requires_params(client):
    assert client.get("/summary").status_code == 400


def test_summary_rejects_malformed_month(client):
    # 2026-7 (no zero padding) would silently return an empty month otherwise.
    assert client.get("/summary", params={"month": "2026-7"}).status_code == 400
    assert client.get("/summary", params={"from": "garbage", "to": "2026-06"}).status_code == 400


def test_transactions_and_months(client):
    _ins("a1", "2026-05-01", 100)
    txns = client.get("/transactions", params={"month": "2026-05"}).json()
    assert len(txns) == 1 and txns[0]["amount"] == 100.0
    assert client.get("/months").json() == ["2026-05"]


def test_patch_txn_valid(client):
    _ins("a1", "2026-05-01", 100)
    txn_id = client.get("/transactions", params={"month": "2026-05"}).json()[0]["id"]
    r = client.patch(f"/transactions/{txn_id}", json={"txn_type": "refund"})
    assert r.status_code == 200
    assert client.get("/transactions", params={"month": "2026-05"}).json()[0]["txn_type"] == "refund"


def test_patch_txn_rejects_invalid_type(client):
    """An arbitrary txn_type would silently vanish from every total."""
    _ins("a1", "2026-05-01", 100)
    txn_id = client.get("/transactions", params={"month": "2026-05"}).json()[0]["id"]
    r = client.patch(f"/transactions/{txn_id}", json={"txn_type": "banana"})
    assert r.status_code in (400, 422)
    assert client.get("/transactions", params={"month": "2026-05"}).json()[0]["txn_type"] == "purchase"


def test_patch_txn_rejects_blank_category(client):
    _ins("a1", "2026-05-01", 100)
    txn_id = client.get("/transactions", params={"month": "2026-05"}).json()[0]["id"]
    assert client.patch(f"/transactions/{txn_id}", json={"category": "   "}).status_code in (400, 422)


def test_rules_crud(client):
    r = client.post("/rules", json={"pattern": "starbucks", "category": "Food & Dining", "priority": 9})
    assert r.status_code == 200
    rules = client.get("/rules").json()
    mine = next(x for x in rules if x["pattern"] == "starbucks")
    assert client.delete(f"/rules/{mine['id']}").status_code == 200
    assert all(x["pattern"] != "starbucks" for x in client.get("/rules").json())


def test_income_rules_crud(client):
    assert client.post("/income-rules", json={"pattern": "acme corp", "note": "salary"}).status_code == 200
    rules = client.get("/income-rules").json()
    mine = next(x for x in rules if x["pattern"] == "acme corp")
    assert client.delete(f"/income-rules/{mine['id']}").status_code == 200


def test_budget_validation(client):
    assert client.post("/budgets", json={"scope": "total", "month_limit": -5}).status_code == 400
    assert client.post("/budgets", json={"scope": "weird", "month_limit": 5}).status_code == 400
    assert client.post("/budgets", json={"scope": "category", "month_limit": 5}).status_code == 400
    assert client.post("/budgets", json={"scope": "total", "month_limit": 5000}).status_code == 200


def test_ai_usage_endpoint(client):
    u = client.get("/ai-usage").json()
    assert u["calls"] == 0


def test_insights_endpoint(client):
    _ins("a1", "2026-05-01", 100)
    r = client.get("/insights", params={"month": "2026-05"})
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 100.0
    assert {"projection", "top_merchants", "recurring",
            "duplicate_suspects", "daily_cumulative"} <= body.keys()
    # defaults to newest month with data
    assert client.get("/insights").json()["month"] == "2026-05"
    assert client.get("/insights", params={"month": "garbage"}).status_code == 400


# ---------- background job machinery ----------

def test_sync_job_lifecycle(client, monkeypatch):
    release = threading.Event()

    def fake_sync(newer_than_days=90, max_messages=500, progress=None):
        if progress:
            progress(1, 2, "Reading transactions…")
        release.wait(timeout=5)
        return {"added": 3, "by_llm": 0, "unparsed": 1,
                "skipped_duplicates": 2, "processed": 6}

    monkeypatch.setattr(gmail_sync, "sync", fake_sync)
    assert client.post("/sync", json={"days": 30}).json() == {"started": True}
    # A second job while one runs must be refused.
    assert client.post("/sync", json={"days": 30}).status_code == 409
    release.set()
    for _ in range(100):
        st = client.get("/sync/status").json()
        if not st["running"]:
            break
        time.sleep(0.05)
    assert st["result"]["added"] == 3
    assert st["error"] is None


def test_sync_error_reported(client, monkeypatch):
    def boom(**kwargs):
        raise RuntimeError("gmail down")
    monkeypatch.setattr(gmail_sync, "sync", boom)
    client.post("/sync", json={"days": 30})
    for _ in range(100):
        st = client.get("/sync/status").json()
        if not st["running"]:
            break
        time.sleep(0.05)
    assert "gmail down" in st["error"]


# ---------- local-only security middleware ----------

def test_foreign_host_header_rejected(client):
    """DNS-rebinding protection: a browser resolving attacker.com -> 127.0.0.1
    still sends Host: attacker.com — refuse to serve it."""
    r = client.get("/months", headers={"host": "attacker.com"})
    assert r.status_code == 421


def test_extra_host_allowed_via_env(client, monkeypatch):
    """Tunnel/Tailscale hostnames are opt-in through EXPENSES_ALLOWED_HOSTS."""
    monkeypatch.setenv("EXPENSES_ALLOWED_HOSTS", "myapp.trycloudflare.com")
    assert client.get("/months", headers={"host": "myapp.trycloudflare.com"}).status_code == 200
    assert client.get("/months", headers={"host": "attacker.com"}).status_code == 421


def test_cross_origin_post_rejected(client):
    r = client.post("/sync/stop", headers={"Origin": "https://evil.example"})
    assert r.status_code == 403


def test_local_origin_post_allowed(client):
    r = client.post("/sync/stop", headers={"Origin": "http://127.0.0.1:8000"})
    assert r.status_code == 200


def test_security_headers_present(client):
    r = client.get("/")
    assert r.headers.get("X-Content-Type-Options") == "nosniff"
    csp = r.headers.get("Content-Security-Policy", "")
    assert "frame-ancestors 'none'" in csp
    assert "connect-src 'self'" in csp
