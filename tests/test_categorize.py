"""Category assignment precedence: amount rules first, then merchant substring
rules by priority, else Uncategorized."""
import categorize


def test_amount_rule_wins_over_everything(fresh_db):
    # 25000 -> Rent from the test config, even for an unknown person payee.
    assert categorize.categorize("SOME NEW LANDLORD", 25000.0) == "Rent"


def test_amount_rule_tolerates_float_drift(fresh_db):
    assert categorize.categorize("X", 25000.004) == "Rent"


def test_merchant_rule_from_config_high_priority(fresh_db):
    assert categorize.categorize("MR LANDLORD", 100.0) == "Rent"


def test_seed_merchant_rules(fresh_db):
    assert categorize.categorize("SWIGGY BANGALORE", 300) == "Food & Dining"
    assert categorize.categorize("BLINKIT", 250) == "Groceries"
    assert categorize.categorize("UBER RIDES", 180) == "Transport"
    assert categorize.categorize("NETFLIX.COM", 649) == "Entertainment"


def test_unknown_merchant_uncategorized(fresh_db):
    assert categorize.categorize("TOTALLY UNKNOWN SHOP", 42) == "Uncategorized"


def test_no_merchant_no_amount(fresh_db):
    assert categorize.categorize(None, None) == "Uncategorized"
