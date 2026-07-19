"""Parser registry: route an email to the right bank parser by sender.

We match the sender's ADDR-SPEC (the real address inside <...>), never the
display name — a display name is attacker-controlled, and Gmail's `from:`
search also matches display names, so a mail spoofing "alerts@hdfcbank.net"
as its display name would otherwise reach a parser. Only real transaction-alert
senders are mapped; marketing senders (HDFC information@, ICICI custcomm/
customercomm, Axis digital.axisbankmail, CRED, etc.) deliberately have no
parser, so promos and bill-payment confirmations are skipped rather than
mis-counted.
"""
import re
from email.utils import parseaddr

from . import hdfc, icici, hsbc, axis

# Substring (in the lowercased addr-spec) -> parser module.
# Order matters: first match wins, so put specific alert senders first.
_ROUTES = [
    ("alerts@hdfcbank.net", hdfc),
    ("alerts@hdfcbank.bank.in", hdfc),
    ("credit_cards@icici.bank.in", icici),
    ("alerts@axis.bank.in", axis),
    ("axis.bank.in", axis),
    ("mail.hsbc.co.in", hsbc),
]

# Senders we explicitly never parse (marketing / 3rd-party). Used to avoid
# logging them as "unparsed" noise.
_IGNORE = [
    "information@hdfcbank.net",
    "custcomm.icicibank.com",
    "customercomm.icicibank.com",
    "customer.icici.bank.in",
    "services@custcomm",
    "digital.axisbankmail",
    "informationservices.hsbc",
    "creditcardstatement@mail.hsbc",
    "notification.hsbc",
    "cred.club",
    "paytm.com",
    "groww.in",
]


# Subjects that are NOT transactions even though they come from a real alert
# sender (e.g. HSBC uses hsbc@mail.hsbc.co.in for both spends AND login notices).
# Word-boundaried where a bare substring could hide inside an innocent word
# ("otp" in "hotpot") and silently drop a real alert.
_IGNORE_SUBJECT_RE = re.compile(
    r"successful log ?on|logged on|statement ready|statement notification"
    r"|statement for the per|card statement|\botp\b|one[- ]?time password"
    r"|e-statement",
    re.IGNORECASE,
)


def _addr(sender: str) -> str:
    """The real address part of a From header, lowercased. Falls back to the
    raw string for malformed headers (better to over-match _IGNORE than to
    let junk through to a parser via the display name)."""
    addr = parseaddr(sender or "")[1]
    return (addr or sender or "").lower()


def is_ignored(sender: str, subject: str = "") -> bool:
    s = _addr(sender)
    if any(ig in s for ig in _IGNORE):
        return True
    return bool(_IGNORE_SUBJECT_RE.search(subject or ""))


def get_parser(sender: str):
    """Return the parser module for this sender, or None."""
    s = _addr(sender)
    for needle, module in _ROUTES:
        if needle in s:
            return module
    return None
