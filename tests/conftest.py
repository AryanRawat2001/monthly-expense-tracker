"""Test bootstrap: point the app at a throwaway DB + config BEFORE any project
import, so tests can never touch the real expenses.db / config.json.
"""
import json
import os
import sys
import tempfile
from pathlib import Path

import pytest

# Make the project root importable when pytest is run from anywhere.
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

_SESSION_DIR = Path(tempfile.mkdtemp(prefix="expense-tests-"))

TEST_CONFIG = {
    "accounts": [
        {"name": "HDFC Credit Card", "type": "credit_card", "bank": "HDFC", "last4": "1234"},
        {"name": "HDFC Savings", "type": "savings", "bank": "HDFC", "last4": "9999"},
        {"name": "ICICI Credit Card", "type": "credit_card", "bank": "ICICI", "last4": "5678"},
        {"name": "Axis Credit Card", "type": "credit_card", "bank": "Axis", "last4": "4321"},
        {"name": "HSBC Credit Card", "type": "credit_card", "bank": "HSBC", "last4": "7777"},
    ],
    "category_rules": [["mr landlord", "Rent", 25]],
    "amount_rules": [[25000, "Rent", "monthly rent"]],
    "income_rules": [["kotak", "self-transfer"], ["papa", "family"]],
}

_config_file = _SESSION_DIR / "config.json"
_config_file.write_text(json.dumps(TEST_CONFIG))

# Must be set before db/app are imported anywhere.
os.environ["EXPENSES_DB"] = str(_SESSION_DIR / "session.db")
os.environ["EXPENSES_CONFIG"] = str(_config_file)

import db  # noqa: E402  (import after env setup is the whole point)


@pytest.fixture
def fresh_db(tmp_path, monkeypatch):
    """A brand-new, seeded database for one test."""
    path = tmp_path / "test.db"
    monkeypatch.setattr(db, "DB_PATH", path)
    db.init_db()
    return path


def account_by_last4(last4):
    return next(a for a in db.get_accounts() if a["last4"] == last4)
