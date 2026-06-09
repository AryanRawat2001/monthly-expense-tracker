"""Parser registry: route an email to the right bank parser by sender.

We match the FROM address. Only real transaction-alert senders are mapped;
marketing senders (HDFC information@, ICICI custcomm/customercomm, Axis
digital.axisbankmail, CRED, etc.) deliberately have no parser, so promos and
bill-payment confirmations are skipped rather than mis-counted.
"""
from . import hdfc, icici, hsbc, axis

# Substring (in the lowercased From header) -> parser module.
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
_IGNORE_SUBJECTS = [
    "successful log on",
    "logged on",
    "statement ready",
    "statement notification",
    "statement for the per",
    "card statement",
    "otp",
    "one time password",
    "e-statement",
]


def is_ignored(sender: str, subject: str = "") -> bool:
    s = (sender or "").lower()
    if any(ig in s for ig in _IGNORE):
        return True
    subj = (subject or "").lower()
    return any(ig in subj for ig in _IGNORE_SUBJECTS)


def get_parser(sender: str):
    """Return the parser module for this sender, or None."""
    s = (sender or "").lower()
    for needle, module in _ROUTES:
        if needle in s:
            return module
    return None
