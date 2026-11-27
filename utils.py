"""
utils.py — shared helper functions and constants used across blueprints.

Keeping these here avoids duplicating logic across blueprint files and
prevents circular imports (blueprints import from utils, not from each other).
"""
import calendar as _calendar
from datetime import date, datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import re as _re
from dateutil.relativedelta import relativedelta
from flask import session
from sqlalchemy import func
from sqlalchemy.exc import SQLAlchemyError
from models import db, User, Expense, Budget

# ── Monetary constants & helpers (integer paise internally) ──────────────────
DEFAULT_BUDGET_PAISE = 200_000        # ₹2,000.00
MAX_EXPENSE_RUPEES = 10_000_000       # ₹1,00,00,000.00
MAX_BUDGET_RUPEES = 100_000_000       # ₹10,00,00,000.00

_ASCII_DECIMAL_RE = _re.compile(r'^[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)$')
_PAISE_PER_RUPEE = Decimal('100')
_CSV_FORMULA_PREFIXES = ('=', '+', '-', '@', '\t', '\r')

# ── Shared expense taxonomy whitelists ───────────────────────────────────────
EXPENSE_CATEGORIES = (
    'Food', 'Transport', 'Shopping', 'Entertainment', 'Health',
    'Utilities', 'Education', 'Travel', 'Personal Care', 'Home',
    'Insurance', 'Other', 'Uncategorised',
)
VALID_CATEGORIES = set(EXPENSE_CATEGORIES)

PAYMENT_METHODS = (
    'Cash', 'UPI', 'Credit Card', 'Debit Card', 'Net Banking',
    'Digital Wallet', 'Other',
)
VALID_METHODS = set(PAYMENT_METHODS)

# ── Savings tips ─────────────────────────────────────────────────────────────
TIPS = [
    "Cook at home instead of eating out.",
    "Track your subscriptions.",
    "Plan weekly budgets.",
    "Use public transport to save fuel.",
    "Avoid unnecessary online purchases.",
]


def parse_rupees_to_paise(raw_value, *, max_rupees=None):
    """
    Parse a decimal rupee input string into positive integer paise (ROUND_HALF_UP).

    Returns (paise, None) on success, or (None, error_code) where error_code is one of:
    'empty', 'invalid', 'non_positive', or 'too_large'.
    """
    cleaned = raw_value.strip() if isinstance(raw_value, str) else ''
    if not cleaned:
        return None, 'empty'
    if not _ASCII_DECIMAL_RE.fullmatch(cleaned):
        return None, 'invalid'
    try:
        dec = Decimal(cleaned)
    except InvalidOperation:
        return None, 'invalid'
    if not dec.is_finite():
        return None, 'invalid'
    if dec <= 0:
        return None, 'non_positive'
    if max_rupees is not None and dec > Decimal(max_rupees):
        return None, 'too_large'

    paise = int((dec * _PAISE_PER_RUPEE).quantize(Decimal('1'), rounding=ROUND_HALF_UP))
    if paise <= 0:
        return None, 'non_positive'
    if max_rupees is not None and paise > int(max_rupees) * 100:
        return None, 'too_large'
    return paise, None


def paise_to_decimal(paise: int) -> Decimal:
    """Convert integer paise to a quantized 2-decimal Rupee Decimal."""
    return (Decimal(int(paise)) / _PAISE_PER_RUPEE).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)


def paise_to_rupees_input(paise: int) -> str:
    """Format integer paise as plain 2-decimal rupee text for HTML form inputs and CSV."""
    return f"{paise_to_decimal(paise):.2f}"


def paise_to_chart_float(paise: int) -> float:
    """Convert integer paise to a rounded rupee float strictly for Chart.js serialization."""
    return float(paise_to_decimal(paise))


def format_paise_as_rupees(paise: int, decimals: int = 2) -> str:
    """Format integer paise as an Indian Rupee display string (e.g. ₹1,234.56)."""
    dec = Decimal(int(paise)) / _PAISE_PER_RUPEE
    sign = '-' if dec < 0 else ''
    abs_dec = abs(dec)
    if decimals <= 0:
        quantized = abs_dec.quantize(Decimal('1'), rounding=ROUND_HALF_UP)
        return f"{sign}₹{quantized:,}"
    quantized = abs_dec.quantize(Decimal('1.' + ('0' * decimals)), rounding=ROUND_HALF_UP)
    return f"{sign}₹{quantized:,.{decimals}f}"


def sanitize_csv_cell(value) -> str:
    """
    Neutralize spreadsheet formula injection (CWE-1236) for CSV exports.

    Any string beginning with '=', '+', '-', '@', tab, or carriage return
    (including after leading whitespace) is prefixed with a single quote.
    """
    if value is None:
        return ''
    text = str(value).replace('\x00', '')
    stripped = text.lstrip(' \t\r\n')
    if text.startswith(_CSV_FORMULA_PREFIXES) or (stripped and stripped[0] in _CSV_FORMULA_PREFIXES):
        return "'" + text
    return text


def commit_session():
    """Commit the active DB session, rolling back cleanly on any SQLAlchemyError."""
    try:
        db.session.commit()
        return None
    except SQLAlchemyError as exc:
        db.session.rollback()
        return exc


def is_logged_in():
    """Return True if a user session is currently active."""
    return 'user_id' in session


def get_current_year_month():
    """Return the current month in YYYY-MM format."""
    return datetime.now().strftime("%Y-%m")


def get_user_budget(user_id, year_month=None):
    """
    Return the budget amount in paise for a given user and month.
    Priority: explicit Budget row > recurring_budget_paise on User > default 200000 paise (₹2,000.00).
    """
    if not year_month:
        year_month = get_current_year_month()

    budget = Budget.query.filter_by(user_id=user_id, year_month=year_month).first()
    if budget:
        return int(budget.amount_paise)

    user = db.session.get(User, user_id)
    if user and user.recurring_budget_paise is not None:
        return int(user.recurring_budget_paise)

    return DEFAULT_BUDGET_PAISE


def month_date_range(year_month):
    """Return half-open [start_date, next_month_start_date) bounds for YYYY-MM."""
    year, month = map(int, year_month.split('-'))
    start = date(year, month, 1)
    return start, start + relativedelta(months=1)


def get_monthly_spending(user_id, year_month):
    """Return total amount spent in paise by a user in the given YYYY-MM month."""
    start_date, end_date = month_date_range(year_month)
    total = (
        db.session.query(func.coalesce(func.sum(Expense.amount_paise), 0))
        .filter(
            Expense.user_id == user_id,
            Expense.date >= start_date,
            Expense.date < end_date,
        )
        .scalar()
    )
    return int(total or 0)


def get_budget_history(user_id, months=6):
    """
    Build a budget vs spending summary (values in integer paise) for the last N months.
    Returns a list of dicts with keys: month, year_month, budget, spending,
    status ('On Track' / 'Near Limit' / 'Over Budget'), remaining.
    """
    history = []
    current_date = datetime.now()

    for i in range(months):
        dt = current_date - relativedelta(months=i)
        year_month = dt.strftime("%Y-%m")

        budget_paise = get_user_budget(user_id, year_month)
        spending_paise = get_monthly_spending(user_id, year_month)

        status = "On Track"
        if spending_paise > budget_paise:
            status = "Over Budget"
        elif spending_paise * 10 > budget_paise * 9:
            status = "Near Limit"

        history.append({
            'month': f"{_calendar.month_name[dt.month]} {dt.year}",
            'year_month': year_month,
            'budget': budget_paise,
            'spending': spending_paise,
            'status': status,
            'remaining': budget_paise - spending_paise,
        })

    return history


def get_user_expenses_sorted(user_id):
    """Return all expenses for a user sorted newest-first by date and id."""
    return (
        Expense.query
        .filter_by(user_id=user_id)
        .order_by(Expense.date.desc(), Expense.id.desc())
        .all()
    )
