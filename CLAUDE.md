# CLAUDE.md — Monthly Expense Tracker

Guidance for Claude Code (and humans) working in this repo. Read this first.

## What this is

A **local, single-user web app** that tracks monthly spending across the user's
credit cards and savings account **automatically**, by reading bank transaction-alert
**emails** via the read-only Gmail API. No manual statement uploads. Runs on the
user's Mac; nothing leaves the machine except read-only Gmail calls (and, only if the
LLM fallback fires, one unparsed snippet to the local `claude` CLI).

**Why email parsing (not a bank API):** the user is in **India**, where the regulated
Account Aggregator framework can't be consumed by an individual (requires being an
RBI-registered FIU). Indian banks email an alert for nearly every card swipe / account
debit, so email parsing is the reliable, free, individual-usable path.

## THE #1 INVARIANT: never double-count

Every transaction gets a `txn_type`. **Spend = Σ(purchase) − Σ(refund).**
`card_payment` and `transfer` are stored but **never** summed.

**CRED is AMBIGUOUS** (`classify.py:CRED_BILL_THRESHOLD`, default ₹5000): CRED settles
credit-card *bills* but is also CRED Pay (UPI) for Ubers/malls — the email can't tell them
apart. So a CRED debit ≥ threshold → `card_payment` (excluded); below → `purchase` (counted).
Match CRED with a word boundary (`\bcred\b`) so it never matches inside "**cred**it card".
ET Money etc. → `Investment` category (still counts as an outflow). Person-name payees →
`Transport` (UPI to Uber/auto drivers). An Axis-bill payee is handled via a user transfer rule.

**Rent** is a fixed amount paid to a person (landlord) via UPI — a name, so the generic
person-name→Transport heuristic mis-tags it. Handled two ways: a high-priority merchant rule
(landlord name→Rent, beats Transport) AND an **`amount_rules`** table (exact rent amounts →
Rent) via `categorize.categorize(merchant, amount)`, which checks amount rules FIRST. Amount
rules also catch a new landlord whose name isn't known yet. (Real names/amounts live in the
gitignored `config.json`, not here.)

**Incoming money model (`income_rules` table)** — user's rule: *any money coming INTO the
savings account DEDUCTS from total spend* (it offsets what was spent), **except** an
ignore-list of salary / self-transfers / family. So in `classify.finalize_txn_type`, an
incoming savings **credit** defaults to `refund` (SUBTRACTS), and only matches in
`income_rules` become `transfer` (excluded). This auto-handles every friend payback
(any friend) and cash deposits **without naming anyone**.
- Ignore-list: own-bank name (self-transfer) + family members. Lives in `config.json`
  (`income_rules`). Managed via `/income-rules`.
- **Salary:** HDFC does NOT email NEFT-in credits, so salary never appears in the data at
  all — nothing to exclude. (No "salary"/"neft" keyword is used; "neft" was removed as too
  broad — it would wrongly ignore NEFT paybacks.)
- Full payback deducts (not capped) — the fronted money was real spend the user is made
  whole on. Validated by a real case: a large Amazon purchase fronted for a friend, repaid
  in splits over 2 days → nets out.

**PayU as a card-bill rail:** `payuaxisbank` is a transfer_rule — a NetBanking payment
"from A/c XXXX to PAYUAXISBANK" is an Axis card-bill payment → `card_payment` (excluded),
like the `to <BANK> CREDIT CARD` case.

| txn_type       | Example                                                        | Counted? |
|----------------|----------------------------------------------------------------|----------|
| `purchase`     | Card swipe, UPI to a merchant                                  | ✅       |
| `refund`       | Merchant refund / reversal                                     | ➖ subtracts |
| `card_payment` | Paying a card bill from savings (CRED/BillDesk); "payment received" on a card | ❌ excluded |
| `transfer`     | Self-transfer / incoming credit to a bank account             | ❌ excluded |

**Worked example:** pay a ₹40k Axis bill from HDFC savings via CRED → that debit is
`card_payment` (excluded); the granular Axis purchases that summed to ₹40k are the
`purchase` rows that count. Counted once, not twice.

**Where it's enforced:**
- `db.py` — `_signed_spend_sql()` maps purchase→+amount, refund→−amount, else 0;
  all summaries use it.
- `classify.py:finalize_txn_type()` — assigns `card_payment`/`transfer`:
  - A parser-set `refund` is authoritative and never reclassified (prevents a refund
    that says "credited to your card" from being dropped).
  - Transfer/bill patterns are matched against the **narration/snippet, NOT the
    merchant name** (so a merchant named "...card..." isn't excluded).
  - Generic rails (`bbps`, `billdesk`) only count as a card payment when "card" also
    appears — so a real BBPS electricity bill from savings still counts as spend.

When editing anything that touches money, re-run the verification tests below.

## Accounts (loaded from `config.json` → `db.py:SEED_ACCOUNTS`, matched by last4)

Accounts (bank, type, last-4) live in the **gitignored `config.json`** (see
`config.example.json`). The app seeds them on first run and matches each alert to an account
by its last-4. Supported banks today: **HDFC** (cards + savings), **ICICI**, **Axis**, **HSBC**.

If an alert's last4 has no matching account, the txn shows "no account" — add that card to
`config.json` (a new card is often discovered this way from a real transaction).

## Real email formats (ground truth for the parsers)

Tuned from the user's actual inbox via the Gmail MCP. Card/account numbers shown as XXXX.
Senders + body shapes:

- **HDFC** — `alerts@hdfcbank.net` / `alerts@hdfcbank.bank.in` (all 3 cards + savings).
  HDFC uses MANY wordings; `parsers/hdfc.py` tries each. Observed real formats:
  - **Card (POS/online):** `Rs.50.00 is debited from your HDFC Bank Credit Card ending XXXX towards PYU*MERCHANT on 08 Apr, 2026 at 20:10:11.`
  - **Card via UPI (newer):** `Rs.6.00 is debited from your HDFC Bank RuPay Credit Card ending XXXX and credited to VPA paytm-...@ptybl (MERCHANT NAME) on 01 Jun, 2026.`
  - **Savings UPI debit (old):** `Rs.161.00 has been debited from account XXXX to VPA xxx@axl NAME on 06-04-26.`
  - **Savings UPI debit (new .bank.in):** `Rs.517.00 is debited from your account ending XXXX towards VPA xxx@ibl (PAYEE NAME) on 08-06-26.` (note "account ending", "towards VPA", payee in parentheses)
  - **Savings credit (old):** `Rs. 3250.00 is successfully credited to your account **XXXX by VPA xxx NAME on 31-01-26.`
  - **Savings credit (new .bank.in):** `Rs.2000.00 has been successfully credited to your HDFC Bank account ending in XXXX. ... Date: 07-06-26 ... Sender: NAME (VPA: xxx) ...`
  - **🔴 NetBanking card-bill payment:** `Thank you for using HDFC Bank NetBanking for payment of Rs. 654.00 from A/c ****XXXX to <BANK> CREDIT CA...` → parser tags this `card_payment` (EXCLUDED). This is the manual/NetBanking card-payment case (no CRED/BillDesk keyword) — critical for no-double-count.
  - Marketing from `information@hdfcbank.net` — IGNORED. Non-transaction notices that
    share the "Account update" subject (login / T&C-accepted) have no amount, so they
    safely land in `unparsed` rather than being counted.
- **ICICI** — `credit_cards@icici.bank.in`:
  `Your ICICI Bank Credit Card XXXXXX has been used for a transaction of INR 634.00 on May 15, 2026 ... Info: AMAZON PAY IN E COMMERCE.`
  (Marketing from `custcomm`/`customercomm.icicibank.com` — IGNORED.)
- **HSBC** — `hsbc@mail.hsbc.co.in`:
  `your Credit card no ending with XXXX, has been used for INR 2258.36 for payment to MERCHANT on 07 Jun 2026 at 23:21.`
  (Statements from `creditcardstatement@mail.hsbc.co.in` — IGNORED.)
- **Axis** — `alerts@axis.bank.in` (HTML table; flattened):
  `Transaction Amount: INR 730  Merchant Name: BLINKIT  Axis Bank Credit Card No. XXXX  Date & Time: 29-05-2026, 14:15:53 IST`
  (Marketing from `digital.axisbankmail.bank.in` — IGNORED.)
- **CRED** (`cred.club`) — bill-payment confirmations. IGNORED as a sender; the
  matching savings debit is what gets classified as `card_payment`.

**Sender vs. subject ignoring** (`parsers/__init__.py`): some senders (e.g. HSBC's
`hsbc@mail.hsbc.co.in`) send BOTH real transactions and non-transactions (login alerts,
statement-ready notices). So `is_ignored(sender, subject)` also filters by subject via
`_IGNORE_SUBJECTS` ("successful log on", "logged on", "statement ready", "card statement",
"otp", "e-statement", …). Pure marketing senders are filtered by `_IGNORE` (sender).

## Architecture / files

```
app.py            FastAPI: routes + serves the dashboard; background sync thread + progress state
gmail_sync.py     Gmail OAuth (readonly) + fetch + orchestrate parse→classify→categorize→insert
parsers/
  base.py         ParsedTxn dataclass; html_to_text, parse_amount, parse_date, clean_merchant
  __init__.py     sender→parser registry (_ROUTES) + _IGNORE marketing senders
  hdfc.py icici.py hsbc.py axis.py   per-bank regex parsers (built from the formats above)
classify.py       finalize_txn_type() — transfer/bill detection (the no-double-count logic)
categorize.py     merchant → spend category via editable rules
llm.py            claude-CLI fallback: rich prompt + DETERMINISTIC VERIFIER (see below)
db.py             SQLite schema, seed data, dedupe insert, summary/trend queries
static/index.html single-page dashboard (vanilla JS + Chart.js via CDN)
```

**Data flow per email:** `sync()` lists message IDs (for the progress total) → for each:
fetch → route to bank parser (regex) → if None and enabled, LLM fallback → resolve
account by last4 → `finalize_txn_type` → categorize → `insert_txn` (dedupe on Gmail
message id). Unparseable / date-less alerts go to the `unparsed` table, never the totals.

## LLM fallback safety (`llm.py`) — and it is ON-DEMAND, not during sync

**Sync is regex-only and fast.** The LLM does NOT run automatically during sync (that was
slow and spent tokens on every promo/OTP). Instead:
- Unparseable alerts go to the `unparsed` table and show in the dashboard's unparsed panel.
- Each unparsed row has an **"✨ Extract with AI"** button (`POST /unparsed/{id}/retry-llm`),
  and there's a bulk **"✨ Extract all with AI"** (`POST /unparsed/retry-all-llm`, runs in the
  background with the same progress bar). The user explicitly chooses when to spend AI.
- **After AI review, rows leave the panel.** Extracted → deleted from `unparsed` (now a txn).
  Not-a-transaction → `unparsed.ai_reviewed=1` (kept for dedup so re-sync won't re-add it, but
  hidden from the panel — `get_unparsed()` filters `ai_reviewed=0`). So "Extract all" empties
  the panel. The panel also shows a heuristic **type tag** per row (login/statement/OTP/
  possible-transaction) so junk is obvious without spending AI.
- `_ingest_one(..., use_llm=False)` by default; the retry paths pass `use_llm=True`.
  `classify_with_llm(..., force=True)` bypasses the `LLM_FALLBACK` env toggle because a
  click is explicit consent.

**Model + cost.** AI calls use `claude -p --model sonnet --output-format json` (Sonnet ≈5×
cheaper than Opus; override via `AI_MODEL` env). `llm._run_claude()` parses `total_cost_usd` +
token `usage` from the JSON envelope and records each call in the `ai_usage` table. `GET
/ai-usage` returns running totals; the dashboard header shows a **🤖 cost counter**
(`$cost · tokens · calls`). Cost tracking is best-effort and never breaks extraction.

Principle: **the LLM proposes, a deterministic verifier disposes.** Every returned field is
validated before it can enter totals:
- amount must appear **verbatim** in the email (Indian digit-grouping aware) and be in
  sane bounds; rejected if it matches a **credit-limit** figure (the classic mis-pick).
- `direction`/`txn_type` validated against enums; `last4` must be 4 digits present in
  the email; `txn_date` must be a real `YYYY-MM-DD`.
- Any failure → `None` → stays in `unparsed`. LLM rows are tagged `parsed_by='llm'`,
  `confidence=0.6`. Disable auto-eligibility with `LLM_FALLBACK=0` (on-demand still works).

## Sync performance & progress

- **Background thread + progress bar.** `POST /sync` starts a thread and returns immediately;
  the UI polls `GET /sync/status` (`{running, done, total, phase, result, error, feed, cancelled}`)
  to drive a progress bar. Same mechanism powers bulk AI retry and categorization.
- **Stop + live feed (AI bulk jobs).** `POST /sync/stop` sets a cancel `threading.Event`; the
  bulk loop checks it between emails and halts cleanly (finishes the current one). The status
  `feed` carries per-email results (`{ok, subject, detail}`) so the UI streams each extraction
  live (✓ ₹X MERCHANT → Category, or ○ not a transaction) — letting the user watch and Stop
  once it's clearly just processing junk (login/account-update notices).
- **Batched Gmail fetch.** Phase 1 lists all matching message IDs (for the total); Phase 2
  fetches them via Gmail **batch HTTP requests** (`_BATCH_SIZE = 50`) instead of one call per
  message — ~10× fewer round-trips. Already-seen IDs are skipped *before* fetching, so
  re-syncs are near-instant (dedupe on Gmail message id).
- **`max_messages` cap** in `sync()` (default 500). Raise it for deep historical syncs; the
  30-day window is well under it.

## Period selection & transaction filters

- `GET /months` returns distinct `YYYY-MM` with data; header has a **month dropdown** (defaults
  to newest month). The free `<input type=month>` is hidden — it's the internal state holder
  (`#month`) the JS reads; the dropdown drives it. (Free date-typing was removed by request.)
- **Date range:** a **📅 Range** toggle swaps the dropdown for From→To month pickers. Backend
  `/summary` and `/transactions` accept EITHER `?month=YYYY-MM` OR `?from=&to=` (range wins).
  `db.summary_for_range` / `transactions_for_range` use `substr(txn_date,1,7) BETWEEN ? AND ?`
  (YYYY-MM sorts lexicographically). `summary_for_month` is now a thin wrapper.
- **Transaction filters** (client-side over the already-fetched rows, instant): search merchant,
  account, category, type, min/max ₹, from/to date, and a **⚑ big ≥ threshold** (default ₹50k)
  with a "big only" checkbox. Big rows get an amber ⚑ tag + row highlight for review. A count
  line shows "X of Y" and the filtered net spend.

## DB schema (SQLite, `expenses.db`)

`transactions` (dedupe key `source_email_id UNIQUE`), `accounts`, `category_rules`,
`transfer_rules`, `amount_rules`, `income_rules`, `unparsed`, `ai_usage`. (`budgets` table
still exists + endpoints, but the **Budgets UI was removed** — unused.) See `db.py` for columns.
Rules are user-editable; `income_rules` = incoming-credit ignore-list (salary/self/family).

## Run

```bash
cd /path/to/expense-tracker
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
# credentials.json already present (Google OAuth desktop client). See README for setup.
uvicorn app:app --reload          # first sync opens a browser for Google consent
# open http://127.0.0.1:8000 → "Sync now"
```

`LLM_FALLBACK=0 uvicorn app:app` to disable the AI fallback (fully offline except Gmail).

## Verification (run after any money-touching change)

The repo has no formal test suite yet; these are the manual checks that must pass:
1. **Double-count:** purchases counted once; a CRED/BillDesk card-bill debit from savings
   is `card_payment` and excluded. Total = Σ purchases − Σ refunds.
2. **BBPS not over-excluded:** a plain BBPS electricity bill from savings (no "card"
   context) counts as spend.
3. **Refund stays refund:** a refund mentioning "credited to your card" must subtract,
   not be reclassified to `card_payment`.
4. **Parsers:** every format in "Real email formats" extracts correct
   amount/last4/merchant/date — including HDFC's UPI-on-card, new `.bank.in` debit/credit,
   and the NetBanking card-payment variant.
5. **NetBanking card-bill → excluded:** `payment of Rs.X from A/c XXXX to <BANK> CREDIT CARD`
   classifies as `card_payment`.
6. **LLM verifier:** fabricated amounts and credit-limit figures are rejected.
7. **Incoming-money model:** a friend payback (UPI credit not in `income_rules`) → `refund`
   (deducts); a family/Kotak/self credit → `transfer` (ignored). Range total = Σ months.
8. Server boots; `/months`, `/summary` (both `?month=` and `?from=&to=`), `/sync/status`
   respond; donut slices sum to total; ⚑ big rows flag; unparsed panel renders.

**UI:** redesigned to match the user's "Aryan_Website" aesthetic (navy + blue/cyan/purple
gradients, glassy gradient-border cards, Exo 2/Inter/Roboto Mono). Pure reskin of
`static/index.html`; all logic preserved.

**Live tuning result (30-day sync of the user's inbox):** 147 emails → 100 parsed by regex,
0 via AI, only 1 unparsed (a login/T&C notice with no amount). Reached after iteratively
reading the unparsed emails via the Gmail MCP and adding the HDFC format variants above.
When adding a new bank/format, repeat: sync → inspect `unparsed` → read real bodies → add a
parser branch or an ignore rule → re-sync.

## Known limitations (by design; document, don't silently fix)

- **Manual card payments:** HDFC NetBanking card-bill payments ("...payment of Rs.X from
  A/c XXXX to <BANK> CREDIT CARD") ARE now auto-excluded by the parser. CRED/BillDesk/BBPS-
  with-card are handled too. A truly unlabelled NEFT/IMPS to a card (no "credit card" text
  and no known biller) could still slip through → user adds a transfer rule for that payee,
  or flips the row's type in the UI.
- **Timezone:** `txn_date` is the bank's local date; a late-night txn may fall in an
  adjacent month.
- **Coverage = email alerts only.** If a card only sends SMS, enable email alerts in
  net-banking (see README).

## Conventions

- Match the existing code's style: small focused modules, regex parsers return
  `ParsedTxn | None`, comments explain *why* (esp. the financial-correctness bits).
- New banks: add a `parsers/<bank>.py`, register its alert sender in
  `parsers/__init__.py:_ROUTES`, add the account to `SEED_ACCOUNTS`, and add a sample to
  the verification checks.
- UI is themed after the user's "Aryan_Website" (`~/Aryan_Website`): navy `#050510`,
  blue/cyan/purple gradients, glassy gradient-border cards, Exo 2 / Inter / Roboto Mono.
  Keep new UI consistent with that language.

---

## Appendix: original build plan

The full design/▶remediation plan is saved at
`~/.claude/plans/radiant-crafting-eagle.md`. It covers: why email parsing (India/AA),
the hybrid regex+LLM engine, the transfer-exclusion model, the expert-review findings
(financial / parsing / analysis) and the fixes applied, and the verification steps.
