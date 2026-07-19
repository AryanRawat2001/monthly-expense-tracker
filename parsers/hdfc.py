"""HDFC parsers — credit cards and savings.
Senders: alerts@hdfcbank.net and the newer alerts@hdfcbank.bank.in.

HDFC uses several wordings; we try each. Formats (card/account numbers shown as XXXX):

  CARD spend:
    "Rs.50.00 is debited from your HDFC Bank Credit Card ending XXXX
     towards PYU*MERCHANT on 08 Apr, 2026 at 20:10:11."

  SAVINGS UPI debit (old):
    "Rs.161.00 has been debited from account XXXX to VPA xxx@axl NAME on 06-04-26."
  SAVINGS UPI debit (new .bank.in):
    "Rs.517.00 is debited from your account ending XXXX towards VPA xxx@ibl
     (PAYEE NAME) on 08-06-26."

  SAVINGS credit (old):
    "Rs. 3250.00 is successfully credited to your account **XXXX by VPA xxx NAME on 31-01-26."
  SAVINGS credit (new .bank.in):
    "Rs.2000.00 has been successfully credited to your HDFC Bank account ending in XXXX.
     ... Date: 07-06-26 ... Sender: NAME (VPA: xxx) ..."
"""
import re
from .base import ParsedTxn, html_to_text, parse_amount, parse_date, clean_merchant

_AMT = r"Rs\.?\s*[\d,]+(?:\.\d{1,2})?"
_ACCT = r"(?:account|a/c)\s*(?:ending(?:\s+in)?\s*|\*+)?\s*(\d{4})"
_DMY = r"\d{1,2}-\d{1,2}-\d{2,4}"


def parse(subject: str, body: str) -> ParsedTxn | None:
    text = html_to_text(body)

    # ---- Credit card spend (POS/online): "...Credit Card ending NNNN towards MERCHANT on DATE" ----
    m = re.search(
        rf"({_AMT})\s+(?:is|has been)\s+debited from your HDFC Bank (?:\w+ )?Credit Card ending\s*(\d{{4}})\s+towards\s+(.+?)\s+on\s+(\d{{1,2}}\s+\w{{3}},?\s+\d{{4}})",
        text, re.IGNORECASE,
    )
    if m:
        return ParsedTxn(
            amount=parse_amount(m.group(1)), direction="debit", last4=m.group(2),
            merchant_raw=clean_merchant(m.group(3)),
            txn_date=parse_date(m.group(4)), txn_type="purchase",
        )

    # ---- Credit card UPI spend: "...Credit Card ending NNNN and credited to VPA xxx (MERCHANT) on DATE" ----
    m = re.search(
        rf"({_AMT})\s+is\s+debited from your HDFC Bank (?:\w+ )?Credit Card ending\s*(\d{{4}})\s+and credited to\s+(.+?)\s+on\s+(\d{{1,2}}\s+\w{{3}},?\s+\d{{4}})",
        text, re.IGNORECASE,
    )
    if m:
        return ParsedTxn(
            amount=parse_amount(m.group(1)), direction="debit", last4=m.group(2),
            merchant_raw=_clean_payee(m.group(3)),
            txn_date=parse_date(m.group(4)), txn_type="purchase",
        )

    # ---- Card spend, "We noticed a transaction" wording (seen from Jul 2026) ----
    #   "Thank you for using your HDFC Bank Credit Card ending in 4242 .You made
    #    a transaction of Rs. 295.00 at RAZ*MERCHANT on 11-07-2026 20:07:07 ."
    m = re.search(
        rf"Credit Card ending in\s*(\d{{4}})\s*\.?\s*You made a transaction of\s+({_AMT})\s+at\s+(.+?)\s+on\s+(\d{{1,2}}-\d{{1,2}}-\d{{4}})",
        text, re.IGNORECASE,
    )
    if m:
        return ParsedTxn(
            amount=parse_amount(m.group(2)), direction="debit", last4=m.group(1),
            merchant_raw=clean_merchant(m.group(3)),
            txn_date=parse_date(m.group(4)), txn_type="purchase",
        )

    # ---- RuPay-card UPI, newer table wording (seen from Jun 2026) ----
    #   "Rs.191.00 has been debited from your RuPay Credit Card 1729 Paid to
    #    q9000...@okbank Date: 16-06-26 UPI Transaction Reference Number: ..."
    #   (no "ending", no payee name — just the bare VPA handle)
    m = re.search(
        rf"({_AMT})\s+has been debited from your (?:\w+\s+){{0,3}}Credit Card\s*(\d{{4}})\s+Paid to\s+(\S+)",
        text, re.IGNORECASE,
    )
    if m:
        date_m = re.search(rf"Date:\s*({_DMY})", text, re.IGNORECASE)
        return ParsedTxn(
            amount=parse_amount(m.group(1)), direction="debit", last4=m.group(2),
            merchant_raw=clean_merchant(m.group(3)),
            txn_date=parse_date(date_m.group(1)) if date_m else None,
            txn_type="purchase",
        )

    # ---- NetBanking payment from account to a payee (often a credit-card bill) ----
    #   "...NetBanking for payment of Rs. 654.00 from A/c ****XXXX to <BANK> CREDIT CA..."
    m = re.search(
        rf"NetBanking for payment of\s+({_AMT})\s+from\s+(?:A/c|account)\s+\**(\d{{4}})\s+to\s+(.+?)(?:\s+As a thank|\.|$)",
        text, re.IGNORECASE,
    )
    if m:
        payee = clean_merchant(m.group(3))
        # If paying a credit card, mark as card_payment so it's excluded from spend.
        is_card_bill = bool(re.search(r"credit\s*ca", m.group(3), re.IGNORECASE))
        return ParsedTxn(
            amount=parse_amount(m.group(1)), direction="debit", last4=m.group(2),
            merchant_raw=payee, txn_date=None,
            txn_type="card_payment" if is_card_bill else "purchase",
        )

    # ---- Savings DEBIT (old "to VPA" and new "towards VPA"/"account ending") ----
    m = re.search(
        rf"({_AMT})\s+(?:is|has been)\s+debited from(?:\s+your)?\s+{_ACCT}\s+(?:to|towards)\s+(.+?)\s+on\s+({_DMY})",
        text, re.IGNORECASE,
    )
    if m:
        return ParsedTxn(
            amount=parse_amount(m.group(1)), direction="debit", last4=m.group(2),
            merchant_raw=_clean_payee(m.group(3)),
            txn_date=parse_date(m.group(4)), txn_type="purchase",
        )

    # ---- Savings CREDIT (old "credited to your account") ----
    m = re.search(
        rf"({_AMT})\s+(?:is|has been)\s+(?:successfully\s+)?credited to your account\s+\*+?(\d{{4}})\s+(?:by VPA\s+\S+\s+)?(.+?)\s+on\s+({_DMY})",
        text, re.IGNORECASE,
    )
    if m:
        return ParsedTxn(
            amount=parse_amount(m.group(1)), direction="credit", last4=m.group(2),
            merchant_raw=_clean_payee(m.group(3)),
            txn_date=parse_date(m.group(4)), txn_type="transfer",
        )

    # ---- Savings CREDIT (new "...account ending in NNNN. ... Date: DD-MM-YY ... Sender: NAME") ----
    m = re.search(
        rf"({_AMT})\s+has been\s+(?:successfully\s+)?credited to your HDFC Bank\s+{_ACCT}",
        text, re.IGNORECASE,
    )
    if m:
        date_m = re.search(rf"Date:\s*({_DMY})", text, re.IGNORECASE)
        sender_m = re.search(r"Sender:\s*(.+?)\s*(?:\(VPA|UPI Reference|$)", text, re.IGNORECASE)
        return ParsedTxn(
            amount=parse_amount(m.group(1)), direction="credit", last4=m.group(2),
            merchant_raw=_clean_payee(sender_m.group(1)) if sender_m else None,
            txn_date=parse_date(date_m.group(1)) if date_m else None,
            txn_type="transfer",
        )

    return None


def _clean_payee(raw: str | None) -> str | None:
    """Isolate the human payee name. Handles both 'VPA <handle> NAME' (old) and
    '<handle> (NAME)' (new); prefers the parenthesised name when present."""
    if not raw:
        return None
    paren = re.search(r"\(([^)]+)\)", raw)
    if paren and not paren.group(1).lower().startswith("vpa"):
        return clean_merchant(paren.group(1))
    # Strip a leading bare "VPA <handle>" so only the payee name remains.
    cleaned = re.sub(r"^\s*VPA\s+\S+\s*", "", raw, flags=re.IGNORECASE)
    return clean_merchant(cleaned)
