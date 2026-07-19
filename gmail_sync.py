"""Gmail read-only sync: fetch bank alert emails and turn them into transactions.

Flow per message:
  fetch -> route to bank parser (regex) -> if None and enabled, LLM fallback
        -> resolve account by last4 -> finalize txn_type (classify)
        -> categorize -> insert (dedupe on Gmail message id).

OAuth: uses credentials.json (Desktop client) and caches token.json. Scope is
read-only (gmail.readonly) — this app never modifies your mailbox.
"""
import base64
from datetime import date, datetime, timezone
from pathlib import Path

from google.auth.exceptions import RefreshError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

import db
import parsers
import classify
import categorize
import llm
from parsers.base import html_to_text

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]

# Gmail allows up to 100 sub-requests per batch; 50 is a safe, fast default.
_BATCH_SIZE = 50
BASE = Path(__file__).parent
CRED_FILE = BASE / "credentials.json"
TOKEN_FILE = BASE / "token.json"

# Senders whose transaction alerts we care about (Gmail search filter).
ALERT_SENDERS = [
    "alerts@hdfcbank.net",
    "alerts@hdfcbank.bank.in",
    "credit_cards@icici.bank.in",
    "alerts@axis.bank.in",
    "mail.hsbc.co.in",
]


def _get_service():
    creds = None
    if TOKEN_FILE.exists():
        try:
            creds = Credentials.from_authorized_user_file(str(TOKEN_FILE), SCOPES)
        except ValueError:
            creds = None                       # corrupt/old-format token file
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
            except RefreshError:
                # Routine, not fatal: a Testing-mode OAuth app's refresh token
                # dies after 7 days (and tokens can be revoked). Fall through to
                # a fresh consent flow instead of failing the sync.
                creds = None
        if not creds or not creds.valid:
            if not CRED_FILE.exists():
                raise FileNotFoundError(
                    "credentials.json not found. See README for the Google OAuth setup."
                )
            flow = InstalledAppFlow.from_client_secrets_file(str(CRED_FILE), SCOPES)
            # Opens a browser tab for Google consent and waits for approval.
            creds = flow.run_local_server(port=0)
        TOKEN_FILE.write_text(creds.to_json())
        TOKEN_FILE.chmod(0o600)   # OAuth token grants mailbox read access — owner-only
    return build("gmail", "v1", credentials=creds)


def _header(headers, name):
    for h in headers:
        if h["name"].lower() == name.lower():
            return h["value"]
    return ""


def _extract_body(payload) -> str:
    """Walk the MIME tree and return the best text/HTML body."""
    def decode(data):
        # Gmail base64url payloads sometimes arrive without padding; pad to a
        # multiple of 4 so urlsafe_b64decode doesn't raise binascii.Error.
        pad = -len(data) % 4
        return base64.urlsafe_b64decode(data + ("=" * pad)).decode("utf-8", "replace")

    # Prefer HTML, then plain.
    html_body = ""
    text_body = ""

    def walk(part):
        nonlocal html_body, text_body
        mime = part.get("mimeType", "")
        body = part.get("body", {})
        data = body.get("data")
        if data:
            if mime == "text/html" and not html_body:
                html_body = decode(data)
            elif mime == "text/plain" and not text_body:
                text_body = decode(data)
        for sub in part.get("parts", []) or []:
            walk(sub)

    walk(payload)
    return html_body or text_body


def _query(newer_than_days: int) -> str:
    senders = " OR ".join(f"from:{s}" for s in ALERT_SENDERS)
    return f"({senders}) newer_than:{newer_than_days}d"


def sync(newer_than_days: int = 90, max_messages: int = 2000, progress=None) -> dict:
    """Fetch and ingest alerts. Returns counts. Safe to re-run (dedupes).

    progress: optional callable(done:int, total:int, phase:str) for the UI bar.
    """
    def report(done, total, phase):
        if progress:
            progress(done, total, phase)

    db.init_db()
    report(0, 0, "Connecting to Gmail…")
    service = _get_service()

    # Phase 1: collect all matching message IDs so we know the total up front.
    report(0, 0, "Finding bank alerts…")
    ids = []
    page_token = None
    while len(ids) < max_messages:
        resp = service.users().messages().list(
            userId="me", q=_query(newer_than_days),
            pageToken=page_token, maxResults=min(100, max_messages - len(ids)),
        ).execute()
        ids.extend(m["id"] for m in resp.get("messages", []))
        page_token = resp.get("nextPageToken")
        if not page_token:
            break

    total = len(ids)
    added = by_llm = unparsed = skipped_dupes = 0

    # Skip already-seen messages BEFORE fetching — makes re-syncs near-instant.
    fresh = [mid for mid in ids if not db.already_seen(mid)]
    skipped_dupes = total - len(fresh)

    # Phase 2: fetch in BATCHES (one HTTP request per ~50 messages instead of one
    # request per message). This is the main speedup — ~10x fewer round-trips.
    done = skipped_dupes

    def handle(request_id, msg, exception):
        nonlocal added, by_llm, unparsed, done
        done += 1
        report(done, total, "Reading transactions…")
        if exception is not None or not msg:
            return
        payload = msg.get("payload", {})
        headers = payload.get("headers", [])
        sender = _header(headers, "From")
        subject = _header(headers, "Subject")
        received_at = _received_iso(msg)
        if parsers.is_ignored(sender, subject):
            # Remember it (hidden, pre-reviewed) so the next sync skips it
            # before fetching instead of re-downloading it forever.
            db.log_unparsed(msg["id"], sender, subject, "", received_at, reviewed=True)
            return
        body = _extract_body(payload)
        result = _ingest_one(msg["id"], sender, subject, body, received_at)
        if result == "added":
            added += 1
        elif result == "added_llm":
            added += 1
            by_llm += 1
        elif result == "unparsed":
            unparsed += 1

    for start in range(0, len(fresh), _BATCH_SIZE):
        chunk = fresh[start:start + _BATCH_SIZE]
        batch = service.new_batch_http_request(callback=handle)
        for mid in chunk:
            batch.add(service.users().messages().get(
                userId="me", id=mid, format="full"))
        batch.execute()

    report(total, total, "Done")
    return {
        "added": added, "by_llm": by_llm, "unparsed": unparsed,
        "skipped_duplicates": skipped_dupes, "processed": total,
        # True when the id listing hit max_messages — older mail in the window
        # was NOT fetched; the user should raise the cap for deep syncs.
        "capped": total >= max_messages,
    }


def retry_with_llm(email_id: str) -> dict:
    """On-demand: re-fetch one unparsed email and try the LLM on it. If it yields
    a valid (verified) transaction, insert it and remove it from `unparsed`."""
    service = _get_service()
    msg = service.users().messages().get(
        userId="me", id=email_id, format="full").execute()
    payload = msg.get("payload", {})
    headers = payload.get("headers", [])
    sender = _header(headers, "From")
    subject = _header(headers, "Subject")
    received_at = _received_iso(msg)
    body = _extract_body(payload)

    result = _ingest_one(email_id, sender, subject, body, received_at, use_llm=True)
    if result in ("added", "added_llm"):
        db.delete_unparsed(email_id)
        return {"status": "added", "subject": subject, "txn": db.txn_by_email_id(email_id)}
    # AI checked and found no transaction -> mark reviewed so it drops off the panel
    # (kept in the table so a future sync won't re-fetch and re-add it).
    db.mark_unparsed_reviewed(email_id)
    return {"status": "still_unparsed", "subject": subject}


def retry_all_unparsed(progress=None, cancel=None) -> dict:
    """On-demand bulk: run the LLM over every currently-unparsed email.
    Reports progress + a per-email feed item so the UI can show results live.
    Stops cleanly (after the current email) if `cancel` event is set."""
    def report(done, total, phase, item=None):
        if progress:
            progress(done, total, phase, item)

    items = db.get_unparsed(limit=1000)
    total = len(items)
    added = still = 0
    report(0, total, "Extracting with AI…")
    for i, u in enumerate(items, start=1):
        if cancel is not None and cancel.is_set():
            report(i - 1, total, "Stopped", None)
            break
        subj = (u.get("subject") or "")[:70]
        try:
            r = retry_with_llm(u["source_email_id"])
            if r["status"] == "added":
                added += 1
                item = {"ok": True, "subject": subj,
                        "detail": _describe_added(r), }
            else:
                still += 1
                item = {"ok": False, "subject": subj, "detail": "not a transaction"}
        except Exception:
            still += 1
            item = {"ok": False, "subject": subj, "detail": "error"}
        report(i, total, "Extracting with AI…", item)
    report(min(added + still, total), total,
           "Stopped" if (cancel and cancel.is_set()) else "Done")
    return {"added": added, "still_unparsed": still, "processed": added + still}


def _describe_added(r: dict) -> str:
    """Short human description of a freshly extracted transaction for the live feed."""
    t = r.get("txn") or {}
    if not t:
        return "added"
    amt = t.get("amount")
    mer = t.get("merchant") or ""
    cat = t.get("category") or ""
    bits = []
    if amt is not None:
        bits.append(f"₹{amt:,.0f}")
    if mer:
        bits.append(mer)
    if cat and cat != "Uncategorized":
        bits.append(f"→ {cat}")
    return " ".join(bits) or "added"


def categorize_unparsed_with_llm(progress=None, save_rules=True) -> dict:
    """On-demand: use the LLM to categorize all Uncategorized transactions.
    Batched into one (or few) LLM calls. Optionally persists each merchant->category
    mapping as a category rule so future syncs categorize it automatically."""
    def report(done, total, phase):
        if progress:
            progress(done, total, phase)

    merchants = db.uncategorized_merchants()
    total = len(merchants)
    if not total:
        report(0, 0, "Done")
        return {"categorized": 0, "merchants": 0, "rules_added": 0}

    report(0, total, "Asking AI to categorize…")
    mapping = llm.categorize_with_llm(merchants)

    updated = rules_added = 0
    existing = {r["pattern"].lower() for r in db.get_category_rules()}
    for i, m in enumerate(merchants, start=1):
        cat = mapping.get(m)
        if cat:
            updated += db.apply_category_to_merchant(m, cat)
            # Save the exact merchant string as a rule (high priority) so it
            # sticks next time. Substring match means it also catches variants —
            # which is why very short strings are NOT saved (a 2-3 char rule
            # would match inside unrelated merchant names forever).
            if save_rules and len(m.strip()) >= 4 and m.lower() not in existing:
                db.add_category_rule(m.strip(), cat, 15)
                rules_added += 1
        report(i, total, "Categorizing…")

    report(total, total, "Done")
    return {"categorized": updated, "merchants": total, "rules_added": rules_added}


def _received_iso(msg) -> str:
    ms = msg.get("internalDate")
    if ms:
        return datetime.fromtimestamp(int(ms) / 1000, tz=timezone.utc).isoformat()
    return ""


def _ingest_one(email_id, sender, subject, body, received_at, use_llm=False) -> str:
    parser = parsers.get_parser(sender)
    parsed = parser.parse(subject, body) if parser else None
    parsed_by = "regex"

    if parsed is None and use_llm:
        # Regex couldn't handle it -> try LLM fallback on the cleaned text.
        # Only when explicitly requested (on-demand), never inline during a
        # normal sync — the LLM is slow and spends tokens. force=True because
        # the user explicitly asked for AI on this email.
        text = html_to_text(body)
        parsed = llm.classify_with_llm(subject, text, force=True)
        parsed_by = "llm"

    if parsed is None or not parsed.amount:
        db.log_unparsed(email_id, sender, subject,
                        html_to_text(body)[:500], received_at)
        return "unparsed"

    # Guarantee a usable date. Without one, the txn would silently vanish from
    # every month view yet still count as "added". Prefer parsed date, then the
    # email's received date; if neither exists, treat as unparsed rather than
    # inserting a date-less row.
    txn_date = parsed.txn_date or (received_at[:10] if received_at else None)
    # An LLM-extracted date can't be verified against the email the way the
    # amount is. If it lands far from the email's own timestamp it would move
    # spend across months — trust the email date instead.
    if parsed_by == "llm" and parsed.txn_date and received_at:
        try:
            drift = abs((date.fromisoformat(received_at[:10])
                         - date.fromisoformat(parsed.txn_date)).days)
            if drift > 40:
                txn_date = received_at[:10]
        except ValueError:
            txn_date = received_at[:10]
    if not txn_date:
        db.log_unparsed(email_id, sender, subject,
                        html_to_text(body)[:500], received_at)
        return "unparsed"

    account_id = db.account_id_for_last4(parsed.last4) if parsed.last4 else None
    account = None
    if account_id:
        account = next((a for a in db.get_accounts() if a["id"] == account_id), None)

    txn = {
        "amount": parsed.amount,
        "direction": parsed.direction,
        "txn_type": parsed.txn_type,
        "account_id": account_id,
        "merchant_raw": parsed.merchant_raw,
        "txn_date": txn_date,
        "posted_at": received_at,
        "source_email_id": email_id,
        "parsed_by": parsed_by,
        "confidence": parsed.confidence,
        "raw_snippet": (subject + " | " + html_to_text(body)[:300]),
    }

    # Finalize type (transfer/bill detection) then categorize.
    txn["txn_type"] = classify.finalize_txn_type(txn, account)
    txn["merchant_clean"] = parsed.merchant_raw
    txn["category"] = categorize.categorize(parsed.merchant_raw, parsed.amount)

    inserted = db.insert_txn(txn)
    if not inserted:
        return "duplicate"
    return "added_llm" if parsed_by == "llm" else "added"
