"""HSBC credit-card parser. From hsbc@mail.hsbc.co.in.

Real format:
  "We write to confirm that your Credit card no ending with XXXX, has been used
   for INR 2258.36 for payment to MERCHANT NAME on 07 Jun 2026 at 23:21."
"""
import re
from .base import ParsedTxn, html_to_text, parse_amount, parse_date, clean_merchant


def parse(subject: str, body: str) -> ParsedTxn | None:
    text = html_to_text(body)

    m = re.search(
        r"Credit card no ending with\s+(\d{4})\s*,?\s*has been used for\s+(INR\s*[\d,]+(?:\.\d{1,2})?)\s+for payment to\s+(.+?)\s+on\s+(\d{1,2}\s+\w{3}\s+\d{4})",
        text, re.IGNORECASE,
    )
    if not m:
        return None

    return ParsedTxn(
        amount=parse_amount(m.group(2)),
        direction="debit",
        last4=m.group(1),
        merchant_raw=clean_merchant(m.group(3)),
        txn_date=parse_date(m.group(4)),
        txn_type="purchase",
    )
