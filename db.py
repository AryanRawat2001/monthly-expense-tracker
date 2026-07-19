"""SQLite storage layer for the Monthly Expense Tracker.

One local file (expenses.db). Holds transactions, accounts, editable rules,
budgets, and an `unparsed` log for alerts we couldn't parse.

The golden rule lives here: spend queries count ONLY txn_type in
('purchase','refund'). card_payment / transfer are stored but never summed,
so paying a card bill from savings never double-counts the underlying spends.
"""
import calendar
import json
import os
import sqlite3
import statistics
from datetime import date
from pathlib import Path
from contextlib import contextmanager

# Overridable via env so tests (and alternate setups) never touch the real data.
DB_PATH = Path(os.environ.get("EXPENSES_DB", Path(__file__).parent / "expenses.db"))
CONFIG_PATH = Path(os.environ.get("EXPENSES_CONFIG", Path(__file__).parent / "config.json"))

# txn_type values that represent real spending (everything else is excluded)
SPEND_TYPES = ("purchase", "refund")

# Rent is paid a few days BEFORE the month it's for (rent sent on 29th June is
# July's rent). Transactions keep their true bank date; a separate `period`
# column (YYYY-MM) carries the month they count in. A Rent txn dated on/after
# this day of month is attributed to the NEXT month. Override in config.json.
RENT_SHIFT_DAY_DEFAULT = 25

# Personal data (accounts, names, amounts) lives in a GITIGNORED config.json so the
# code itself can be shared publicly. See config.example.json for the format.
def _load_config():
    if CONFIG_PATH.exists():
        try:
            return json.loads(CONFIG_PATH.read_text())
        except Exception:
            pass
    return {}

_CFG = _load_config()
RENT_SHIFT_DAY = int(_CFG.get("rent_shift_day", RENT_SHIFT_DAY_DEFAULT))

# Accounts seeded on first run (from config.json). last4 matches alerts to an account.
SEED_ACCOUNTS = [
    (a["name"], a["type"], a["bank"], a["last4"])
    for a in _CFG.get("accounts", [])
]

# Starter merchant -> category rules (substring match on cleaned merchant, case-insensitive).
SEED_CATEGORY_RULES = [
    ("swiggy", "Food & Dining", 10),
    ("zomato", "Food & Dining", 10),
    ("eternal", "Food & Dining", 10),      # Zomato/Eternal Ltd
    ("blinkit", "Groceries", 10),
    ("zepto", "Groceries", 10),
    ("bigbasket", "Groceries", 10),
    ("instamart", "Groceries", 10),
    ("dmart", "Groceries", 10),
    ("amazon", "Shopping", 5),
    ("flipkart", "Shopping", 5),
    ("myntra", "Shopping", 5),
    ("ajio", "Shopping", 5),
    ("uber", "Transport", 10),
    ("ola", "Transport", 10),
    ("rapido", "Transport", 10),
    ("irctc", "Transport", 10),
    ("indigo", "Travel", 10),
    ("makemytrip", "Travel", 10),
    ("goibibo", "Travel", 10),
    ("netflix", "Entertainment", 10),
    ("spotify", "Entertainment", 10),
    ("hotstar", "Entertainment", 10),
    ("bookmyshow", "Entertainment", 10),
    ("jio", "Bills & Utilities", 8),
    ("airtel", "Bills & Utilities", 8),
    ("electricity", "Bills & Utilities", 8),
    ("bescom", "Bills & Utilities", 8),
    ("bbps", "Bills & Utilities", 8),
    ("pharmacy", "Health", 8),
    ("apollo", "Health", 8),
    ("pharmeasy", "Health", 8),
    ("1mg", "Health", 8),
    ("fuel", "Fuel", 8),
    ("petrol", "Fuel", 8),
    ("hpcl", "Fuel", 8),
    ("iocl", "Fuel", 8),
    ("bharat petroleum", "Fuel", 8),
]
# Personal payee→category rules (landlord/family/etc.) come from config.json.
SEED_CATEGORY_RULES += [tuple(r) for r in _CFG.get("category_rules", [])]

# Exact-amount -> category rules (e.g. fixed rent to a person). From config.json.
SEED_AMOUNT_RULES = [tuple(r) for r in _CFG.get("amount_rules", [])]

# INCOME / IGNORE list: incoming savings credits matching these are NEITHER spend
# nor a deduction (typed `transfer`, fully excluded). Everything else coming in is
# treated as a payback that REDUCES spend (`refund`). This list = "money in that
# isn't a reimbursement": salary sender, self-transfers, family. From config.json.
# (Salary is a NEFT credit and HDFC does NOT email those, so salary never appears.)
SEED_INCOME_RULES = [tuple(r) for r in _CFG.get("income_rules", [])]

# Patterns that force a txn_type of card_payment / transfer (excluded from spend).
# Matched (case-insensitive substring) against the merchant/narration text.
SEED_TRANSFER_RULES = [
    ("cred", "CRED credit-card bill payment"),
    ("@cred", "CRED UPI handle"),
    ("yescred", "CRED UPI handle (Yes Bank)"),
    ("billdesk", "BillDesk biller"),
    ("bbps", "Bharat BillPay (often a bill payment)"),
    ("credit card payment", "Explicit card bill payment"),
    ("card payment", "Explicit card bill payment"),
    ("payment received", "Card statement: payment received"),
    ("autopay", "Card autopay"),
]


def _next_month(month: str) -> str:
    y, m = map(int, month.split("-"))
    return f"{y + 1:04d}-01" if m == 12 else f"{y:04d}-{m + 1:02d}"


def period_for(txn_date: str | None, category: str | None) -> str | None:
    """The YYYY-MM a transaction COUNTS in. Equal to the txn's own month,
    except Rent paid on/after RENT_SHIFT_DAY, which belongs to the next month
    (rent is paid a few days early). Rent on the 1st-24th stays in its month,
    so both 'end of June' and 'start of July' land in July."""
    if not txn_date:
        return None
    month = txn_date[:7]
    try:
        day = int(txn_date[8:10])
    except (ValueError, IndexError):
        return month
    if category == "Rent" and day >= RENT_SHIFT_DAY:
        return _next_month(month)
    return month


def _month_expr(alias: str = "") -> str:
    """SQL for the attribution month, with a fallback for legacy NULL periods."""
    p = f"{alias}." if alias else ""
    return f"COALESCE({p}period, substr({p}txn_date,1,7))"


@contextmanager
def get_conn():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    # WAL lets the dashboard read while the background sync thread writes,
    # instead of surfacing "database is locked" errors.
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 5000")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db():
    """Create tables (if absent) and seed accounts / starter rules."""
    with get_conn() as conn:
        c = conn.cursor()
        c.executescript(
            """
            CREATE TABLE IF NOT EXISTS accounts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                type TEXT NOT NULL,            -- credit_card | savings
                bank TEXT NOT NULL,
                last4 TEXT NOT NULL UNIQUE
            );

            CREATE TABLE IF NOT EXISTS transactions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                txn_date TEXT,                 -- ISO date YYYY-MM-DD (the bank's date)
                period TEXT,                   -- YYYY-MM the txn COUNTS in (rent shifting)
                posted_at TEXT,                -- email received time (ISO)
                amount REAL NOT NULL,          -- always positive; sign comes from direction
                direction TEXT NOT NULL,       -- debit | credit
                txn_type TEXT NOT NULL,        -- purchase | refund | card_payment | transfer
                account_id INTEGER,
                merchant_raw TEXT,
                merchant_clean TEXT,
                category TEXT DEFAULT 'Uncategorized',
                source_email_id TEXT UNIQUE,   -- Gmail message id -> dedupe key
                parsed_by TEXT,                -- regex | llm
                confidence REAL DEFAULT 1.0,
                linked_txn_id INTEGER,         -- paired transfer (optional)
                raw_snippet TEXT,
                FOREIGN KEY (account_id) REFERENCES accounts(id)
            );

            CREATE TABLE IF NOT EXISTS category_rules (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                pattern TEXT NOT NULL,
                category TEXT NOT NULL,
                priority INTEGER DEFAULT 5
            );

            CREATE TABLE IF NOT EXISTS transfer_rules (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                pattern TEXT NOT NULL,
                note TEXT
            );

            CREATE TABLE IF NOT EXISTS budgets (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                scope TEXT NOT NULL,           -- category | account | total
                scope_value TEXT,              -- category name / account last4 / NULL for total
                month_limit REAL NOT NULL
            );

            CREATE TABLE IF NOT EXISTS unparsed (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_email_id TEXT UNIQUE,
                sender TEXT,
                subject TEXT,
                snippet TEXT,
                received_at TEXT,
                ai_reviewed INTEGER DEFAULT 0   -- 1 = AI checked, found no transaction
            );

            CREATE TABLE IF NOT EXISTS ai_usage (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                model TEXT,
                input_tokens INTEGER DEFAULT 0,
                output_tokens INTEGER DEFAULT 0,
                cost_usd REAL DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS amount_rules (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                amount REAL NOT NULL UNIQUE,   -- exact transaction amount
                category TEXT NOT NULL,
                note TEXT
            );

            CREATE TABLE IF NOT EXISTS income_rules (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                pattern TEXT NOT NULL,         -- incoming credits matching this are IGNORED
                note TEXT                      -- (salary/self-transfer/family), not a deduction
            );
            """
        )

        # Lightweight migration: add ai_reviewed to pre-existing unparsed tables.
        cols = [r[1] for r in c.execute("PRAGMA table_info(unparsed)").fetchall()]
        if "ai_reviewed" not in cols:
            c.execute("ALTER TABLE unparsed ADD COLUMN ai_reviewed INTEGER DEFAULT 0")

        # Migration: attribution month. Backfill is idempotent and also repairs
        # any rows written without a period (e.g. by older code).
        tcols = [r[1] for r in c.execute("PRAGMA table_info(transactions)").fetchall()]
        if "period" not in tcols:
            c.execute("ALTER TABLE transactions ADD COLUMN period TEXT")
        for row in c.execute(
            "SELECT id, txn_date, category FROM transactions "
            "WHERE period IS NULL AND txn_date IS NOT NULL"
        ).fetchall():
            c.execute("UPDATE transactions SET period = ? WHERE id = ?",
                      (period_for(row[1], row[2]), row[0]))

        # Seed accounts (idempotent on last4).
        for name, atype, bank, last4 in SEED_ACCOUNTS:
            c.execute(
                "INSERT OR IGNORE INTO accounts (name, type, bank, last4) VALUES (?,?,?,?)",
                (name, atype, bank, last4),
            )

        # Seed rules only if the tables are empty (so user edits aren't clobbered).
        if c.execute("SELECT COUNT(*) FROM category_rules").fetchone()[0] == 0:
            c.executemany(
                "INSERT INTO category_rules (pattern, category, priority) VALUES (?,?,?)",
                SEED_CATEGORY_RULES,
            )
        if c.execute("SELECT COUNT(*) FROM transfer_rules").fetchone()[0] == 0:
            c.executemany(
                "INSERT INTO transfer_rules (pattern, note) VALUES (?,?)",
                SEED_TRANSFER_RULES,
            )
        if c.execute("SELECT COUNT(*) FROM amount_rules").fetchone()[0] == 0:
            c.executemany(
                "INSERT OR IGNORE INTO amount_rules (amount, category, note) VALUES (?,?,?)",
                SEED_AMOUNT_RULES,
            )
        if c.execute("SELECT COUNT(*) FROM income_rules").fetchone()[0] == 0:
            c.executemany(
                "INSERT INTO income_rules (pattern, note) VALUES (?,?)",
                SEED_INCOME_RULES,
            )


# ---------- account helpers ----------

def get_accounts():
    with get_conn() as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM accounts ORDER BY bank, name")]


def account_id_for_last4(last4):
    if not last4:
        return None
    last4 = str(last4).strip()
    with get_conn() as conn:
        row = conn.execute("SELECT id FROM accounts WHERE last4 = ?", (last4,)).fetchone()
        return row["id"] if row else None


def months_with_data():
    """Distinct YYYY-MM (attribution months) with at least one txn, newest first."""
    with get_conn() as conn:
        rows = conn.execute(
            f"""SELECT DISTINCT {_month_expr()} AS month FROM transactions
                WHERE txn_date IS NOT NULL ORDER BY month DESC"""
        ).fetchall()
        return [r["month"] for r in rows]


# ---------- transaction helpers ----------

def insert_txn(txn: dict) -> bool:
    """Insert a transaction. Returns True if inserted, False if it was a duplicate
    (same source_email_id). Dedupe makes re-syncing safe."""
    with get_conn() as conn:
        try:
            conn.execute(
                """INSERT INTO transactions
                   (txn_date, period, posted_at, amount, direction, txn_type, account_id,
                    merchant_raw, merchant_clean, category, source_email_id,
                    parsed_by, confidence, raw_snippet)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    txn.get("txn_date"),
                    period_for(txn.get("txn_date"), txn.get("category", "Uncategorized")),
                    txn.get("posted_at"), txn["amount"],
                    txn["direction"], txn["txn_type"], txn.get("account_id"),
                    txn.get("merchant_raw"), txn.get("merchant_clean"),
                    txn.get("category", "Uncategorized"), txn.get("source_email_id"),
                    txn.get("parsed_by", "regex"), txn.get("confidence", 1.0),
                    txn.get("raw_snippet"),
                ),
            )
            return True
        except sqlite3.IntegrityError:
            return False  # duplicate source_email_id


def log_unparsed(email_id, sender, subject, snippet, received_at, reviewed=False):
    """reviewed=True records a known non-transaction (ignored sender/subject)
    so re-syncs skip it before fetching, without it appearing in the panel."""
    with get_conn() as conn:
        conn.execute(
            """INSERT OR IGNORE INTO unparsed
               (source_email_id, sender, subject, snippet, received_at, ai_reviewed)
               VALUES (?,?,?,?,?,?)""",
            (email_id, sender, subject, snippet, received_at, 1 if reviewed else 0),
        )


def already_seen(email_id) -> bool:
    """True if this Gmail message id is already stored (in transactions or unparsed)."""
    with get_conn() as conn:
        a = conn.execute("SELECT 1 FROM transactions WHERE source_email_id = ?", (email_id,)).fetchone()
        b = conn.execute("SELECT 1 FROM unparsed WHERE source_email_id = ?", (email_id,)).fetchone()
        return bool(a or b)


def update_txn(txn_id, category=None, txn_type=None):
    with get_conn() as conn:
        if category is not None:
            conn.execute("UPDATE transactions SET category = ? WHERE id = ?", (category, txn_id))
            # Category drives the attribution month (Rent shifts to next month),
            # so flipping a row to/from Rent must move it between months too.
            row = conn.execute("SELECT txn_date, category FROM transactions WHERE id = ?",
                               (txn_id,)).fetchone()
            if row:
                conn.execute("UPDATE transactions SET period = ? WHERE id = ?",
                             (period_for(row["txn_date"], row["category"]), txn_id))
        if txn_type is not None:
            conn.execute("UPDATE transactions SET txn_type = ? WHERE id = ?", (txn_type, txn_id))


def transactions_for_month(month: str):
    """All transactions in YYYY-MM, joined with account info, newest first."""
    return transactions_for_range(month, month)


def transactions_for_range(start: str, end: str):
    """All transactions ATTRIBUTED to the inclusive YYYY-MM range, newest first.
    (A June-29 rent with period 2026-07 shows in July's list, with its true date.)"""
    if end < start:
        start, end = end, start
    with get_conn() as conn:
        rows = conn.execute(
            f"""SELECT t.*, a.name AS account_name, a.bank, a.last4, a.type AS account_type
                FROM transactions t LEFT JOIN accounts a ON t.account_id = a.id
                WHERE {_month_expr('t')} BETWEEN ? AND ?
                ORDER BY t.txn_date DESC, t.id DESC""",
            (start, end),
        )
        return [dict(r) for r in rows]


def _signed_spend_sql():
    """SQL expression: positive for purchases, negative for refunds, 0 otherwise."""
    return (
        "SUM(CASE WHEN txn_type='purchase' THEN amount "
        "WHEN txn_type='refund' THEN -amount ELSE 0 END)"
    )


def summary_for_month(month: str):
    """Convenience wrapper: summary for a single YYYY-MM."""
    return summary_for_range(month, month)


def summary_for_range(start: str, end: str):
    """True spend over the inclusive YYYY-MM range [start, end]: per account, per
    category, and total. YYYY-MM strings sort lexicographically, so we filter with
    substr(txn_date,1,7) BETWEEN start AND end. Excludes card_payment/transfer."""
    if end < start:
        start, end = end, start
    with get_conn() as conn:
        spend = _signed_spend_sql()
        month = _month_expr()
        month_t = _month_expr("t")
        rng = (start, end)

        by_account = conn.execute(
            f"""SELECT a.id, a.name, a.bank, a.last4, a.type,
                       COALESCE({spend},0) AS spend
                FROM accounts a
                LEFT JOIN transactions t
                       ON t.account_id = a.id
                      AND {month_t} BETWEEN ? AND ?
                GROUP BY a.id ORDER BY spend DESC""",
            rng,
        ).fetchall()

        # Donut shows positive net spend only. A category that nets negative
        # (refunds > purchases) can't be drawn as a pie slice, so exclude it here.
        by_category = conn.execute(
            f"""SELECT category, COALESCE({spend},0) AS spend
                FROM transactions
                WHERE {month} BETWEEN ? AND ? AND txn_type IN ('purchase','refund')
                GROUP BY category HAVING spend > 0 ORDER BY spend DESC""",
            rng,
        ).fetchall()

        total = conn.execute(
            f"""SELECT COALESCE({spend},0) AS spend FROM transactions
                WHERE {month} BETWEEN ? AND ?""",
            rng,
        ).fetchone()["spend"]

        excluded = conn.execute(
            f"""SELECT COUNT(*) AS n, COALESCE(SUM(amount),0) AS amt FROM transactions
                WHERE {month} BETWEEN ? AND ? AND txn_type IN ('card_payment','transfer')""",
            rng,
        ).fetchone()

        # Refunds/reimbursements: money that came back, reducing spend.
        refunds = conn.execute(
            f"""SELECT COUNT(*) AS n, COALESCE(SUM(amount),0) AS amt FROM transactions
                WHERE {month} BETWEEN ? AND ? AND txn_type = 'refund'""",
            rng,
        ).fetchone()

        # Spend per (account, category) — drives the stacked bar chart.
        acct_cat = conn.execute(
            f"""SELECT a.last4, a.bank, t.category, COALESCE({spend},0) AS spend
                FROM transactions t JOIN accounts a ON t.account_id = a.id
                WHERE {month_t} BETWEEN ? AND ? AND t.txn_type IN ('purchase','refund')
                GROUP BY a.id, t.category HAVING spend > 0""",
            rng,
        ).fetchall()

        # Spend rows whose last4 matched no known account — invisible in
        # by_account but part of the total. Surfaced so the numbers reconcile
        # and the user knows to add the card to config.json.
        unassigned = conn.execute(
            f"""SELECT COUNT(*) AS n, COALESCE({spend},0) AS amt FROM transactions
                WHERE {month} BETWEEN ? AND ?
                  AND account_id IS NULL AND txn_type IN ('purchase','refund')""",
            rng,
        ).fetchone()

        label = start if start == end else f"{start} → {end}"
        return {
            "month": label,
            "start": start, "end": end,
            "total_spend": round(total, 2),
            "by_account": [dict(r) for r in by_account],
            "by_category": [dict(r) for r in by_category],
            "by_account_category": [dict(r) for r in acct_cat],
            "excluded_transfers": {"count": excluded["n"], "amount": round(excluded["amt"], 2)},
            "refunds": {"count": refunds["n"], "amount": round(refunds["amt"], 2)},
            "unassigned": {"count": unassigned["n"], "amount": round(unassigned["amt"], 2)},
        }


def monthly_trend(limit_months=6):
    """Total spend per month for the most recent N months (for the trend chart)."""
    with get_conn() as conn:
        spend = _signed_spend_sql()
        rows = conn.execute(
            f"""SELECT {_month_expr()} AS month, COALESCE({spend},0) AS spend
                FROM transactions
                WHERE txn_date IS NOT NULL AND txn_type IN ('purchase','refund')
                GROUP BY month ORDER BY month DESC LIMIT ?""",
            (limit_months,),
        ).fetchall()
        return [dict(r) for r in reversed(rows)]


# ---------- insights / month-end estimator ----------

def _prev_month(month: str) -> str:
    y, m = map(int, month.split("-"))
    return f"{y - 1:04d}-12" if m == 1 else f"{y:04d}-{m - 1:02d}"


def _daily_cumulative(conn, month: str):
    """Day-by-day running net spend for one attribution month (the pace chart).
    A txn attributed here but dated in another month (early-paid rent) counts
    from day 1 — it's a start-of-month obligation, not a day-29 spike."""
    spend = _signed_spend_sql()
    month_e = _month_expr()
    rows = conn.execute(
        f"""SELECT CASE WHEN substr(txn_date,1,7) = {month_e}
                        THEN CAST(substr(txn_date,9,2) AS INTEGER) ELSE 1 END AS day,
                   COALESCE({spend},0) AS s
            FROM transactions
            WHERE {month_e} = ? AND txn_date IS NOT NULL
              AND txn_type IN ('purchase','refund')
            GROUP BY day ORDER BY day""",
        (month,),
    ).fetchall()
    out, cum = [], 0.0
    for r in rows:
        cum += r["s"]
        out.append({"date": f"{month}-{r['day']:02d}", "day": r["day"],
                    "cum": round(cum, 2)})
    return out


def insights_for_month(month: str, today: str | None = None) -> dict:
    """The analysis pack for one month.

    - projection: month-end estimate for the CURRENT month — actual spend so far
      plus the observed daily run-rate for the remaining days, with the median of
      the three prior months as a "typical month" reference. For past months the
      projection is simply the actual total.
    - category movers vs the previous month, top merchants, recurring payments
      (same merchant + same amount in >= 3 distinct months), duplicate-alert
      suspects (identical account/amount/date/merchant), and the daily
      cumulative series for this and the previous month.

    `today` is injectable for tests; defaults to the real date.
    """
    today = today or date.today().isoformat()
    prev = _prev_month(month)
    spend = _signed_spend_sql()
    month_e = _month_expr()
    month_t = _month_expr("t")

    with get_conn() as conn:
        def month_total(m):
            return round(conn.execute(
                f"SELECT COALESCE({spend},0) AS s FROM transactions WHERE {month_e}=?",
                (m,),
            ).fetchone()["s"], 2)

        total = month_total(month)
        prev_total = month_total(prev)

        # --- projection (the estimator) ---
        days_in_month = calendar.monthrange(*map(int, month.split("-")))[1]
        is_current = month == today[:7]
        days_elapsed = min(int(today[8:10]), days_in_month) if is_current else days_in_month
        daily_rate = total / max(days_elapsed, 1)
        projected = (round(total + daily_rate * (days_in_month - days_elapsed), 2)
                     if is_current else total)
        prior = conn.execute(
            f"""SELECT {month_e} AS m, COALESCE({spend},0) AS s
                FROM transactions
                WHERE txn_date IS NOT NULL AND {month_e} < ?
                  AND txn_type IN ('purchase','refund')
                GROUP BY m ORDER BY m DESC LIMIT 3""",
            (month,),
        ).fetchall()
        typical = round(statistics.median(r["s"] for r in prior), 2) if prior else None

        # --- category movers vs previous month ---
        def cat_totals(m):
            return {r["category"]: r["s"] for r in conn.execute(
                f"""SELECT category, COALESCE({spend},0) AS s FROM transactions
                    WHERE {month_e}=? AND txn_type IN ('purchase','refund')
                    GROUP BY category""", (m,))}
        cur_cat, prev_cat = cat_totals(month), cat_totals(prev)
        movers = []
        for cat in set(cur_cat) | set(prev_cat):
            c = round(cur_cat.get(cat, 0), 2)
            p = round(prev_cat.get(cat, 0), 2)
            if c or p:
                movers.append({"category": cat, "current": c, "previous": p,
                               "delta": round(c - p, 2)})
        movers.sort(key=lambda x: -abs(x["delta"]))

        # --- top merchants (net spend) ---
        top_merchants = [dict(r) for r in conn.execute(
            f"""SELECT COALESCE(merchant_clean, merchant_raw, '(unknown)') AS merchant,
                       COALESCE({spend},0) AS spend, COUNT(*) AS count
                FROM transactions
                WHERE {month_e}=? AND txn_type IN ('purchase','refund')
                GROUP BY lower(COALESCE(merchant_clean, merchant_raw, '(unknown)'))
                HAVING spend > 0 ORDER BY spend DESC LIMIT 8""",
            (month,),
        )]

        # --- recurring: same merchant + same exact amount in >= 3 distinct months ---
        # The exact repeated amount is the signature of a subscription/rent;
        # varying spend at the same shop (groceries) correctly does NOT match.
        rows = conn.execute(
            f"""SELECT lower(COALESCE(merchant_clean, merchant_raw)) AS key,
                       COALESCE(merchant_clean, merchant_raw) AS merchant,
                       amount, {month_e} AS m
                FROM transactions
                WHERE txn_type = 'purchase' AND txn_date IS NOT NULL
                  AND COALESCE(merchant_clean, merchant_raw) IS NOT NULL""",
        ).fetchall()
        groups = {}
        for r in rows:
            g = groups.setdefault((r["key"], round(r["amount"], 2)),
                                  {"merchant": r["merchant"],
                                   "amount": round(r["amount"], 2), "months": set()})
            g["months"].add(r["m"])
        recurring = sorted(
            ({"merchant": g["merchant"], "amount": g["amount"],
              "months_seen": len(g["months"]), "active_this_month": month in g["months"]}
             for g in groups.values() if len(g["months"]) >= 3),
            key=lambda x: -x["amount"],
        )[:10]

        # --- duplicate-alert suspects (flagged for review, never auto-dropped) ---
        duplicate_suspects = [dict(r) for r in conn.execute(
            f"""SELECT t.txn_date, t.amount,
                       COALESCE(t.merchant_clean, t.merchant_raw, '') AS merchant,
                       a.bank, a.last4, COUNT(*) AS count
                FROM transactions t LEFT JOIN accounts a ON t.account_id = a.id
                WHERE {month_t}=? AND t.txn_type='purchase'
                GROUP BY t.account_id, t.amount, t.txn_date,
                         lower(COALESCE(t.merchant_clean, t.merchant_raw, ''))
                HAVING COUNT(*) > 1 ORDER BY t.amount DESC""",
            (month,),
        )]

        biggest = conn.execute(
            f"""SELECT amount, COALESCE(merchant_clean, merchant_raw) AS merchant,
                       txn_date, category
                FROM transactions WHERE {month_e}=? AND txn_type='purchase'
                ORDER BY amount DESC LIMIT 1""",
            (month,),
        ).fetchone()

        daily_current = _daily_cumulative(conn, month)
        daily_previous = _daily_cumulative(conn, prev)

    return {
        "month": month,
        "prev_month": prev,
        "total": total,
        "prev_total": prev_total,
        "mom_delta": round(total - prev_total, 2),
        "mom_pct": (round((total - prev_total) / abs(prev_total) * 100, 1)
                    if prev_total else None),
        "projection": {
            "is_current": is_current,
            "days_elapsed": days_elapsed,
            "days_in_month": days_in_month,
            "daily_rate": round(daily_rate, 2),
            "projected": projected,
            "typical_month": typical,
        },
        "category_movers": movers[:8],
        "top_merchants": top_merchants,
        "recurring": recurring,
        "duplicate_suspects": duplicate_suspects,
        "biggest": dict(biggest) if biggest else None,
        "daily_cumulative": {"current": daily_current, "previous": daily_previous},
    }


# ---------- rules / budgets ----------

def get_category_rules():
    with get_conn() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM category_rules ORDER BY priority DESC, id")]


def get_transfer_rules():
    with get_conn() as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM transfer_rules ORDER BY id")]


def get_amount_rules():
    with get_conn() as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM amount_rules ORDER BY amount")]


def get_income_rules():
    with get_conn() as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM income_rules ORDER BY id")]


def add_income_rule(pattern, note=""):
    with get_conn() as conn:
        conn.execute("INSERT INTO income_rules (pattern, note) VALUES (?,?)", (pattern, note))


def delete_income_rule(rule_id):
    with get_conn() as conn:
        conn.execute("DELETE FROM income_rules WHERE id = ?", (rule_id,))


def add_amount_rule(amount, category, note=""):
    with get_conn() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO amount_rules (amount, category, note) VALUES (?,?,?)",
            (float(amount), category, note),
        )


def delete_amount_rule(rule_id):
    with get_conn() as conn:
        conn.execute("DELETE FROM amount_rules WHERE id = ?", (rule_id,))


def add_category_rule(pattern, category, priority=5):
    with get_conn() as conn:
        conn.execute("INSERT INTO category_rules (pattern, category, priority) VALUES (?,?,?)",
                     (pattern, category, priority))


def delete_category_rule(rule_id):
    with get_conn() as conn:
        conn.execute("DELETE FROM category_rules WHERE id = ?", (rule_id,))


def add_transfer_rule(pattern, note=""):
    with get_conn() as conn:
        conn.execute("INSERT INTO transfer_rules (pattern, note) VALUES (?,?)", (pattern, note))


def delete_transfer_rule(rule_id):
    with get_conn() as conn:
        conn.execute("DELETE FROM transfer_rules WHERE id = ?", (rule_id,))


def get_budgets():
    with get_conn() as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM budgets ORDER BY scope, scope_value")]


def add_budget(scope, scope_value, month_limit):
    with get_conn() as conn:
        conn.execute("INSERT INTO budgets (scope, scope_value, month_limit) VALUES (?,?,?)",
                     (scope, scope_value, month_limit))


def delete_budget(budget_id):
    with get_conn() as conn:
        conn.execute("DELETE FROM budgets WHERE id = ?", (budget_id,))


def get_unparsed(include_reviewed=False, limit=100):
    """Unparsed emails for the panel. By default hides ones the AI already checked
    and found to be non-transactions (they stay in the table for dedup)."""
    with get_conn() as conn:
        where = "" if include_reviewed else "WHERE COALESCE(ai_reviewed,0) = 0"
        return [dict(r) for r in conn.execute(
            f"SELECT * FROM unparsed {where} ORDER BY received_at DESC LIMIT ?",
            (limit,))]


def mark_unparsed_reviewed(email_id):
    """AI checked this email and found no transaction — keep it (dedup) but hide it."""
    with get_conn() as conn:
        conn.execute("UPDATE unparsed SET ai_reviewed = 1 WHERE source_email_id = ?", (email_id,))


def add_ai_usage(model, input_tokens, output_tokens, cost_usd):
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO ai_usage (model, input_tokens, output_tokens, cost_usd) VALUES (?,?,?,?)",
            (model, input_tokens, output_tokens, cost_usd),
        )


def ai_usage_total():
    """Running totals across all AI calls (for the dashboard cost counter)."""
    with get_conn() as conn:
        r = conn.execute(
            """SELECT COUNT(*) AS calls,
                      COALESCE(SUM(input_tokens),0) AS input_tokens,
                      COALESCE(SUM(output_tokens),0) AS output_tokens,
                      COALESCE(SUM(cost_usd),0) AS cost_usd
               FROM ai_usage"""
        ).fetchone()
        return dict(r)


def delete_unparsed(email_id):
    with get_conn() as conn:
        conn.execute("DELETE FROM unparsed WHERE source_email_id = ?", (email_id,))


def txn_by_email_id(email_id):
    with get_conn() as conn:
        r = conn.execute(
            """SELECT amount, COALESCE(merchant_clean, merchant_raw) AS merchant, category, txn_type
               FROM transactions WHERE source_email_id = ?""",
            (email_id,),
        ).fetchone()
        return dict(r) if r else None


def uncategorized_merchants():
    """Distinct merchant names on Uncategorized, countable (purchase/refund) rows."""
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT DISTINCT COALESCE(merchant_clean, merchant_raw) AS m
               FROM transactions
               WHERE category = 'Uncategorized' AND txn_type IN ('purchase','refund')
                 AND COALESCE(merchant_clean, merchant_raw) IS NOT NULL"""
        ).fetchall()
        return [r["m"] for r in rows if r["m"]]


def apply_category_to_merchant(merchant, category):
    """Set category on all Uncategorized rows matching this merchant. Returns count.
    Recomputes each row's attribution month (matters if the category is Rent)."""
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT id, txn_date FROM transactions
               WHERE category = 'Uncategorized'
                 AND COALESCE(merchant_clean, merchant_raw) = ?""",
            (merchant,),
        ).fetchall()
        for r in rows:
            conn.execute("UPDATE transactions SET category = ?, period = ? WHERE id = ?",
                         (category, period_for(r["txn_date"], category), r["id"]))
        return len(rows)
