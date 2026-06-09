"""Axis credit-card parser. From alerts@axis.bank.in.

The alert is a table; flattened to text it reads:
  "... Transaction Amount: INR 730  Merchant Name: BLINKIT
   Axis Bank Credit Card No. XXXX  Date & Time: 29-05-2026, 14:15:53 IST ..."
Subject is also reliable: "INR 730 spent on credit card no. XXXX".
"""
import re
from .base import ParsedTxn, html_to_text, parse_amount, parse_date, clean_merchant


def parse(subject: str, body: str) -> ParsedTxn | None:
    text = html_to_text(body)

    amount = None
    last4 = None
    merchant = None
    txn_date = None

    m = re.search(r"Transaction Amount:\s*(INR\s*[\d,]+(?:\.\d{1,2})?)", text, re.IGNORECASE)
    if m:
        amount = parse_amount(m.group(1))

    m = re.search(r"Merchant Name:\s*(.+?)\s+Axis Bank", text, re.IGNORECASE)
    if m:
        merchant = clean_merchant(m.group(1))

    m = re.search(r"Credit Card No\.?\s*XX?(\d{4})", text, re.IGNORECASE)
    if m:
        last4 = m.group(1)

    m = re.search(r"Date & Time:\s*(\d{1,2}-\d{1,2}-\d{4})", text, re.IGNORECASE)
    if m:
        txn_date = parse_date(m.group(1))

    # Fallbacks from the subject line.
    if amount is None:
        amount = parse_amount(subject)
    if last4 is None:
        sm = re.search(r"card no\.?\s*XX?(\d{4})", subject, re.IGNORECASE)
        if sm:
            last4 = sm.group(1)

    if amount is None or last4 is None:
        return None

    # "spent" = debit/purchase. Refund wording flips it.
    direction, txn_type = "debit", "purchase"
    if re.search(r"refund|reversed|credited", text + " " + subject, re.IGNORECASE):
        direction, txn_type = "credit", "refund"

    return ParsedTxn(
        amount=amount, direction=direction, last4=last4,
        merchant_raw=merchant, txn_date=txn_date, txn_type=txn_type,
    )
