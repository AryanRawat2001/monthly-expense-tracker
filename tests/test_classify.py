"""finalize_txn_type is the no-double-count gatekeeper. Every rule in
CLAUDE.md's verification checklist is pinned here.
"""
import classify
from conftest import account_by_last4


def _txn(amount=1000.0, direction="debit", txn_type="purchase",
         merchant="SOME MERCHANT", snippet=""):
    return {
        "amount": amount, "direction": direction, "txn_type": txn_type,
        "merchant_raw": merchant, "raw_snippet": snippet,
    }


def _savings(fresh_db):
    return account_by_last4("9999")


def _card(fresh_db):
    return account_by_last4("1234")


# ---------- CRED ambiguity ----------

def test_cred_big_debit_is_card_payment(fresh_db):
    t = _txn(amount=40000, merchant="CRED",
             snippet="Rs.40000.00 debited from account 9999 to VPA cred.club@axisb CRED")
    assert classify.finalize_txn_type(t, _savings(fresh_db)) == "card_payment"


def test_cred_small_debit_counts_as_spend(fresh_db):
    t = _txn(amount=350, merchant="CRED",
             snippet="Rs.350.00 debited from account 9999 to VPA cred.club@axisb CRED")
    assert classify.finalize_txn_type(t, _savings(fresh_db)) == "purchase"


def test_cred_never_matches_inside_credit_words(fresh_db):
    """REGRESSION: the 'cred' transfer rule must not fire on 'incredible' or
    'credited' — that silently excluded real purchases from spend."""
    t = _txn(amount=5000, merchant="SOME STORE",
             snippet="Rs.5000.00 is debited towards VPA store@ybl (SOME STORE). "
                     "Incredible offers await you!")
    assert classify.finalize_txn_type(t, _savings(fresh_db)) == "purchase"


# ---------- card-bill payments from savings ----------

def test_explicit_credit_card_payee_excluded(fresh_db):
    t = _txn(amount=654, merchant="ICICI BANK CREDIT CARD",
             snippet="payment of Rs. 654.00 from A/c ****9999 to ICICI BANK CREDIT CA")
    assert classify.finalize_txn_type(t, _savings(fresh_db)) == "card_payment"


def test_billdesk_with_card_context_excluded(fresh_db):
    t = _txn(amount=12000, merchant="BILLDESK",
             snippet="Rs.12000 debited to BILLDESK towards HDFC credit card bill")
    assert classify.finalize_txn_type(t, _savings(fresh_db)) == "card_payment"


def test_plain_bbps_electricity_still_counts(fresh_db):
    """BBPS without 'card' context is a REAL bill (electricity) — must stay spend."""
    t = _txn(amount=2000, merchant="BESCOM",
             snippet="Rs.2000 debited via BBPS towards BESCOM electricity bill")
    assert classify.finalize_txn_type(t, _savings(fresh_db)) == "purchase"


def test_parser_card_payment_survives_without_account(fresh_db):
    """The HDFC NetBanking parser tags bills itself; that must hold even when
    the last4 doesn't match a known account."""
    t = _txn(txn_type="card_payment", merchant="ICICI BANK CREDIT CARD")
    assert classify.finalize_txn_type(t, None) == "card_payment"


# ---------- refunds ----------

def test_refund_is_authoritative_never_reclassified(fresh_db):
    """A refund whose text says 'credited to your card ... payment received'
    must SUBTRACT from spend, not be dropped as a card_payment."""
    t = _txn(direction="credit", txn_type="refund", merchant="AMAZON",
             snippet="INR 500 reversed and credited to your card. Payment received.")
    assert classify.finalize_txn_type(t, _card(fresh_db)) == "refund"


def test_card_credit_payment_received_excluded(fresh_db):
    t = _txn(direction="credit", txn_type="purchase", merchant="HDFC CARD",
             snippet="Payment received, thank you. Rs.40,000 received towards your card")
    assert classify.finalize_txn_type(t, _card(fresh_db)) == "card_payment"


# ---------- incoming money model ----------

def test_friend_payback_credit_becomes_refund(fresh_db):
    t = _txn(direction="credit", txn_type="transfer", merchant="RAHUL SHARMA",
             snippet="Rs.2000.00 credited to your HDFC Bank account Sender: RAHUL SHARMA")
    assert classify.finalize_txn_type(t, _savings(fresh_db)) == "refund"


def test_income_rule_credit_is_ignored_transfer(fresh_db):
    t = _txn(direction="credit", txn_type="transfer", merchant="KOTAK SELF",
             snippet="Rs.50000.00 credited by VPA me@kotak KOTAK SELF")
    assert classify.finalize_txn_type(t, _savings(fresh_db)) == "transfer"


def test_family_income_rule_ignored(fresh_db):
    t = _txn(direction="credit", txn_type="transfer", merchant="PAPA",
             snippet="Rs.10000.00 credited by VPA papa@upi PAPA")
    assert classify.finalize_txn_type(t, _savings(fresh_db)) == "transfer"


# ---------- pass-throughs ----------

def test_normal_card_purchase_unchanged(fresh_db):
    t = _txn(merchant="SWIGGY", snippet="Rs.300 debited towards SWIGGY")
    assert classify.finalize_txn_type(t, _card(fresh_db)) == "purchase"


def test_unknown_account_purchase_unchanged(fresh_db):
    t = _txn(merchant="SHOP", snippet="Rs.100 debited")
    assert classify.finalize_txn_type(t, None) == "purchase"
