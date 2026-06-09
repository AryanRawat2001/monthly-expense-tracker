"""ICICI credit-card parser. From credit_cards@icici.bank.in.

Real format:
  "Your ICICI Bank Credit Card XXXXXX has been used for a transaction of
   INR 634.00 on May 15, 2026 at 10:06:48. Info: AMAZON PAY IN E COMMERCE."
"""
import re
from .base import ParsedTxn, html_to_text, parse_amount, parse_date, clean_merchant


def parse(subject: str, body: str) -> ParsedTxn | None:
    text = html_to_text(body)

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
