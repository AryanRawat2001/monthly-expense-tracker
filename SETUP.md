# Setup Guide

Step-by-step to get the Expense Tracker running on your own machine. ~15 minutes,
all free. Prerequisites: **Python 3.10+** and a **Gmail account** that receives your
bank's transaction-alert emails.

---

## 1. Install

```bash
git clone https://github.com/AryanRawat2001/monthly-expense-tracker.git
cd monthly-expense-tracker
python3 -m venv venv
source venv/bin/activate          # Windows: venv\Scripts\activate
pip install -r requirements.txt
```

---

## 2. Configure your accounts (`config.json`)

Your personal details (accounts, names, rent amounts) live in `config.json`, which is
**gitignored** and never committed. Start from the template:

```bash
cp config.example.json config.json
```

Edit `config.json`:

- **`accounts`** — one entry per card / savings account. The `last4` is how the app
  matches an email alert to an account:
  ```json
  {"name": "HDFC Credit Card", "type": "credit_card", "bank": "HDFC", "last4": "1234"}
  ```
  `type` is `credit_card` or `savings`. Supported banks: HDFC, ICICI, Axis, HSBC.
- **`category_rules`** — personal payees → category, e.g. your landlord → Rent, family →
  Family. Format `["pattern", "Category", priority]` (higher priority wins; rent/family
  should beat the generic person-name→Transport guess).
- **`amount_rules`** — exact recurring amounts → category (great for rent paid to a
  person). Format `[amount, "Category", "note"]`.
- **`income_rules`** — incoming credits to IGNORE (not treated as a payback that reduces
  spend): your salary sender, your own other-bank name for self-transfers, and family.
  **Everything else coming in is auto-deducted from spend** — no need to list friends.

You can also edit accounts/rules later from the app; `config.json` just seeds the first run.

---

## 3. Get Google OAuth credentials (`credentials.json`) — free, ~10 min

The app reads Gmail via Google's API using a free "Desktop app" OAuth credential.

1. Go to **https://console.cloud.google.com** → **create a new project** (e.g. "expense-tracker").
2. **APIs & Services → Library** → search **Gmail API** → **Enable**.
3. **APIs & Services → OAuth consent screen**:
   - User type: **External** → Create.
   - Fill App name + your email for the support/developer fields.
   - On **Test users**, click **+ Add users** and add **your own Gmail address**.
   - You can leave publishing status as **Testing**, BUT Google then expires your
     login every **7 days** (the app recovers by re-opening the consent tab). To log
     in once and be done, click **Publish app** (→ In production) — no verification
     is needed for your own personal use; the consent page just shows an
     "unverified app" warning you click through.
4. **APIs & Services → Credentials → Create Credentials → OAuth client ID**:
   - Application type: **Desktop app** → Create.
5. **Download** the JSON and save it in the project folder as **`credentials.json`**.

> `credentials.json` and the `token.json` created on first login are **gitignored** —
> never commit them.

---

## 4. Run

```bash
uvicorn app:app --reload
```

Open **http://127.0.0.1:8000** and click **Sync now**.

- The **first sync** opens a browser for Google consent. You'll see a *"Google hasn't
  verified this app"* warning (expected, since your consent screen is in Testing) →
  **Advanced → Go to (app) → Allow**. A `token.json` is cached so you won't sign in again.
- After that, every sync is one click. Re-syncing is safe (deduped by Gmail message id).

---

## 5. Make sure your bank sends EMAIL alerts

The app only sees transactions your bank **emails** you. Most Indian banks do this by
default; some only SMS. In each bank's net-banking / app, enable **email** transaction
alerts for every card and your savings account. (Or forward bank SMS → email.)

Senders the app reads: `alerts@hdfcbank.net` / `@hdfcbank.bank.in`,
`credit_cards@icici.bank.in`, `alerts@axis.bank.in`, `hsbc@mail.hsbc.co.in`.
Marketing senders are ignored.

> Note: some banks don't email incoming **NEFT** (e.g. salary) at all — those transactions
> simply never appear, which is usually what you want for a *spend* tracker.

---

## 6. (Optional) AI fallback

For the rare alert the built-in parsers can't read, an opt-in AI pass can extract it using
your local **`claude` CLI** (no API key). It's **off during normal sync** — you trigger it
per-email from the "Unparsed" panel. To disable entirely:

```bash
LLM_FALLBACK=0 uvicorn app:app --reload
```

Use Sonnet (default, cheap) or override: `AI_MODEL=opus uvicorn app:app`.

---

## Troubleshooting

- **403 access_denied on consent** → you didn't add your email under OAuth consent screen →
  **Test users**. Add it and retry.
- **Sync fails with `invalid_grant` / re-asks for consent weekly** → your OAuth app is in
  **Testing** mode (7-day token expiry). Publish it to Production (see step 3).
- **"no account" on a transaction** → that card's `last4` isn't in `config.json`; add it.
- **A transaction is mis-categorized or counted wrong** → fix its category/type inline in
  the table, or add a rule in `config.json`.
- **Reset everything** → delete `expenses.db` and re-sync (your `config.json` is untouched).
