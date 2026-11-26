"""
blueprints/expenses.py — expense CRUD routes.

Routes: /add, /view, /edit/<id>, /delete/<id>
Blueprint name: 'expenses'
Endpoint prefix: expenses.<function_name>
"""
import re as _re
import logging
import random
from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP
from flask import (
    Blueprint, render_template, request, redirect,
    session, flash, url_for,
)
from models import db, Expense
from utils import (
    is_logged_in, get_current_year_month, get_user_budget,
    get_monthly_spending, get_user_expenses_sorted, TIPS,
    MAX_EXPENSE_RUPEES, parse_rupees_to_paise, paise_to_rupees_input,
)

logger = logging.getLogger('expenseflow')

expenses_bp = Blueprint('expenses', __name__)


def _parse_expense_date(raw_date):
    """Parse and validate an ASCII YYYY-MM-DD non-future expense date."""
    if not raw_date or not _re.fullmatch(r'[0-9]{4}-[0-9]{2}-[0-9]{2}', raw_date):
        return None, 'Please select a valid date.'
    try:
        parsed = datetime.strptime(raw_date, '%Y-%m-%d').date()
    except ValueError:
        return None, 'Please select a valid date.'
    if parsed > datetime.now().date():
        return None, 'Expense date cannot be in the future.'
    return parsed, None

def _parse_expense_amount(amount_raw, *, require_non_empty=False):
    """Parse decimal rupee input into paise and return (paise, flash_msg, flash_cat)."""
    amount_paise, err = parse_rupees_to_paise(amount_raw, max_rupees=MAX_EXPENSE_RUPEES)
    if err is None:
        return amount_paise, None, None
    if err == 'empty' and require_non_empty:
        return None, 'Amount is required.', 'error'
    if err == 'too_large':
        return None, 'Amount seems too large. Please check your entry.', 'warning'
    return None, 'Please enter a valid positive amount.', 'error'


# ── Shared validation sets ────────────────────────────────────────────────────
VALID_CATEGORIES = {
    'Food', 'Transport', 'Shopping', 'Entertainment', 'Health',
    'Utilities', 'Education', 'Travel', 'Personal Care', 'Home', 'Insurance', 'Other'
}
VALID_METHODS = {
    'Cash', 'UPI', 'Credit Card', 'Debit Card', 'Net Banking', 'Digital Wallet', 'Other'
}


# ── Add expense ───────────────────────────────────────────────────────────────

@expenses_bp.route('/add', methods=['GET', 'POST'])
def add_expense():
    """
    GET:  Show the add-expense form with current budget context.
    POST: Validate all fields server-side, persist a new Expense record.
    Validation: date format, category whitelist, positive amount cap,
    payment method whitelist, description length.
    """
    if not is_logged_in():
        flash('Please log in to add expenses.', 'error')
        return redirect(url_for('auth.login'))

    if request.method == 'POST':
        date            = request.form.get('date', '').strip()
        category        = request.form.get('category', '').strip()
        amount_raw      = request.form.get('amount', '').strip()
        description     = request.form.get('description', '').strip()
        payment_method  = request.form.get('payment_method', '').strip()

        # ── Server-side validation ────────────────────────────────────────
        expense_date, date_err = _parse_expense_date(date)
        if date_err:
            flash(date_err, 'error')
            return redirect(url_for('expenses.add_expense'))
        if category not in VALID_CATEGORIES:
            flash('Please select a valid category.', 'error')
            return redirect(url_for('expenses.add_expense'))
        amount_paise, amt_msg, amt_cat = _parse_expense_amount(amount_raw, require_non_empty=True)
        if amt_msg:
            flash(amt_msg, amt_cat)
            return redirect(url_for('expenses.add_expense'))
        if payment_method not in VALID_METHODS:
            flash('Please select a valid payment method.', 'error')
            return redirect(url_for('expenses.add_expense'))
        # Sanitise description
        description = description[:255]

        # ── Persist to database ───────────────────────────────────────────
        user_id = session['user_id']
        new_expense = Expense(
            user_id=user_id,
            date=expense_date,
            category=category,
            amount_paise=amount_paise,
            description=description,
            payment_method=payment_method
        )
        db.session.add(new_expense)
        db.session.commit()
        logger.info('Expense added: user=%s amount_paise=%d category=%s', user_id, amount_paise, category)

        flash('Expense added successfully!', 'success')
        return redirect(url_for('auth.dashboard'))

    # GET — compute budget context for the info strip
    user_id = session['user_id']

    current_ym = get_current_year_month()
    month_spent = get_monthly_spending(user_id, current_ym)
    add_budget = get_user_budget(user_id, current_ym)
    add_budget_remaining = add_budget - month_spent
    add_budget_pct = (month_spent / add_budget * 100) if add_budget > 0 else 0

    return render_template('add_expense.html',
        current_date=datetime.now(),
        budget=add_budget,
        budget_spent=month_spent,
        budget_remaining=add_budget_remaining,
        budget_pct=add_budget_pct,
    )


# ── View / list expenses ──────────────────────────────────────────────────────

@expenses_bp.route('/view')
def view_expense():
    """
    Transactions list page.
    Aggregates all user expenses into category, monthly and payment-method
    totals for the filter UI; passes the raw expense list for JS filtering.
    """
    if not is_logged_in():
        flash('Please log in to view expenses.', 'error')
        return redirect(url_for('auth.login'))

    user_id = session['user_id']
    expenses_obj = get_user_expenses_sorted(user_id)

    expenses = []
    total_spent = 0
    category_totals = {}
    monthly_totals = {}
    payment_method_totals = {}

    for e in expenses_obj:
        expenses.append([
            e.date.isoformat(),
            e.category,
            paise_to_rupees_input(e.amount_paise),
            e.description or '',
            e.payment_method,
            e.id,
            e.amount_paise,
        ])

        total_spent += e.amount_paise
        category_totals[e.category] = category_totals.get(e.category, 0) + e.amount_paise
        payment_method_totals[e.payment_method] = payment_method_totals.get(e.payment_method, 0) + e.amount_paise

        month_key = e.date.strftime('%Y-%m')
        monthly_totals[month_key] = monthly_totals.get(month_key, 0) + e.amount_paise

    if expenses_obj:
        first_date = min(e.date for e in expenses_obj)
        days_active = max(1, (datetime.now().date() - first_date).days + 1)
        avg_daily_spend = int((Decimal(total_spent) / Decimal(days_active)).quantize(Decimal('1'), rounding=ROUND_HALF_UP))
    else:
        avg_daily_spend = 0
    top_category = max(category_totals, key=category_totals.get) if category_totals else 'None'
    top_payment_method = max(payment_method_totals, key=payment_method_totals.get) if payment_method_totals else 'None'

    sorted_category_totals = sorted(category_totals.items(), key=lambda x: x[1], reverse=True)
    sorted_monthly_totals = sorted(monthly_totals.items(), key=lambda x: x[0], reverse=True)
    sorted_payment_method_totals = sorted(payment_method_totals.items(), key=lambda x: x[1], reverse=True)

    savings_tip = random.choice(TIPS) if TIPS else "Track your expenses regularly!"

    return render_template('view_expense.html',
                         expenses=expenses,
                         total_spent=total_spent,
                         category_totals=sorted_category_totals,
                         monthly_totals=sorted_monthly_totals,
                         payment_method_totals=sorted_payment_method_totals,
                         avg_daily_spend=avg_daily_spend,
                         top_category=top_category,
                         top_payment_method=top_payment_method,
                         savings_tip=savings_tip,
                         current_date=datetime.now())


# ── Edit expense ──────────────────────────────────────────────────────────────

@expenses_bp.route('/edit/<int:expense_id>', methods=['GET', 'POST'])
def edit_expense(expense_id):
    """
    GET:  Load an existing expense into the edit form.
    POST: Validate and persist the updated fields.
    expense_id is the database primary key (Expense.id), scoped to the
    logged-in user so one user cannot edit another user's expense.
    """
    if not is_logged_in():
        flash('Please log in to edit expenses.', 'error')
        return redirect(url_for('auth.login'))

    user_id = session['user_id']
    expense_to_edit = Expense.query.filter_by(id=expense_id, user_id=user_id).first_or_404()

    if request.method == 'POST':
        date           = request.form.get('date', '').strip()
        category       = request.form.get('category', '').strip()
        amount_raw     = request.form.get('amount', '').strip()
        description    = request.form.get('description', '').strip()
        payment_method = request.form.get('payment_method', '').strip()

        expense_date, date_err = _parse_expense_date(date)
        if date_err:
            flash(date_err, 'error')
            return redirect(url_for('expenses.edit_expense', expense_id=expense_id))
        if category not in VALID_CATEGORIES:
            flash('Please select a valid category.', 'error')
            return redirect(url_for('expenses.edit_expense', expense_id=expense_id))
        amount_paise, amt_msg, amt_cat = _parse_expense_amount(amount_raw)
        if amt_msg:
            flash(amt_msg, amt_cat)
            return redirect(url_for('expenses.edit_expense', expense_id=expense_id))
        if payment_method not in VALID_METHODS:
            flash('Please select a valid payment method.', 'error')
            return redirect(url_for('expenses.edit_expense', expense_id=expense_id))
        description = description[:255]

        expense_to_edit.date = expense_date
        expense_to_edit.category = category
        expense_to_edit.amount_paise = amount_paise
        expense_to_edit.description = description
        expense_to_edit.payment_method = payment_method

        db.session.commit()
        logger.info('Expense edited: user=%s id=%s', user_id, expense_to_edit.id)

        flash('Expense updated successfully!', 'success')
        return redirect(url_for('expenses.view_expense'))

    expense_list = [
        expense_to_edit.date.isoformat(),
        expense_to_edit.category,
        paise_to_rupees_input(expense_to_edit.amount_paise),
        expense_to_edit.description,
        expense_to_edit.payment_method,
    ]
    return render_template('edit_expense.html', expense=expense_list, expense_id=expense_id)


# ── Delete expense ────────────────────────────────────────────────────────────

@expenses_bp.route('/delete/<int:expense_id>', methods=['POST'])
def delete_expense(expense_id):
    """
    Delete the expense identified by its database primary key (Expense.id).
    Accepts POST only — GET requests are rejected with 405 Method Not Allowed.
    The query is scoped to the logged-in user so one user cannot delete
    another user's expense.
    Redirects to the transactions page with a success or error flash.
    """
    if not is_logged_in():
        flash('Please log in to delete expenses.', 'error')
        return redirect(url_for('auth.login'))

    user_id = session['user_id']
    expense_to_delete = Expense.query.filter_by(id=expense_id, user_id=user_id).first_or_404()

    db.session.delete(expense_to_delete)
    db.session.commit()
    logger.info('Expense deleted: user=%s id=%s', user_id, expense_to_delete.id)
    flash('Expense deleted successfully!', 'success')

    return redirect(url_for('expenses.view_expense'))
