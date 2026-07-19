"""Storage-layer math: Spend = Σ(purchase) − Σ(refund); card_payment/transfer
stored but NEVER summed. Plus dedupe and range semantics."""
import db


def _ins(email_id, month_day, amount, txn_type, direction="debit",
         merchant="SHOP", account_last4="1234", category="Shopping"):
    account_id = db.account_id_for_last4(account_last4)
    return db.insert_txn({
        "txn_date": month_day, "posted_at": month_day + "T10:00:00+00:00",
        "amount": amount, "direction": direction, "txn_type": txn_type,
        "account_id": account_id, "merchant_raw": merchant,
        "merchant_clean": merchant, "category": category,
        "source_email_id": email_id, "parsed_by": "regex", "confidence": 1.0,
        "raw_snippet": "test",
    })


def _seed_may_june():
    assert _ins("m1", "2026-05-03", 100, "purchase")
    assert _ins("m2", "2026-05-10", 200, "purchase")
    assert _ins("m3", "2026-05-12", 50, "refund", direction="credit")
    assert _ins("m4", "2026-05-15", 1000, "card_payment", account_last4="9999",
                merchant="ICICI BANK CREDIT CARD", category="Uncategorized")
    assert _ins("m5", "2026-05-20", 500, "transfer", direction="credit",
                account_last4="9999", merchant="KOTAK", category="Uncategorized")
    assert _ins("m6", "2026-06-01", 400, "purchase")


def test_signed_spend_only_counts_purchase_minus_refund(fresh_db):
    _seed_may_june()
    s = db.summary_for_month("2026-05")
    assert s["total_spend"] == 250.0                     # 100+200-50; 1500 excluded
    assert s["excluded_transfers"] == {"count": 2, "amount": 1500.0}
    assert s["refunds"] == {"count": 1, "amount": 50.0}


def test_range_total_equals_sum_of_months(fresh_db):
    _seed_may_june()
    may = db.summary_for_month("2026-05")["total_spend"]
    june = db.summary_for_month("2026-06")["total_spend"]
    rng = db.summary_for_range("2026-05", "2026-06")["total_spend"]
    assert rng == may + june == 650.0


def test_reversed_range_is_normalized(fresh_db):
    _seed_may_june()
    assert db.summary_for_range("2026-06", "2026-05")["total_spend"] == 650.0
    assert len(db.transactions_for_range("2026-06", "2026-05")) == 6


def test_dedupe_on_source_email_id(fresh_db):
    assert _ins("dup1", "2026-05-01", 100, "purchase")
    assert not _ins("dup1", "2026-05-01", 100, "purchase")
    assert db.summary_for_month("2026-05")["total_spend"] == 100.0


def test_already_seen_covers_both_tables(fresh_db):
    _ins("t1", "2026-05-01", 10, "purchase")
    db.log_unparsed("u1", "s@x", "subj", "snip", "2026-05-01T00:00:00")
    assert db.already_seen("t1")
    assert db.already_seen("u1")
    assert not db.already_seen("nope")


def test_negative_net_category_excluded_from_donut_but_in_total(fresh_db):
    _ins("r1", "2026-05-01", 80, "refund", direction="credit", category="Returns")
    _ins("p1", "2026-05-02", 100, "purchase", category="Shopping")
    s = db.summary_for_month("2026-05")
    assert s["total_spend"] == 20.0
    assert all(c["category"] != "Returns" for c in s["by_category"])


def test_months_with_data_newest_first(fresh_db):
    _seed_may_june()
    assert db.months_with_data() == ["2026-06", "2026-05"]


def test_monthly_trend_chronological(fresh_db):
    _seed_may_june()
    trend = db.monthly_trend()
    assert [t["month"] for t in trend] == ["2026-05", "2026-06"]
    assert [t["spend"] for t in trend] == [250.0, 400.0]


def test_unparsed_panel_hides_reviewed(fresh_db):
    db.log_unparsed("u1", "s", "subj1", "snip", "2026-05-01T00:00:00")
    db.log_unparsed("u2", "s", "subj2", "snip", "2026-05-02T00:00:00")
    db.mark_unparsed_reviewed("u1")
    ids = [u["source_email_id"] for u in db.get_unparsed()]
    assert ids == ["u2"]
    assert len(db.get_unparsed(include_reviewed=True)) == 2
    db.delete_unparsed("u2")
    assert db.get_unparsed() == []


def test_update_txn_and_lookup(fresh_db):
    _ins("e9", "2026-05-01", 75, "purchase", merchant="ZUDIO")
    txn = db.txn_by_email_id("e9")
    assert txn["amount"] == 75
    rows = db.transactions_for_month("2026-05")
    db.update_txn(rows[0]["id"], category="Shopping", txn_type="refund")
    assert db.txn_by_email_id("e9")["txn_type"] == "refund"


def test_uncategorized_merchant_flow(fresh_db):
    _ins("e1", "2026-05-01", 10, "purchase", merchant="MYSTERY", category="Uncategorized")
    assert db.uncategorized_merchants() == ["MYSTERY"]
    assert db.apply_category_to_merchant("MYSTERY", "Shopping") == 1
    assert db.uncategorized_merchants() == []


def test_ai_usage_totals(fresh_db):
    db.add_ai_usage("sonnet", 100, 20, 0.003)
    db.add_ai_usage("sonnet", 200, 30, 0.005)
    tot = db.ai_usage_total()
    assert tot["calls"] == 2
    assert tot["input_tokens"] == 300
    assert round(tot["cost_usd"], 3) == 0.008


def test_accounts_seeded_from_config(fresh_db):
    last4s = {a["last4"] for a in db.get_accounts()}
    assert {"1234", "9999", "5678", "4321", "7777"} <= last4s
    assert db.account_id_for_last4("1234") is not None
    assert db.account_id_for_last4("0000") is None
