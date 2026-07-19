"""The deterministic verifier is the safety net under the LLM fallback:
nothing the model says enters the totals unless it survives these checks.
"""
import json

import llm


EMAIL = ("Your ICICI Bank Credit Card XX5678 has been used for a transaction of "
         "INR 634.00 on May 15, 2026. Info: AMAZON. "
         "The Available Credit Limit on your card is INR 2,58,712.00.")


# ---------- _verify_amount ----------

def test_amount_verbatim_accepted():
    assert llm._verify_amount(634.0, EMAIL) == 634.0


def test_amount_fabricated_rejected():
    assert llm._verify_amount(999.0, EMAIL) is None


def test_amount_absurd_rejected():
    assert llm._verify_amount(5_000_000.0, "Rs.5000000 credited") is None
    assert llm._verify_amount(-10.0, "Rs.-10") is None
    assert llm._verify_amount("not a number", EMAIL) is None


def test_amount_indian_grouping_accepted():
    assert llm._verify_amount(258712.0, "limit is INR 2,58,712.00") == 258712.0


def test_amount_partial_digit_run_rejected():
    """58712 'appears' inside 2,58,712 only as a partial slice — reject.
    (This is how a mis-picked credit-limit fragment sneaks in.)"""
    assert llm._verify_amount(58712.0, "Limit INR 2,58,712.00 only") is None


def test_amount_prefix_of_longer_number_rejected():
    assert llm._verify_amount(25.0, "Rs.255.00 was debited") is None


def test_amount_with_currency_prefix_accepted():
    assert llm._verify_amount(517.0, "Rs.517.00 is debited") == 517.0
    assert llm._verify_amount(730.0, "Transaction Amount: INR 730 done") == 730.0
    assert llm._verify_amount(62609.02, "paid ₹62,609.02 today") == 62609.02


def test_amount_decimal_variants():
    assert llm._verify_amount(1234.5, "Rs.1234.50 spent") == 1234.5
    assert llm._verify_amount(1234.5, "Rs.1,234.50 spent") == 1234.5


# ---------- credit-limit structural guard ----------

def test_credit_limit_figure_flagged():
    assert llm._looks_like_credit_limit(258712.0, EMAIL)


def test_transaction_amount_not_flagged():
    assert not llm._looks_like_credit_limit(634.0, EMAIL)


def test_indian_group_formatting():
    assert llm._indian_group(258712) == "2,58,712"
    assert llm._indian_group(730) == "730"
    assert llm._indian_group(62609) == "62,609"
    assert llm._indian_group(10000000) == "1,00,00,000"


# ---------- last4 / date ----------

def test_last4_must_appear_in_email():
    assert llm._verify_last4("5678", EMAIL) == "5678"
    assert llm._verify_last4("0000", EMAIL) is None
    assert llm._verify_last4("123", EMAIL) is None
    assert llm._verify_last4(None, EMAIL) is None


def test_date_validation():
    assert llm._verify_date("2026-05-15") == "2026-05-15"
    assert llm._verify_date("2026-13-01") is None
    assert llm._verify_date("15-05-2026") is None
    assert llm._verify_date("soon") is None
    assert llm._verify_date(None) is None


# ---------- JSON extraction ----------

def test_extract_json_plain_and_fenced_and_prose():
    assert llm._extract_json('{"amount": 5}') == {"amount": 5}
    assert llm._extract_json('```json\n{"amount": 5}\n```') == {"amount": 5}
    assert llm._extract_json('Here you go: {"amount": 5} hope that helps') == {"amount": 5}
    assert llm._extract_json("no json at all") is None
    assert llm._extract_json("") is None


# ---------- end-to-end: the LLM proposes, the verifier disposes ----------

def _fake_claude(payload):
    def run(prompt, timeout=60):
        return {"result": json.dumps(payload), "cost_usd": 0,
                "input_tokens": 0, "output_tokens": 0, "model": "test"}
    return run


def _force(monkeypatch, payload):
    monkeypatch.setattr(llm, "_run_claude", _fake_claude(payload))
    monkeypatch.setattr(llm.shutil, "which", lambda _: "/usr/bin/claude")
    return llm.classify_with_llm("subject", EMAIL, force=True)


def test_llm_valid_extraction_accepted(monkeypatch):
    t = _force(monkeypatch, {"amount": 634.0, "direction": "debit", "last4": "5678",
                             "merchant": "AMAZON", "txn_date": "2026-05-15",
                             "txn_type": "purchase"})
    assert t is not None
    assert t.amount == 634.0
    assert t.last4 == "5678"
    assert t.confidence == 0.6


def test_llm_credit_limit_pick_rejected(monkeypatch):
    t = _force(monkeypatch, {"amount": 258712.0, "direction": "debit",
                             "last4": "5678", "txn_type": "purchase"})
    assert t is None


def test_llm_fabricated_amount_rejected(monkeypatch):
    t = _force(monkeypatch, {"amount": 4242.0, "direction": "debit",
                             "last4": "5678", "txn_type": "purchase"})
    assert t is None


def test_llm_bad_direction_rejected(monkeypatch):
    t = _force(monkeypatch, {"amount": 634.0, "direction": "sideways",
                             "last4": "5678", "txn_type": "purchase"})
    assert t is None


def test_llm_not_a_transaction(monkeypatch):
    assert _force(monkeypatch, {"amount": None}) is None


def test_llm_bad_type_defaults_to_purchase_and_bad_fields_dropped(monkeypatch):
    t = _force(monkeypatch, {"amount": 634.0, "direction": "debit", "last4": "0000",
                             "merchant": "  AMAZON  ", "txn_date": "not-a-date",
                             "txn_type": "weird"})
    assert t is not None
    assert t.txn_type == "purchase"
    assert t.last4 is None            # 0000 not in email
    assert t.txn_date is None
    assert t.merchant_raw == "AMAZON"


def test_llm_categorize_only_allowed_categories(monkeypatch):
    mapping = {"SWIGGY": "Food & Dining", "ODD SHOP": "Made Up Category"}
    monkeypatch.setattr(llm, "_run_claude", _fake_claude(mapping))
    monkeypatch.setattr(llm.shutil, "which", lambda _: "/usr/bin/claude")
    out = llm.categorize_with_llm(["SWIGGY", "ODD SHOP"])
    assert out == {"SWIGGY": "Food & Dining"}   # invented category dropped
