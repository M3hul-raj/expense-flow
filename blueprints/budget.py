"""
blueprints/budget.py — budget management routes.

Routes: /set_budget
Blueprint name: 'budget'
Endpoint prefix: budget.<function_name>
"""
import logging
import re
from datetime import datetime
from flask import (
    Blueprint, render_template, request, redirect,
    session, flash, url_for,
)
from models import db, User, Budget
from utils import (
    is_logged_in, get_current_year_month, get_user_budget,
    MAX_BUDGET_RUPEES, parse_rupees_to_paise, paise_to_rupees_input,
    format_paise_as_rupees, commit_session,
)

logger = logging.getLogger('expenseflow')

budget_bp = Blueprint('budget', __name__)

MAX_BUDGET_AMOUNT = MAX_BUDGET_RUPEES


# ── Set budget ────────────────────────────────────────────────────────────────

@budget_bp.route('/set_budget', methods=['GET', 'POST'])
def set_budget():
    """
    GET:  Show the set-budget form with the current month's budget pre-filled.
    POST: Validate the submitted month/year/budget, upsert the Budget record.
          If 'recurring' checkbox is checked, also update user.recurring_budget_paise.
    """
    if not is_logged_in():
        flash('Please log in to set your budget.', 'error')
        return redirect(url_for('auth.login'))

    user_id = session['user_id']
    current_year_month = get_current_year_month()
    current_budget_paise = get_user_budget(user_id, current_year_month)

    if request.method == 'POST':
        month_raw = request.form.get('month', '').strip()
        year_raw = request.form.get('year', '').strip()
        budget_raw = request.form.get('budget', '').strip()

        if not re.fullmatch(r'[0-9]{1,2}', month_raw) or not (1 <= int(month_raw) <= 12):
            flash('Please select a valid month (01–12).', 'error')
            return redirect(url_for('budget.set_budget'))
        month = f"{int(month_raw):02d}"

        if not re.fullmatch(r'[0-9]{4}', year_raw) or not (2000 <= int(year_raw) <= 2100):
            flash('Please select a valid year (2000–2100).', 'error')
            return redirect(url_for('budget.set_budget'))
        year = year_raw
        year_month = f"{year}-{month}"

        new_budget_paise, err = parse_rupees_to_paise(budget_raw, max_rupees=MAX_BUDGET_AMOUNT)
        if err in {'empty', 'invalid'}:
            flash('Please enter a valid budget amount.', 'error')
            return redirect(url_for('budget.set_budget'))
        if err == 'non_positive':
            flash('Budget must be a positive number.', 'error')
            return redirect(url_for('budget.set_budget'))
        if err == 'too_large':
            flash('Budget amount is too large.', 'error')
            return redirect(url_for('budget.set_budget'))

        budget = Budget.query.filter_by(user_id=user_id, year_month=year_month).first()
        if budget:
            budget.amount_paise = new_budget_paise
        else:
            budget = Budget(user_id=user_id, year_month=year_month, amount_paise=new_budget_paise)
            db.session.add(budget)

        if 'recurring' in request.form:
            user = db.session.get(User, user_id)
            if user:
                user.recurring_budget_paise = new_budget_paise
            flash(
                f'Recurring budget set to {format_paise_as_rupees(new_budget_paise)}! '
                'This will be applied to future months.',
                'success',
            )
        else:
            flash(f'Budget for {month}/{year} updated successfully!', 'success')

        if commit_session() is not None:
            logger.exception('Database error saving budget: user=%s', user_id)
            flash('Unable to save budget right now. Please try again.', 'error')
            return redirect(url_for('budget.set_budget'))
        return redirect(url_for('auth.dashboard'))

    return render_template('set_budget.html',
                         current_budget=current_budget_paise,
                         current_budget_input=paise_to_rupees_input(current_budget_paise),
                         current_month=datetime.now().strftime("%m"),
                         current_year=datetime.now().year)
