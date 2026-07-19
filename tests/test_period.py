"""Attribution month (`period`): rent paid a few days before the month starts
counts toward the NEXT month, while keeping the txn's true bank date."""
import db


def _ins(email_id, d, amount, merchant, category, txn_type="purchase"):
    assert db.insert_txn({
        "txn_date": d, "posted_at": d, "amount": amount, "direction": "debit",
        "txn_type": txn_type, "account_id": db.account_id_for_last4("9999"),
        "merchant_raw": merchant, "merchant_clean": merchant, "category": category,
        "source_email_id": email_id, "parsed_by": "regex", "confidence": 1.0,
        "raw_snippet": "t",
    })


def test_period_for_rules():
    assert db.period_for("2026-06-29", "Rent") == "2026-07"     # month-end -> next
    assert db.period_for("2026-07-01", "Rent") == "2026-07"     # month-start -> same
    assert db.period_for("2026-06-10", "Rent") == "2026-06"     # mid-month -> same
    assert db.period_for("2026-12-30", "Rent") == "2027-01"     # year rollover
    assert db.period_for("2026-06-29", "Groceries") == "2026-06"  # only Rent shifts
    assert db.period_for(None, "Rent") is None


def test_month_end_rent_counts_in_next_month(fresh_db):
    _ins("r1", "2026-06-29", 47250, "LANDLORD NAME", "Rent")
    _ins("g1", "2026-06-29", 500, "BLINKIT", "Groceries")
    assert db.summary_for_month("2026-06")["total_spend"] == 500.0
    july = db.summary_for_month("2026-07")
    assert july["total_spend"] == 47250.0
    # The row lists under July with its TRUE June date.
    rows = db.transactions_for_range("2026-07", "2026-07")
    assert len(rows) == 1
    assert rows[0]["txn_date"] == "2026-06-29"
    assert rows[0]["period"] == "2026-07"


def test_start_of_month_rent_stays(fresh_db):
    _ins("r1", "2026-07-01", 47250, "LANDLORD NAME", "Rent")
    assert db.summary_for_month("2026-07")["total_spend"] == 47250.0
    assert db.summary_for_month("2026-08")["total_spend"] == 0.0


def test_months_and_trend_use_period(fresh_db):
    _ins("r1", "2026-06-29", 1000, "LANDLORD NAME", "Rent")
    assert db.months_with_data() == ["2026-07"]
    trend = db.monthly_trend()
    assert trend == [{"month": "2026-07", "spend": 1000.0}]


def test_patch_category_moves_between_months(fresh_db):
    _ins("x1", "2026-06-29", 900, "SOMEONE", "Transport")
    assert db.summary_for_month("2026-06")["total_spend"] == 900.0
    txn_id = db.transactions_for_range("2026-06", "2026-06")[0]["id"]
    db.update_txn(txn_id, category="Rent")
    assert db.summary_for_month("2026-06")["total_spend"] == 0.0
    assert db.summary_for_month("2026-07")["total_spend"] == 900.0
    db.update_txn(txn_id, category="Transport")           # and back
    assert db.summary_for_month("2026-06")["total_spend"] == 900.0


def test_migration_backfills_existing_rows(fresh_db):
    _ins("r1", "2026-06-29", 1000, "LANDLORD NAME", "Rent")
    with db.get_conn() as conn:                           # simulate pre-period rows
        conn.execute("UPDATE transactions SET period = NULL")
    db.init_db()
    rows = db.transactions_for_range("2026-07", "2026-07")
    assert rows and rows[0]["period"] == "2026-07"


def test_insights_use_period(fresh_db):
    _ins("r1", "2026-06-29", 47250, "LANDLORD NAME", "Rent")
    _ins("s1", "2026-07-05", 300, "SWIGGY", "Food & Dining")
    ins = db.insights_for_month("2026-07", today="2026-07-10")
    assert ins["total"] == 47550.0
    assert {m["merchant"] for m in ins["top_merchants"]} == {"LANDLORD NAME", "SWIGGY"}
    # Early-paid rent counts from day 1 of the attributed month in the pace chart.
    days = ins["daily_cumulative"]["current"]
    assert days[0]["day"] == 1 and days[0]["cum"] == 47250.0
    assert days[-1]["cum"] == 47550.0


def test_recurring_uses_attribution_months(fresh_db):
    for i, d in enumerate(["2026-04-29", "2026-05-30", "2026-06-29"]):
        _ins(f"r{i}", d, 45000, "LANDLORD NAME", "Rent")
    rec = db.insights_for_month("2026-07", today="2026-07-10")["recurring"]
    rent = next(r for r in rec if r["merchant"] == "LANDLORD NAME")
    assert rent["months_seen"] == 3
    assert rent["active_this_month"] is True     # Jun-29 payment IS July's rent
