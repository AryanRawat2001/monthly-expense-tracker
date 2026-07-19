"""Every documented real email format must extract the right
amount / last4 / merchant / date / direction / provisional type.
These samples mirror CLAUDE.md's "Real email formats" section (numbers faked).
"""
import parsers
from parsers import hdfc, icici, hsbc, axis
from parsers.base import html_to_text, parse_amount, parse_date, clean_merchant


# ---------- HDFC ----------

def test_hdfc_card_pos():
    body = ("Dear Customer, Rs.50.00 is debited from your HDFC Bank Credit Card "
            "ending 1234 towards PYU*BIGTREE ENTERTAINMENT on 08 Apr, 2026 at 20:10:11. "
            "Your available limit is Rs.1,00,000.00.")
    t = hdfc.parse("Alert: card transaction", body)
    assert t is not None
    assert t.amount == 50.0
    assert t.last4 == "1234"
    assert t.merchant_raw == "BIGTREE ENTERTAINMENT"
    assert t.txn_date == "2026-04-08"
    assert (t.direction, t.txn_type) == ("debit", "purchase")


def test_hdfc_card_upi():
    body = ("Rs.6.00 is debited from your HDFC Bank RuPay Credit Card ending 1234 "
            "and credited to VPA paytm-12345@ptybl (ADANI ONE) on 01 Jun, 2026.")
    t = hdfc.parse("UPI txn", body)
    assert t is not None
    assert t.amount == 6.0
    assert t.last4 == "1234"
    assert t.merchant_raw == "ADANI ONE"
    assert t.txn_date == "2026-06-01"
    assert (t.direction, t.txn_type) == ("debit", "purchase")


def test_hdfc_savings_upi_debit_old():
    body = ("Dear Customer, Rs.161.00 has been debited from account 9999 to "
            "VPA merchant@axl JOHN DOE on 06-04-26. Your UPI transaction reference number is 1234.")
    t = hdfc.parse("You have done a UPI txn", body)
    assert t is not None
    assert t.amount == 161.0
    assert t.last4 == "9999"
    assert t.merchant_raw == "JOHN DOE"
    assert t.txn_date == "2026-04-06"
    assert (t.direction, t.txn_type) == ("debit", "purchase")


def test_hdfc_savings_upi_debit_new_bank_in():
    body = ("Rs.517.00 is debited from your account ending 9999 towards "
            "VPA merchant123@ybl (BOMBAY VADAPAV) on 08-06-26.")
    t = hdfc.parse("UPI debit", body)
    assert t is not None
    assert t.amount == 517.0
    assert t.last4 == "9999"
    assert t.merchant_raw == "BOMBAY VADAPAV"
    assert t.txn_date == "2026-06-08"


def test_hdfc_savings_credit_old():
    body = ("Rs. 3250.00 is successfully credited to your account **9999 by "
            "VPA friend@okhdfc JANE DOE on 31-01-26.")
    t = hdfc.parse("credit alert", body)
    assert t is not None
    assert t.amount == 3250.0
    assert t.last4 == "9999"
    assert t.merchant_raw == "JANE DOE"
    assert t.txn_date == "2026-01-31"
    assert (t.direction, t.txn_type) == ("credit", "transfer")


def test_hdfc_savings_credit_new_bank_in():
    body = ("Dear Customer, Rs.2000.00 has been successfully credited to your "
            "HDFC Bank account ending in 9999. Details: Date: 07-06-26 "
            "Sender: RAHUL SHARMA (VPA: rahul@upi) UPI Reference number 555.")
    t = hdfc.parse("credit alert", body)
    assert t is not None
    assert t.amount == 2000.0
    assert t.last4 == "9999"
    assert t.merchant_raw == "RAHUL SHARMA"
    assert t.txn_date == "2026-06-07"
    assert t.direction == "credit"


def test_hdfc_netbanking_card_bill_is_card_payment():
    body = ("Dear Customer, Thank you for using HDFC Bank NetBanking for payment "
            "of Rs. 654.00 from A/c ****9999 to ICICI BANK CREDIT CARD. "
            "As a thank you gesture...")
    t = hdfc.parse("NetBanking payment", body)
    assert t is not None
    assert t.amount == 654.0
    assert t.last4 == "9999"
    assert t.txn_type == "card_payment"      # the no-double-count case
    assert t.direction == "debit"


def test_hdfc_netbanking_non_card_payee_counts_as_spend():
    body = ("Thank you for using HDFC Bank NetBanking for payment of Rs. 900.00 "
            "from A/c ****9999 to BESCOM ELECTRICITY. As a thank you gesture...")
    t = hdfc.parse("NetBanking payment", body)
    assert t is not None
    assert t.txn_type == "purchase"


def test_hdfc_card_new_noticed_wording():
    """Format HDFC started sending ~Jul 2026 ("We noticed a transaction")."""
    body = ("Dear Customer, Greetings from HDFC Bank. Thank you for using your "
            "HDFC Bank Credit Card ending in 4242 .You made a transaction of "
            "Rs. 295.00 at RAZ*Swiggy on 11-07-2026 20:07:07 . Authorization code: 014658")
    t = hdfc.parse("We noticed a transaction on your Credit Card", body)
    assert t is not None
    assert t.amount == 295.0
    assert t.last4 == "4242"
    assert t.merchant_raw == "Swiggy"          # RAZ* gateway prefix stripped
    assert t.txn_date == "2026-07-11"
    assert (t.direction, t.txn_type) == ("debit", "purchase")


def test_hdfc_rupay_upi_new_table_wording():
    """RuPay-card UPI format from ~Jun 2026: no 'ending', bare VPA payee."""
    body = ("Dear Customer, Greetings from HDFC Bank! We're sharing this alert to help "
            "you quickly check a recent UPI transaction made using your RuPay Credit Card. "
            "Transaction Details: Rs.191.00 has been debited from your RuPay Credit Card "
            "1729 Paid to q900011122@okbank Date: 16-06-26 UPI Transaction Reference Number: 653300553318")
    t = hdfc.parse("❗ You have done a UPI txn. Check details!", body)
    assert t is not None
    assert t.amount == 191.0
    assert t.last4 == "1729"
    assert t.merchant_raw == "q900011122@okbank"
    assert t.txn_date == "2026-06-16"
    assert (t.direction, t.txn_type) == ("debit", "purchase")


def test_hdfc_marketing_returns_none():
    assert hdfc.parse("Great offers inside!", "Get 10x rewards this month!") is None


# ---------- ICICI ----------

ICICI_BODY = ("Dear Customer, Your ICICI Bank Credit Card XX5678 has been used for "
              "a transaction of INR 634.00 on May 15, 2026 at 10:06:48. "
              "Info: AMAZON PAY IN E COMMERCE. "
              "The Available Credit Limit on your card is INR 2,58,712.00.")


def test_icici_card():
    t = icici.parse("Transaction alert", ICICI_BODY)
    assert t is not None
    assert t.amount == 634.0                  # NOT the 2,58,712 credit limit
    assert t.last4 == "5678"
    assert t.merchant_raw == "AMAZON PAY IN E COMMERCE"
    assert t.txn_date == "2026-05-15"
    assert (t.direction, t.txn_type) == ("debit", "purchase")


def test_icici_payment_received_is_excluded_card_payment():
    """ICICI 'payment received' (from Jul 2026) is a card-BILL payment: it must
    be card_payment (excluded), and the last4 is the FINAL digit group of the
    masked number (4001 XXXX XXXX 6002 -> 6002, not 4001)."""
    body = ("Dear Customer, Jul 15, 2026 Greetings from ICICI bank! We have received "
            "payment of INR 724 on your ICICI Bank Credit Card account 4001 XXXX XXXX 6002 "
            "on 15-JUL-26 through Click to Pay. Thank you.")
    t = icici.parse("Payment of INR 724 received on your ICICI Bank Credit Card", body)
    assert t is not None
    assert t.amount == 724.0
    assert t.last4 == "6002"
    assert t.txn_date == "2026-07-15"
    assert (t.direction, t.txn_type) == ("credit", "card_payment")


def test_icici_refund_wording_flips_to_refund():
    body = ("Your ICICI Bank Credit Card XX5678 has been used for a transaction of "
            "INR 200.00 on May 16, 2026 at 10:00:00. Info: SWIGGY. "
            "This amount has been reversed and credited to your card.")
    t = icici.parse("alert", body)
    assert t is not None
    assert (t.direction, t.txn_type) == ("credit", "refund")


# ---------- HSBC ----------

def test_hsbc_card():
    body = ("We write to confirm that your Credit card no ending with 7777, has been "
            "used for INR 2258.36 for payment to DISTRICT MOVIES on 07 Jun 2026 at 23:21.")
    t = hsbc.parse("transaction confirmation", body)
    assert t is not None
    assert t.amount == 2258.36
    assert t.last4 == "7777"
    assert t.merchant_raw == "DISTRICT MOVIES"
    assert t.txn_date == "2026-06-07"


# ---------- Axis ----------

AXIS_HTML = """
<table>
<tr><td>Transaction Amount:</td><td>INR 730</td></tr>
<tr><td>Merchant Name:</td><td>BLINKIT</td></tr>
<tr><td>Axis Bank Credit Card No.</td><td>XX4321</td></tr>
<tr><td>Date &amp; Time:</td><td>29-05-2026, 14:15:53 IST</td></tr>
</table>
"""


def test_axis_card_html_table():
    t = axis.parse("Transaction alert on Axis Bank Credit Card no. XX4321", AXIS_HTML)
    assert t is not None
    assert t.amount == 730.0
    assert t.last4 == "4321"
    assert t.merchant_raw == "BLINKIT"
    assert t.txn_date == "2026-05-29"
    assert (t.direction, t.txn_type) == ("debit", "purchase")


def test_axis_subject_fallback():
    t = axis.parse("INR 450 spent on credit card no. XX4321", "<p>see details</p>")
    assert t is not None
    assert t.amount == 450.0
    assert t.last4 == "4321"


def test_axis_purchase_not_flipped_by_footer_credited():
    """A purchase whose footer mentions 'cashback will be credited' must stay a
    debit/purchase — a sign flip here would subtract real spend."""
    html = AXIS_HTML + "<p>Cashback for this spend will be credited within 90 days.</p>"
    t = axis.parse("Transaction alert on Axis Bank Credit Card no. XX4321", html)
    assert t is not None
    assert (t.direction, t.txn_type) == ("debit", "purchase")


def test_axis_real_refund_still_detected():
    html = (AXIS_HTML +
            "<p>This is a reversal. The amount has been credited to your card.</p>")
    t = axis.parse("Refund alert on Axis Bank Credit Card no. XX4321", html)
    assert t is not None
    assert (t.direction, t.txn_type) == ("credit", "refund")


# ---------- routing / ignore rules ----------

def test_routing_by_sender():
    assert parsers.get_parser("HDFC Bank InstaAlerts <alerts@hdfcbank.net>") is hdfc
    assert parsers.get_parser("HDFC Bank <alerts@hdfcbank.bank.in>") is hdfc
    assert parsers.get_parser("ICICI Bank <credit_cards@icici.bank.in>") is icici
    assert parsers.get_parser("Axis Bank <alerts@axis.bank.in>") is axis
    assert parsers.get_parser("HSBC <hsbc@mail.hsbc.co.in>") is hsbc
    assert parsers.get_parser("Random <someone@example.com>") is None


def test_routing_not_fooled_by_display_name_spoof():
    """The display name is attacker-controlled; only the addr-spec may route."""
    assert parsers.get_parser('"alerts@hdfcbank.net" <evil@attacker.com>') is None


def test_ignored_senders_and_subjects():
    assert parsers.is_ignored("HDFC <information@hdfcbank.net>", "anything")
    assert parsers.is_ignored("CRED <updates@cred.club>", "bill paid")
    assert parsers.is_ignored("HSBC <hsbc@mail.hsbc.co.in>", "Successful log on to internet banking")
    assert parsers.is_ignored("HSBC <hsbc@mail.hsbc.co.in>", "Your card statement is ready")
    assert not parsers.is_ignored("HSBC <hsbc@mail.hsbc.co.in>", "transaction confirmation")


def test_ignore_subject_otp_needs_word_boundary():
    """'otp' inside a word (hotpot) must NOT cause a real alert to be dropped."""
    assert parsers.is_ignored("HSBC <hsbc@mail.hsbc.co.in>", "Your OTP for the transaction")
    assert not parsers.is_ignored("HSBC <hsbc@mail.hsbc.co.in>",
                                  "Hotpot dinner spend confirmation INR 4,500")


# ---------- base helpers ----------

def test_html_to_text_strips_tags_and_entities():
    assert html_to_text("<p>Rs.100 <b>debited</b>&nbsp;today</p>") == "Rs.100 debited today"


def test_html_to_text_entity_encoded_markup_becomes_live_text():
    """Documents WHY the frontend must escape: entity-encoded markup in an email
    survives tag-stripping and comes out as active HTML characters."""
    out = html_to_text("Paid to &lt;img src=x onerror=alert(1)&gt; store")
    assert "<img src=x onerror=alert(1)>" in out


def test_parse_amount_variants():
    assert parse_amount("Rs.50.00") == 50.0
    assert parse_amount("Rs. 3250.00") == 3250.0
    assert parse_amount("INR 730") == 730.0
    assert parse_amount("₹62,609.02") == 62609.02
    assert parse_amount("INR 2,58,712.00") == 258712.0
    assert parse_amount("no money here") is None


def test_parse_date_variants():
    assert parse_date("on 08 Apr, 2026 at 20:10") == "2026-04-08"
    assert parse_date("on 07 Jun 2026 at 23:21") == "2026-06-07"
    assert parse_date("on 06-04-26") == "2026-04-06"
    assert parse_date("29-05-2026, 14:15:53 IST") == "2026-05-29"
    assert parse_date("May 15, 2026 at 10:06") == "2026-05-15"
    assert parse_date("on 15-JUL-26 through") == "2026-07-15"
    assert parse_date("today sometime") is None


def test_clean_merchant():
    assert clean_merchant("PYU*BIGTREE") == "BIGTREE"
    assert clean_merchant("RAZ*Swiggy") == "Swiggy"
    assert clean_merchant("POS SOME SHOP") == "SOME SHOP"
    assert clean_merchant("  spaced   name  ") == "spaced name"
    assert clean_merchant(None) is None
