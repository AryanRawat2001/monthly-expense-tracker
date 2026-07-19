"""End-to-end ingest pipeline (no Gmail): parse -> classify -> categorize -> insert."""
import db
import llm
import gmail_sync
from parsers.base import ParsedTxn


HDFC = "HDFC Bank InstaAlerts <alerts@hdfcbank.net>"


def test_ingest_savings_purchase(fresh_db):
    body = ("Rs.517.00 is debited from your account ending 9999 towards "
            "VPA merchant123@ybl (BOMBAY VADAPAV) on 08-06-26.")
    r = gmail_sync._ingest_one("e1", HDFC, "UPI txn", body, "2026-06-08T12:00:00+00:00")
    assert r == "added"
    t = db.transactions_for_month("2026-06")[0]
    assert t["amount"] == 517.0
    assert t["txn_type"] == "purchase"
    assert t["merchant_raw"] == "BOMBAY VADAPAV"
    assert t["last4"] == "9999"


def test_ingest_cred_bill_excluded(fresh_db):
    body = ("Rs.40000.00 has been debited from account 9999 to "
            "VPA cred.club@axisb CRED on 06-04-26.")
    r = gmail_sync._ingest_one("e2", HDFC, "UPI txn", body, "2026-04-06T12:00:00+00:00")
    assert r == "added"
    t = db.transactions_for_month("2026-04")[0]
    assert t["txn_type"] == "card_payment"
    assert db.summary_for_month("2026-04")["total_spend"] == 0.0


def test_ingest_netbanking_bill_uses_received_date(fresh_db):
    body = ("Thank you for using HDFC Bank NetBanking for payment of Rs. 654.00 "
            "from A/c ****9999 to ICICI BANK CREDIT CARD. As a thank you...")
    r = gmail_sync._ingest_one("e3", HDFC, "NetBanking", body, "2026-06-05T09:00:00+00:00")
    assert r == "added"
    t = db.transactions_for_month("2026-06")[0]
    assert t["txn_type"] == "card_payment"
    assert t["txn_date"] == "2026-06-05"      # parser had no date; email date used


def test_ingest_unparseable_goes_to_unparsed(fresh_db):
    r = gmail_sync._ingest_one("e4", HDFC, "T&C update", "We updated our terms.",
                               "2026-06-01T00:00:00+00:00")
    assert r == "unparsed"
    assert db.get_unparsed()[0]["source_email_id"] == "e4"
    assert db.transactions_for_month("2026-06") == []


def test_ingest_duplicate_email_id(fresh_db):
    body = ("Rs.100.00 is debited from your account ending 9999 towards "
            "VPA a@b (SHOP) on 01-06-26.")
    assert gmail_sync._ingest_one("e5", HDFC, "s", body, "2026-06-01T00:00:00+00:00") == "added"
    assert gmail_sync._ingest_one("e5", HDFC, "s", body, "2026-06-01T00:00:00+00:00") == "duplicate"


def test_ingest_llm_date_far_from_email_is_clamped(fresh_db, monkeypatch):
    """A hallucinated txn_date months away from the email's own timestamp must
    not silently move spend across months — fall back to the received date."""
    fake = ParsedTxn(amount=99.0, direction="debit", last4="9999",
                     merchant_raw="SHOP", txn_date="2020-01-01",
                     txn_type="purchase", confidence=0.6)
    monkeypatch.setattr(llm, "classify_with_llm", lambda *a, **k: fake)
    r = gmail_sync._ingest_one("e6", HDFC, "weird alert", "not parseable Rs.99",
                               "2026-06-05T09:00:00+00:00", use_llm=True)
    assert r == "added_llm"
    t = db.transactions_for_month("2026-06")[0]
    assert t["txn_date"] == "2026-06-05"


def test_ingest_llm_plausible_date_kept(fresh_db, monkeypatch):
    fake = ParsedTxn(amount=99.0, direction="debit", last4="9999",
                     merchant_raw="SHOP", txn_date="2026-06-03",
                     txn_type="purchase", confidence=0.6)
    monkeypatch.setattr(llm, "classify_with_llm", lambda *a, **k: fake)
    gmail_sync._ingest_one("e7", HDFC, "weird alert", "not parseable Rs.99",
                           "2026-06-05T09:00:00+00:00", use_llm=True)
    assert db.transactions_for_month("2026-06")[0]["txn_date"] == "2026-06-03"
