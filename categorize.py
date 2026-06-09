"""Assign a spending category to a transaction.

Order of precedence:
1. EXACT-AMOUNT rules (e.g. fixed monthly rent paid to a person via UPI — the
   amount is the reliable signal when the payee is just a name).
2. MERCHANT substring rules (highest priority first).
3. 'Uncategorized'.

Rules live in the DB so the user can edit them from the dashboard.
"""
from db import get_category_rules, get_amount_rules


def categorize(merchant: str | None, amount: float | None = None) -> str:
    # 1. Exact-amount match (rent, etc.). Compared with a small tolerance for floats.
    if amount is not None:
        for rule in get_amount_rules():
            if abs(rule["amount"] - amount) < 0.5:
                return rule["category"]

    # 2. Merchant substring rules.
    if merchant:
        m = merchant.lower()
        for rule in get_category_rules():        # ordered by priority DESC
            if rule["pattern"].lower() in m:
                return rule["category"]

    return "Uncategorized"
