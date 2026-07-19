"""ICICI credit-card parser. From credit_cards@icici.bank.in.

Real format:
  "Your ICICI Bank Credit Card XXXXXX has been used for a transaction of
   INR 634.00 on May 15, 2026 at 10:06:48. Info: AMAZON PAY IN E COMMERCE."
"""
import re
from .base import ParsedTxn, html_to_text, parse_amount, parse_date, clean_merchant


def parse(subject: str, body: str) -> ParsedTxn | None:
    text = html_to_text(body)

    # ---- Card-bill payment received (seen from Jul 2026) — EXCLUDED from spend ----
    #   "We have received payment of INR 724 on your ICICI Bank Credit Card
    #    account 1234 XXXX XXXX 5678 on 15-JUL-26 through Click to Pay."
    #   The card number is masked in the middle; the REAL last4 is the final group.
    m = re.search(
        r"received payment of\s+(INR\s*[\d,]+(?:\.\d{1,2})?)\s+on your ICICI Bank "
        r"Credit Card account\s+([0-9X\s]+?)\s+on\s+(\d{1,2}-[A-Za-z]{3}-\d{2,4})",
        text, re.IGNORECASE,
    )
    if m:
        digit_groups = re.findall(r"\d{4}", m.group(2))
        return ParsedTxn(
            amount=parse_amount(m.group(1)),
            direction="credit",
            last4=digit_groups[-1] if digit_groups else None,
            merchant_raw="ICICI CREDIT CARD BILL PAYMENT",
            txn_date=parse_date(m.group(3)),
            txn_type="card_payment",
        )

    m = re.search(
        r"Credit Card\s+XX?(\d{4})\s+has been used for a transaction of\s+(INR\s*[\d,]+(?:\.\d{1,2})?)\s+on\s+([A-Za-z]{3,9}\s+\d{1,2},\s+\d{4})",
        text, re.IGNORECASE,
    )
    if not m:
        return None

    last4 = m.group(1)
    amount = parse_amount(m.group(2))
    txn_date = parse_date(m.group(3))

    # Merchant is in "Info: <merchant>." followed by the "Available Credit Limit"
    # sentence. Anchor on that trailing text (or end) so multi-word merchants and
    # names with internal periods like "DR. SMITH CLINIC" survive intact.
    info = re.search(
        r"Info:\s*(.+?)\s*(?:The Available Credit Limit|In case|<|$)",
        text, re.IGNORECASE,
    )
    merchant = None
    if info:
        raw = info.group(1).rstrip(". ").strip()
        merchant = clean_merchant(raw)

    # Refund/reversal wording (defensive — credit-card credits are refunds).
    direction = "debit"
    txn_type = "purchase"
    if re.search(r"reversed|refund|credited to your card", text, re.IGNORECASE):
        direction, txn_type = "credit", "refund"

    return ParsedTxn(
        amount=amount, direction=direction, last4=last4,
        merchant_raw=merchant, txn_date=txn_date, txn_type=txn_type,
    )
