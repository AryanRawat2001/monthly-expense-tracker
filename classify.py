"""Finalize txn_type so spend is never double-counted.

A parser sets a *provisional* txn_type. Here we apply transfer_rules + account
context to decide the final type. The whole point: when you pay a card bill from
your HDFC savings account, that debit must NOT be counted as spend — only the
underlying card purchases (which arrive as their own alerts) count.

Two classes of signal:
- STRONG billers (CRED, BillDesk, @cred, yescred): these payees ONLY ever settle
  credit-card bills, so matching them in the merchant PAYEE name is safe.
- AMBIGUOUS words ("card payment", "bbps"): could appear in a real merchant name
  or a genuine utility bill, so only trust them in the bank's narration + need
  "card" context.

Rules, in order:
1. refund stays refund (never dropped).
2. Savings DEBIT to a strong biller (merchant or narration) -> card_payment.
3. Savings DEBIT whose narration has ambiguous card-bill words -> card_payment.
4. CARD credit with "payment received/thank you" -> card_payment.
5. Incoming credit to savings -> transfer.
"""
import os
import re
from db import get_transfer_rules, get_income_rules

# CRED is ambiguous: it settles credit-card BILLS but is also used as CRED Pay
# (UPI) for Ubers and at malls. The email cannot distinguish them, so we use an
# amount heuristic — a large CRED debit is almost certainly a card bill, a small
# one is a real spend. Configurable; 0 disables the heuristic (CRED always counts).
CRED_BILL_THRESHOLD = float(os.environ.get("CRED_BILL_THRESHOLD", "5000"))


def _matches_any(text: str, patterns) -> bool:
    t = (text or "").lower()
    return any(p.lower() in t for p in patterns)


# Generic biller rails (BBPS/BillDesk) carry BOTH real utility bills AND
# credit-card bill payments. To avoid wrongly excluding an electricity bill paid
# via BBPS, these only count as a card payment when "card" also appears.
_AMBIGUOUS_RAILS = {"bbps", "billdesk", "card payment", "credit card payment", "autopay"}

# Strong billers: a payee whose name/handle is CRED. Word-boundaried so it does
# NOT match inside "credit card" (the substring "cred" lives in "credit"!).
_CRED_RE = re.compile(r"\bcred\b|@cred|cred\.club|yescred", re.IGNORECASE)

# These transfer_rules patterns are fully handled by _CRED_RE above. They must
# NOT reach the plain-substring matcher: "cred" would match inside "credited"/
# "incredible" and silently exclude real purchases from spend.
_CRED_FAMILY = {"cred", "@cred", "yescred"}


def _matches_transfer(narration: str, patterns) -> bool:
    """True if the narration indicates a card-bill payment / self-transfer.
    Ambiguous biller rails require additional 'card' context."""
    t = (narration or "").lower()
    has_card_ctx = "card" in t
    for p in patterns:
        pl = p.lower()
        if pl not in t:
            continue
        if pl in _AMBIGUOUS_RAILS and not has_card_ctx:
            continue  # e.g. plain BBPS electricity bill -> not a card payment
        return True
    return False


def finalize_txn_type(txn: dict, account: dict | None) -> str:
    """txn: dict with keys merchant_raw, direction, txn_type, raw_snippet.
    account: dict with 'type' (credit_card|savings) or None."""
    provisional = txn.get("txn_type", "purchase")
    snippet = txn.get("raw_snippet") or ""
    merchant = txn.get("merchant_raw") or txn.get("merchant_clean") or ""

    # Transfer/bill patterns are matched against the NARRATION (snippet); strong
    # billers are ALSO matched against the merchant payee name (safe — those names
    # only ever mean a card-bill payment).
    narration = snippet

    transfer_patterns = [r["pattern"] for r in get_transfer_rules()
                         if r["pattern"].lower() not in _CRED_FAMILY]
    acct_type = account.get("type") if account else None

    # A parser-detected refund is authoritative — never reclassify it as a
    # card_payment (that would wrongly drop the refund from spend entirely).
    if provisional == "refund":
        return "refund"

    if acct_type == "savings" and txn.get("direction") == "debit":
        mlow = merchant.lower()
        is_cred = bool(_CRED_RE.search(f"{merchant} {narration}"))

        # 1a. CRED is AMBIGUOUS (card bill vs CRED Pay for Uber/malls). Only treat
        #     a CRED debit as a card payment when the amount is large (a bill).
        if is_cred:
            amount = txn.get("amount") or 0
            if CRED_BILL_THRESHOLD and amount >= CRED_BILL_THRESHOLD:
                return "card_payment"
            # else: small CRED debit -> real spend, keep provisional (purchase)

        # 1b. Payee that explicitly names a bank's "credit card" account is
        #     unambiguously a card-bill payment (e.g. "ICICI BANK CREDIT CARD").
        #     "credit ca" (not "card") because alerts often truncate "CARD"->"CA";
        #     \b so "accredited"-style words can't trigger it.
        elif re.search(r"\bcredit\s*ca", mlow):
            return "card_payment"

        # 2. Card-bill / transfer markers. User-added transfer_rules are explicit,
        #    so we check them against the PAYEE NAME + narration (a user who adds
        #    "axis bank limited" means that payee). Ambiguous rails still need
        #    'card' context (handled in _matches_transfer).
        elif _matches_transfer(f"{merchant} {narration}", transfer_patterns):
            return "card_payment"

    # 2. A CREDIT on a credit card matching payment-received language = bill payment.
    if acct_type == "credit_card" and txn.get("direction") == "credit":
        if re.search(r"payment received|thank you|received towards|payment of",
                     narration, re.IGNORECASE):
            return "card_payment"

    # 3. Incoming credit to savings.
    #    Model (user's): money coming IN offsets what you spent -> default `refund`
    #    (subtracts from total). EXCEPT income/self-transfers, which neither add nor
    #    subtract -> `transfer` (excluded). The ignore list (salary, own-bank
    #    transfers, optionally family) is the `income_rules` table.
    if acct_type == "savings" and txn.get("direction") == "credit":
        ignore = [r["pattern"] for r in get_income_rules()]
        if _matches_any(f"{merchant} {narration}", ignore):
            return "transfer"     # salary / self-transfer / family -> not counted
        return "refund"           # any other incoming money reduces spend

    return provisional
