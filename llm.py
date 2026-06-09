"""LLM fallback for alerts the regex parsers can't handle.

Design principle: **the LLM proposes, a deterministic verifier disposes.**
LLMs can hallucinate — most dangerously by picking the wrong rupee figure from an
Indian card alert (which lists txn amount AND available/total credit limit). So
every field the model returns is validated against the raw email before it's
allowed into your totals. Anything that fails verification is rejected (returns
None) and the alert is logged to `unparsed` for manual review — never guessed
into the numbers.

Shells out to the local `claude` CLI (no API key needed). Only invoked when:
  - regex parsing returned None, AND
  - LLM_FALLBACK is enabled (default on; toggle via env LLM_FALLBACK=0).

Privacy: only the single unparsed alert's text is sent, and only to the local CLI.
"""
import os
import re
import json
import shutil
import subprocess

from parsers.base import ParsedTxn, parse_amount

LLM_ENABLED = os.environ.get("LLM_FALLBACK", "1") not in ("0", "false", "False")

# Use Sonnet for the cheap, high-volume extraction/categorization work (≈5× cheaper
# than Opus and plenty capable for this). Override with AI_MODEL if needed.
AI_MODEL = os.environ.get("AI_MODEL", "sonnet")

_VALID_DIRECTIONS = {"debit", "credit"}
_VALID_TYPES = {"purchase", "refund", "card_payment", "transfer"}


def _run_claude(prompt: str, timeout: int = 60):
    """Run the claude CLI with Sonnet + JSON output. Returns (result_text, None)
    on failure-to-run, else (result_text, usage_dict). Records cost in db."""
    if not shutil.which("claude"):
        return None
    try:
        proc = subprocess.run(
            ["claude", "-p", prompt, "--model", AI_MODEL, "--output-format", "json"],
            capture_output=True, text=True, timeout=timeout,
        )
    except (subprocess.TimeoutExpired, OSError):
        return None
    if proc.returncode != 0:
        return None
    try:
        envelope = json.loads(proc.stdout)
    except json.JSONDecodeError:
        # Older CLI without --output-format json: treat stdout as the raw result.
        return {"result": proc.stdout, "cost_usd": 0, "input_tokens": 0, "output_tokens": 0}

    usage = envelope.get("usage", {}) or {}
    rec = {
        "result": envelope.get("result", ""),
        "cost_usd": envelope.get("total_cost_usd", 0) or 0,
        "input_tokens": usage.get("input_tokens", 0) or 0,
        "output_tokens": usage.get("output_tokens", 0) or 0,
        "model": AI_MODEL,
    }
    _record_usage(rec)
    return rec


def _record_usage(rec: dict):
    """Persist one AI call's cost/tokens so the dashboard can show a running total."""
    try:
        import db
        db.add_ai_usage(rec["model"], rec["input_tokens"], rec["output_tokens"], rec["cost_usd"])
    except Exception:
        pass  # cost tracking must never break extraction

# Reject obviously-wrong amounts. ₹10 lakh is far above any normal alert; tune if needed.
_MAX_REASONABLE_AMOUNT = 1_000_000.0

_PROMPT = """You extract ONE financial transaction from an Indian bank alert email.
Output ONLY a single compact JSON object — no prose, no markdown fences.

Keys (exactly these):
  amount      : number — the TRANSACTED amount, positive, digits only (no commas/symbols)
  direction   : "debit" or "credit"
  last4       : string of exactly 4 digits (card/account), or null
  merchant    : string (payee/merchant), or null
  txn_date    : "YYYY-MM-DD", or null
  txn_type    : "purchase" | "refund" | "card_payment" | "transfer"

CRITICAL rules to avoid mistakes:
- Copy the amount VERBATIM from the email. Do NOT add, round, or compute.
- Indian card alerts also mention "Available Credit Limit" and "Total Credit Limit".
  These are NOT the transaction amount. Pick ONLY the amount that was spent/debited/credited
  in THIS transaction, never a credit-limit figure.
- "debit"/"spent"/"used for"/"debited" => direction "debit", usually txn_type "purchase".
- "refund"/"reversed"/"reversal" => txn_type "refund", direction "credit".
- Paying a credit-card BILL (CRED / BillDesk / "payment received, thank you" on a card)
  => txn_type "card_payment". Moving money between your own accounts / incoming credit
  to a bank account => "transfer".
- If unsure of txn_type, use "purchase".
- If this email is NOT a transaction (promo, statement summary, OTP, reminder),
  output exactly {"amount": null}.

Examples:
EMAIL: "Rs.50.00 is debited from your HDFC Credit Card ending 1234 towards ZOMATO on 08 Apr, 2026."
JSON: {"amount":50.00,"direction":"debit","last4":"1234","merchant":"ZOMATO","txn_date":"2026-04-08","txn_type":"purchase"}

EMAIL: "Your ICICI Card XX1234 used for INR 634.00 on May 15, 2026. Info: AMAZON. Available Credit Limit INR 2,58,712.00."
JSON: {"amount":634.00,"direction":"debit","last4":"1234","merchant":"AMAZON","txn_date":"2026-05-15","txn_type":"purchase"}

EMAIL SUBJECT: %s

EMAIL TEXT:
%s
"""


def classify_with_llm(subject: str, text: str, force: bool = False) -> ParsedTxn | None:
    # `force` = the user explicitly clicked "Extract with AI", so bypass the
    # LLM_FALLBACK env toggle (which only governs automatic use).
    if (not LLM_ENABLED and not force) or not shutil.which("claude"):
        return None

    prompt = _PROMPT % (subject[:300], text[:4000])
    rec = _run_claude(prompt, timeout=60)
    if not rec:
        return None

    data = _extract_json((rec["result"] or "").strip())
    if not data or data.get("amount") in (None, "", 0):
        return None

    # ---- Deterministic verification: every field must survive a check. ----
    full_text = f"{subject}\n{text}"

    amount = _verify_amount(data.get("amount"), full_text)
    if amount is None:
        return None
    # Structural guard against the classic mis-pick: if the chosen amount is the
    # one quoted right after "available/total credit limit", it's almost certainly
    # wrong — reject so it can't inflate spend.
    if _looks_like_credit_limit(amount, full_text):
        return None

    direction = str(data.get("direction", "")).lower().strip()
    if direction not in _VALID_DIRECTIONS:
        return None

    txn_type = str(data.get("txn_type", "purchase")).lower().strip()
    if txn_type not in _VALID_TYPES:
        txn_type = "purchase"

    last4 = _verify_last4(data.get("last4"), full_text)
    txn_date = _verify_date(data.get("txn_date"))
    merchant = data.get("merchant")
    if isinstance(merchant, str):
        merchant = merchant.strip() or None
    else:
        merchant = None

    return ParsedTxn(
        amount=amount,
        direction=direction,
        last4=last4,
        merchant_raw=merchant,
        txn_date=txn_date,
        txn_type=txn_type,
        confidence=0.6,
    )


def _verify_amount(value, email_text: str) -> float | None:
    """The returned amount must (a) parse to a positive, sane float, AND
    (b) actually appear in the email text. This kills the most common
    hallucination — fabricating or mis-picking a figure."""
    try:
        amount = float(value)
    except (TypeError, ValueError):
        return None
    if amount <= 0 or amount > _MAX_REASONABLE_AMOUNT:
        return None

    # The amount must be present in the email (allowing comma grouping and
    # optional 2-dp), so the model can't invent a number that isn't there.
    int_part = int(amount)
    grouped_in = _indian_group(int_part)
    plain_in = str(int_part)
    cents = round(amount - int_part, 2)
    # Build candidate string fragments to look for.
    candidates = {plain_in, grouped_in}
    if cents:
        dec = f"{amount:.2f}"
        candidates.add(dec)
        candidates.add(_indian_group(int_part) + dec[dec.find("."):])
    norm = email_text.replace(" ", "")
    return amount if any(c.replace(" ", "") in norm for c in candidates) else None


def _looks_like_credit_limit(amount: float, email_text: str) -> bool:
    """True if `amount` appears in the email as a credit-limit figure (which is
    never the transaction amount). Matches '<credit/available/total> limit ... <amt>'."""
    int_part = int(amount)
    amt_pat = re.escape(_indian_group(int_part)) + r"(?:\.\d{2})?"
    plain_pat = str(int_part) + r"(?:\.\d{2})?"
    limit_ctx = r"(?:available|total|credit)\s+(?:credit\s+)?limit[^\d]{0,20}(?:Rs\.?|INR|₹)?\s*"
    pattern = limit_ctx + f"(?:{amt_pat}|{plain_pat})"
    return re.search(pattern, email_text, re.IGNORECASE) is not None


def _indian_group(n: int) -> str:
    """Format an integer with Indian digit grouping: 258712 -> '2,58,712'."""
    s = str(n)
    if len(s) <= 3:
        return s
    head, tail = s[:-3], s[-3:]
    parts = []
    while len(head) > 2:
        parts.insert(0, head[-2:])
        head = head[:-2]
    parts.insert(0, head)
    return ",".join(parts) + "," + tail


def _verify_last4(value, email_text: str) -> str | None:
    """last4 must be exactly 4 digits and appear in the email."""
    if not value:
        return None
    s = re.sub(r"\D", "", str(value))
    if len(s) != 4:
        return None
    return s if s in re.sub(r"\D", "", email_text) else None


def _verify_date(value) -> str | None:
    """Accept only a real YYYY-MM-DD date."""
    if not value or not isinstance(value, str):
        return None
    m = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})", value.strip())
    if not m:
        return None
    y, mo, d = map(int, m.groups())
    if 2000 <= y <= 2100 and 1 <= mo <= 12 and 1 <= d <= 31:
        return value.strip()
    return None


# ---------------------------------------------------------------------------
# AI categorization (separate, lower-risk use of the LLM)
#
# Unlike amount extraction, mislabeling a category has no double-count risk —
# worst case is a wrong tag the user fixes in one click. So this runs on demand
# over many merchants in ONE call, and we only accept categories from the known
# allowed set (anything else -> "Uncategorized").
# ---------------------------------------------------------------------------

# Must match the dashboard's CATEGORIES list (minus "Uncategorized").
ALLOWED_CATEGORIES = [
    "Food & Dining", "Groceries", "Shopping", "Transport", "Travel",
    "Entertainment", "Bills & Utilities", "Health", "Fuel", "Family", "Investment", "Rent",
]

_CATEGORIZE_PROMPT = """You categorize Indian transaction merchants into spending categories.
Allowed categories (use EXACTLY these strings): %s
If a merchant doesn't clearly fit, use "Uncategorized".

Helpful hints for this user:
- A merchant that is a PERSON'S NAME (e.g. "RAVI KUMAR") is almost always a Uber/Rapido/auto
  driver or a small vendor paid via UPI -> categorize as "Transport" unless context says otherwise.
- Indian retail/brand examples: "KAMATHS NATURAL RETAIL"->Groceries, "THE HOUSE OF RARE"/
  "RSP*..."/clothing brands->Shopping, "DISTRICT"/"BookMyShow"/cinemas->Entertainment,
  "BLINKIT"/"ZEPTO"/"INSTAMART"->Groceries, "SWIGGY"/"ZOMATO"/restaurants->Food & Dining,
  fuel pumps/"HPCL"/"IOCL"->Fuel, pharmacies/hospitals->Health, telecom/electricity->Bills & Utilities.

Input is a JSON array of merchant strings. Output ONLY a JSON object mapping each EXACT input
string to its category. No prose, no markdown.

MERCHANTS:
%s
"""


def categorize_with_llm(merchants: list[str]) -> dict:
    """Return {merchant: category} for the given merchants. Categories are
    validated against ALLOWED_CATEGORIES; unknown -> 'Uncategorized'. Returns {}
    on any failure. Always allowed on demand (no env gate — it's a manual click)."""
    if not merchants or not shutil.which("claude"):
        return {}

    # De-dup and cap input size for one call.
    uniq = list(dict.fromkeys(m for m in merchants if m))[:60]
    prompt = _CATEGORIZE_PROMPT % (
        ", ".join(f'"{c}"' for c in ALLOWED_CATEGORIES),
        json.dumps(uniq, ensure_ascii=False),
    )
    rec = _run_claude(prompt, timeout=120)
    if not rec:
        return {}

    data = _extract_json((rec["result"] or "").strip())
    if not isinstance(data, dict):
        return {}

    allowed = set(ALLOWED_CATEGORIES)
    result = {}
    for m in uniq:
        cat = data.get(m)
        if isinstance(cat, str) and cat in allowed:
            result[m] = cat
    return result


def _extract_json(s: str) -> dict | None:
    """Pull a JSON object out of the CLI output, tolerating markdown fences
    and surrounding prose."""
    if not s:
        return None
    # Strip ```json ... ``` fences if present.
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", s, re.DOTALL)
    if fence:
        try:
            return json.loads(fence.group(1))
        except json.JSONDecodeError:
            pass
    start, end = s.find("{"), s.rfind("}")
    if start == -1 or end == -1 or end < start:
        return None
    try:
        return json.loads(s[start:end + 1])
    except json.JSONDecodeError:
        return None
