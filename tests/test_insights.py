"""The estimator/insights pack: projection math, MoM movers, top merchants,
recurring detection, duplicate suspects, daily pace series."""
import db


def _ins(email_id, d, amount, merchant, txn_type="purchase", direction="debit",
         last4="1234", category="Shopping"):
    assert db.insert_txn({
        "txn_date": d, "posted_at": d, "amount": amount, "direction": direction,
        "txn_type": txn_type, "account_id": db.account_id_for_last4(last4),
        "merchant_raw": merchant, "merchant_clean": merchant, "category": category,
        "source_email_id": email_id, "parsed_by": "regex", "confidence": 1.0,
        "raw_snippet": "t",
    })


def _seed(fresh_db):
    # April
    _ins("a1", "2026-04-01", 25000, "MR LANDLORD", category="Rent")
    _ins("a2", "2026-04-05", 649, "NETFLIX", category="Entertainment")
    _ins("a3", "2026-04-10", 300, "SWIGGY", category="Food & Dining")
    # May
    _ins("b1", "2026-05-01", 25000, "MR LANDLORD", category="Rent")
    _ins("b2", "2026-05-05", 649, "NETFLIX", category="Entertainment")
    _ins("b3", "2026-05-12", 450, "SWIGGY", category="Food & Dining")
    _ins("b4", "2026-05-20", 5000, "AMAZON", category="Shopping")
    # June (has a duplicate-alert pair)
    _ins("c1", "2026-06-01", 25000, "MR LANDLORD", category="Rent")
    _ins("c2", "2026-06-05", 649, "NETFLIX", category="Entertainment")
    _ins("c3", "2026-06-15", 250, "UBER", category="Transport")
    _ins("c4", "2026-06-15", 250, "UBER", category="Transport")
    # July (the "current" month; today fixed to 2026-07-10 in tests)
    _ins("d1", "2026-07-03", 600, "AMAZON")
    _ins("d2", "2026-07-08", 400, "SWIGGY", category="Food & Dining")
    _ins("d3", "2026-07-09", 100, "AMAZON", txn_type="refund", direction="credit")


def test_projection_for_current_month(fresh_db):
    _seed(fresh_db)
    ins = db.insights_for_month("2026-07", today="2026-07-10")
    p = ins["projection"]
    assert ins["total"] == 900.0                      # 600+400-100
    assert p["is_current"] is True
    assert p["days_elapsed"] == 10
    assert p["days_in_month"] == 31
    assert p["daily_rate"] == 90.0
    assert p["projected"] == 900.0 + 90.0 * 21        # run-rate extrapolation
    # typical = median of Apr(25949), May(31099), Jun(26149)
    assert p["typical_month"] == 26149.0


def test_projection_past_month_is_actual(fresh_db):
    _seed(fresh_db)
    ins = db.insights_for_month("2026-06", today="2026-07-10")
    assert ins["projection"]["is_current"] is False
    assert ins["projection"]["projected"] == ins["total"] == 26149.0


def test_mom_delta_and_pct(fresh_db):
    _seed(fresh_db)
    ins = db.insights_for_month("2026-06", today="2026-07-10")
    assert ins["prev_total"] == 31099.0
    assert ins["mom_delta"] == round(26149.0 - 31099.0, 2)
    assert ins["mom_pct"] == -15.9


def test_category_movers(fresh_db):
    _seed(fresh_db)
    ins = db.insights_for_month("2026-06", today="2026-07-10")
    shopping = next(m for m in ins["category_movers"] if m["category"] == "Shopping")
    assert shopping["previous"] == 5000.0 and shopping["current"] == 0.0
    transport = next(m for m in ins["category_movers"] if m["category"] == "Transport")
    assert transport["delta"] == 500.0


def test_top_merchants_grouped_and_ordered(fresh_db):
    _seed(fresh_db)
    tm = db.insights_for_month("2026-06", today="2026-07-10")["top_merchants"]
    assert tm[0]["merchant"] == "MR LANDLORD" and tm[0]["spend"] == 25000.0
    uber = next(m for m in tm if m["merchant"] == "UBER")
    assert uber["spend"] == 500.0 and uber["count"] == 2


def test_recurring_same_amount_three_months(fresh_db):
    _seed(fresh_db)
    rec = db.insights_for_month("2026-06", today="2026-07-10")["recurring"]
    names = {r["merchant"] for r in rec}
    assert "NETFLIX" in names and "MR LANDLORD" in names
    assert "SWIGGY" not in names          # varying amounts != subscription
    netflix = next(r for r in rec if r["merchant"] == "NETFLIX")
    assert netflix["amount"] == 649.0
    assert netflix["months_seen"] == 3
    assert netflix["active_this_month"] is True
    # In July NETFLIX hasn't billed yet
    rec_jul = db.insights_for_month("2026-07", today="2026-07-10")["recurring"]
    assert next(r for r in rec_jul if r["merchant"] == "NETFLIX")["active_this_month"] is False


def test_duplicate_suspects(fresh_db):
    _seed(fresh_db)
    dups = db.insights_for_month("2026-06", today="2026-07-10")["duplicate_suspects"]
    assert len(dups) == 1
    assert dups[0]["merchant"] == "UBER" and dups[0]["count"] == 2
    assert db.insights_for_month("2026-05", today="2026-07-10")["duplicate_suspects"] == []


def test_daily_cumulative_ends_at_total(fresh_db):
    _seed(fresh_db)
    ins = db.insights_for_month("2026-06", today="2026-07-10")
    days = ins["daily_cumulative"]["current"]
    assert days[0] == {"date": "2026-06-01", "day": 1, "cum": 25000.0}
    assert days[-1]["cum"] == ins["total"]
    assert ins["daily_cumulative"]["previous"][-1]["cum"] == ins["prev_total"]


def test_biggest_purchase(fresh_db):
    _seed(fresh_db)
    big = db.insights_for_month("2026-05", today="2026-07-10")["biggest"]
    assert big["merchant"] == "MR LANDLORD" and big["amount"] == 25000.0


def test_prev_month_helper():
    assert db._prev_month("2026-07") == "2026-06"
    assert db._prev_month("2026-01") == "2025-12"


def test_summary_reports_unassigned_spend(fresh_db):
    _ins("z1", "2026-06-02", 777, "MYSTERY CARD", last4="0000")  # unknown last4
    s = db.summary_for_month("2026-06")
    assert s["unassigned"] == {"count": 1, "amount": 777.0}
    assert s["total_spend"] == 777.0     # still in the total — now visibly so
