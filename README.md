# 💸 Expense Tracker

A **local, private** web app that automatically tracks your monthly spending across all your
credit cards and savings account — by reading your bank's **transaction-alert emails** (via
read-only Gmail). No manual statement uploads. Nothing leaves your machine except read-only
Gmail API calls (and, only if you opt in, one unparsed email to your local `claude` CLI).

Built for Indian banks (**HDFC, ICICI, Axis, HSBC**), with category graphs and — crucially —
**correct handling of credit-card bill payments and reimbursements so your spend is never
double-counted**.

> **Privacy:** your transactions live in a local SQLite file, and your personal details
> (accounts, names, amounts) live in a gitignored `config.json`. Secrets (`credentials.json`,
> `token.json`) and your data are never committed. See [SETUP.md](SETUP.md).

---

## Features

- **Auto-sync** transaction alerts from Gmail (read-only OAuth) — fast, regex-based, free.
- **No double-counting:** every transaction is typed `purchase` / `refund` / `card_payment`
  / `transfer`; only `Spend = Σ purchases − Σ refunds` is counted. Card-bill payments and
  self-transfers are excluded.
- **Reimbursements:** money friends send back **deducts** from your spend automatically
  (salary / self-transfers / family are ignored via a small editable list).
- **Smart categorization** with editable rules + an optional on-demand **AI** pass
  (local `claude` CLI, with a deterministic verifier so it can't hallucinate amounts).
- **Dashboard:** category donut, per-account stacked bar, monthly trend, single-month or
  date-range views, and a filterable transactions table (account / category / type / amount /
  date / ⚑ big-amount flag).
- **Cost counter** for any AI usage; **Stop** button + live feed for bulk AI jobs.

---

## Quick start

```bash
git clone https://github.com/aryanrawat2001/<repo>.git
cd <repo>
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt

cp config.example.json config.json     # then edit with YOUR accounts/rules
# add your Google OAuth credentials.json (see SETUP.md)

uvicorn app:app --reload
```

Open **http://127.0.0.1:8000** → click **Sync now**. The first sync opens a browser for
read-only Gmail consent; after that it's one click.

**Full step-by-step (Google Cloud OAuth, config, bank email alerts):** see **[SETUP.md](SETUP.md)**.

---

## How it stays accurate (no double-counting)

| txn_type | Example | Counted? |
|----------|---------|----------|
| `purchase` | Card swipe, UPI to a merchant | ✅ counts |
| `refund` | Merchant refund, or a friend paying you back | ➖ subtracts |
| `card_payment` | Paying a card bill (CRED/BillDesk/NetBanking) | ❌ excluded |
| `transfer` | Self-transfer, salary, family support | ❌ excluded |

So if you pay a ₹40k card bill from savings, that payment is **excluded** — only the
underlying card purchases (already captured) count. The ₹40k is counted once.

You can fix any row's **category** or **type** from the dashboard, and edit the rules
(`config.json` + the in-app rule endpoints).

---

## Privacy & the optional AI

- Default operation is fully local: read-only Gmail + a local SQLite file (`expenses.db`).
- For alerts the built-in parsers can't read, an **opt-in** AI pass calls your local
  `claude` CLI (no API key) on just that one email; every value it returns is verified
  against the email before use. Turn it off entirely with `LLM_FALLBACK=0 uvicorn app:app`.

---

## Limitations (honest)

- Captures only what your bank **emails**. SMS-only alerts are missed (enable email alerts).
- Some banks don't email incoming **NEFT** (e.g. salary) — so those simply never appear.
- Tuned for HDFC / ICICI / Axis / HSBC India alert formats; other banks need a new parser.

---

## Project layout

See **[CLAUDE.md](CLAUDE.md)** for architecture, the real per-bank email formats, the
data-flow, and the financial-correctness rules.

```
app.py  gmail_sync.py  classify.py  categorize.py  llm.py  db.py
parsers/{base,__init__,hdfc,icici,hsbc,axis}.py
static/index.html
config.example.json   # copy to config.json (gitignored) with your details
```

## License

MIT — personal project, use at your own discretion. Not affiliated with any bank.
