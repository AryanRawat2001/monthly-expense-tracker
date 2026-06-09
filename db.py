"""SQLite storage layer for the Monthly Expense Tracker.

One local file (expenses.db). Holds transactions, accounts, editable rules,
budgets, and an `unparsed` log for alerts we couldn't parse.

The golden rule lives here: spend queries count ONLY txn_type in
('purchase','refund'). card_payment / transfer are stored but never summed,
so paying a card bill from savings never double-counts the underlying spends.
"""
import json
import sqlite3
from pathlib import Path
from contextlib import contextmanager

DB_PATH = Path(__file__).parent / "expenses.db"
CONFIG_PATH = Path(__file__).parent / "config.json"

# txn_type values that represent real spending (everything else is excluded)
SPEND_TYPES = ("purchase", "refund")

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


@contextmanager
def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
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
                txn_date TEXT,                 -- ISO date YYYY-MM-DD
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
    """Distinct YYYY-MM that have at least one transaction, newest first."""
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT DISTINCT substr(txn_date,1,7) AS month FROM transactions
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
                   (txn_date, posted_at, amount, direction, txn_type, account_id,
                    merchant_raw, merchant_clean, category, source_email_id,
                    parsed_by, confidence, raw_snippet)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    txn.get("txn_date"), txn.get("posted_at"), txn["amount"],
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


def log_unparsed(email_id, sender, subject, snippet, received_at):
    with get_conn() as conn:
        conn.execute(
            """INSERT OR IGNORE INTO unparsed
               (source_email_id, sender, subject, snippet, received_at)
               VALUES (?,?,?,?,?)""",
            (email_id, sender, subject, snippet, received_at),
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
        if txn_type is not None:
            conn.execute("UPDATE transactions SET txn_type = ? WHERE id = ?", (txn_type, txn_id))


def transactions_for_month(month: str):
    """All transactions in YYYY-MM, joined with account info, newest first."""
    return transactions_for_range(month, month)


def transactions_for_range(start: str, end: str):
    """All transactions in the inclusive YYYY-MM range, newest first."""
    if end < start:
        start, end = end, start
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT t.*, a.name AS account_name, a.bank, a.last4, a.type AS account_type
               FROM transactions t LEFT JOIN accounts a ON t.account_id = a.id
               WHERE substr(t.txn_date,1,7) BETWEEN ? AND ?
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
        rng = (start, end)

        by_account = conn.execute(
            f"""SELECT a.id, a.name, a.bank, a.last4, a.type,
                       COALESCE({spend},0) AS spend
                FROM accounts a
                LEFT JOIN transactions t
                       ON t.account_id = a.id
                      AND substr(t.txn_date,1,7) BETWEEN ? AND ?
                GROUP BY a.id ORDER BY spend DESC""",
            rng,
        ).fetchall()

        # Donut shows positive net spend only. A category that nets negative
        # (refunds > purchases) can't be drawn as a pie slice, so exclude it here.
        by_category = conn.execute(
            f"""SELECT category, COALESCE({spend},0) AS spend
                FROM transactions
                WHERE substr(txn_date,1,7) BETWEEN ? AND ? AND txn_type IN ('purchase','refund')
                GROUP BY category HAVING spend > 0 ORDER BY spend DESC""",
            rng,
        ).fetchall()

        total = conn.execute(
            f"""SELECT COALESCE({spend},0) AS spend FROM transactions
                WHERE substr(txn_date,1,7) BETWEEN ? AND ?""",
            rng,
        ).fetchone()["spend"]

        excluded = conn.execute(
            """SELECT COUNT(*) AS n, COALESCE(SUM(amount),0) AS amt FROM transactions
               WHERE substr(txn_date,1,7) BETWEEN ? AND ? AND txn_type IN ('card_payment','transfer')""",
            rng,
        ).fetchone()

        # Refunds/reimbursements: money that came back, reducing spend.
        refunds = conn.execute(
            """SELECT COUNT(*) AS n, COALESCE(SUM(amount),0) AS amt FROM transactions
               WHERE substr(txn_date,1,7) BETWEEN ? AND ? AND txn_type = 'refund'""",
            rng,
        ).fetchone()

        # Spend per (account, category) — drives the stacked bar chart.
        acct_cat = conn.execute(
            f"""SELECT a.last4, a.bank, t.category, COALESCE({spend},0) AS spend
                FROM transactions t JOIN accounts a ON t.account_id = a.id
                WHERE substr(t.txn_date,1,7) BETWEEN ? AND ? AND t.txn_type IN ('purchase','refund')
                GROUP BY a.id, t.category HAVING spend > 0""",
            rng,
        ).fetchall()

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
        }


def monthly_trend(limit_months=6):
    """Total spend per month for the most recent N months (for the trend chart)."""
    with get_conn() as conn:
        spend = _signed_spend_sql()
        rows = conn.execute(
            f"""SELECT substr(txn_date,1,7) AS month, COALESCE({spend},0) AS spend
                FROM transactions
                WHERE txn_date IS NOT NULL AND txn_type IN ('purchase','refund')
                GROUP BY month ORDER BY month DESC LIMIT ?""",
            (limit_months,),
        ).fetchall()
        return [dict(r) for r in reversed(rows)]


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


def get_unparsed(include_reviewed=False):
    """Unparsed emails for the panel. By default hides ones the AI already checked
    and found to be non-transactions (they stay in the table for dedup)."""
    with get_conn() as conn:
        where = "" if include_reviewed else "WHERE COALESCE(ai_reviewed,0) = 0"
        return [dict(r) for r in conn.execute(
            f"SELECT * FROM unparsed {where} ORDER BY received_at DESC LIMIT 100")]


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
    """Set category on all Uncategorized rows matching this merchant. Returns count."""
    with get_conn() as conn:
        cur = conn.execute(
            """UPDATE transactions SET category = ?
               WHERE category = 'Uncategorized'
                 AND COALESCE(merchant_clean, merchant_raw) = ?""",
            (category, merchant),
        )
        return cur.rowcount
