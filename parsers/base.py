"""Shared helpers for bank-alert parsers.

Each bank parser returns a ParsedTxn (or None if it can't parse). The orchestrator
in gmail_sync.py then resolves the account, runs classification/categorization,
and inserts. Parsers only do extraction + a provisional txn_type.
"""
import re
import html as _html
from dataclasses import dataclass, field
from datetime import datetime
from html.parser import HTMLParser


@dataclass
class ParsedTxn:
    amount: float
    direction: str                 # debit | credit
    last4: str | None = None       # card/account last 4 digits
    merchant_raw: str | None = None
    txn_date: str | None = None    # ISO YYYY-MM-DD
    txn_type: str = "purchase"     # provisional; classify.py finalizes it
    confidence: float = 1.0


class _HTMLToText(HTMLParser):
    def __init__(self):
        super().__init__()
        self._chunks = []
        self._skip = 0   # depth inside <style>/<script> we must not capture

    def handle_starttag(self, tag, attrs):
        if tag in ("style", "script", "head"):
            self._skip += 1

    def handle_endtag(self, tag):
        if tag in ("style", "script", "head") and self._skip:
            self._skip -= 1

    def handle_data(self, data):
        if not self._skip:
            self._chunks.append(data)

    def text(self):
        return " ".join(self._chunks)


def html_to_text(html: str) -> str:
    """Strip HTML tags and collapse whitespace into a single clean string.
    Bank alert bodies are HTML; the meaningful sentence is what we want."""
    parser = _HTMLToText()
    try:
        parser.feed(html)
        text = parser.text()
    except Exception:
        text = re.sub(r"<[^>]+>", " ", html)
    # Decode all HTML entities (&nbsp; &amp; &quot; &#39; ...) and collapse whitespace.
    text = _html.unescape(text).replace("\xa0", " ")
    return re.sub(r"\s+", " ", text).strip()


# Amount like "Rs.50.00", "INR 634.00", "INR 730", "₹62,609.02"
_AMOUNT_RE = re.compile(
    r"(?:Rs\.?|INR|₹)\s*([\d,]+(?:\.\d{1,2})?)", re.IGNORECASE)


def parse_amount(text: str) -> float | None:
    m = _AMOUNT_RE.search(text)
    if not m:
        return None
    try:
        return float(m.group(1).replace(",", ""))
    except ValueError:
        return None


# Date formats seen across the four banks.
_DATE_FORMATS = [
    "%d %b, %Y",      # 08 Apr, 2026  (HDFC card)
    "%d %b %Y",       # 07 Jun 2026   (HSBC)
    "%d-%m-%y",       # 06-04-26      (HDFC UPI)
    "%d-%m-%Y",       # 29-05-2026    (Axis)
    "%b %d, %Y",      # May 15, 2026  (ICICI)
    "%d-%b-%y",       # 15-JUL-26     (ICICI payment received)
    "%d-%b-%Y",       # 15-JUL-2026
]


def parse_date(text: str) -> str | None:
    """Try each known format against candidate date substrings; return ISO date."""
    candidates = re.findall(
        r"\d{1,2}\s+[A-Za-z]{3,9},?\s+\d{2,4}"      # 08 Apr, 2026 / 07 Jun 2026
        r"|[A-Za-z]{3,9}\s+\d{1,2},\s+\d{4}"        # May 15, 2026
        r"|\d{1,2}-[A-Za-z]{3,9}-\d{2,4}"           # 15-JUL-26
        r"|\d{1,2}-\d{1,2}-\d{2,4}",                # 29-05-2026 / 06-04-26
        text,
    )
    for cand in candidates:
        cand = cand.strip()
        for fmt in _DATE_FORMATS:
            try:
                return datetime.strptime(cand, fmt).date().isoformat()
            except ValueError:
                continue
    return None


def clean_merchant(raw: str | None) -> str | None:
    """Tidy a raw merchant string: drop common UPI prefixes, trim noise."""
    if not raw:
        return raw
    m = raw.strip()
    # Strip leading "to/by VPA <handle>" so only the payee name remains.
    m = re.sub(r"^(?:to|by)\s+VPA\s+\S+\s*", "", m, flags=re.IGNORECASE)
    # Payment-gateway prefixes (PayU, Razorpay, POS, UPI) are noise, not identity.
    m = re.sub(r"^(PYU\*|RAZ\*|POS\s+|UPI[-/ ]?)", "", m, flags=re.IGNORECASE)
    # If only a bare VPA handle (xxx@bank) remains, keep it as-is.
    m = re.sub(r"\s+", " ", m).strip(" .,-")
    return m or None
