"""
blueprints/budget.py — budget management routes.

Routes: /set_budget
Blueprint name: 'budget'
Endpoint prefix: budget.<function_name>
"""
import logging
from datetime import datetime
from flask import (
    Blueprint, render_template, request, redirect,
    session, flash, url_for,
)
from models import db, User, Budget
from utils import is_logged_in, get_current_year_month, get_user_budget

logger = logging.getLogger('expenseflow')

budget_bp = Blueprint('budget', __name__)


# ── Set budget ────────────────────────────────────────────────────────────────

@budget_bp.route('/set_budget', methods=['GET', 'POST'])
def set_budget():
    """
    GET:  Show the set-budget form with the current month's budget pre-filled.
    POST: Validate the submitted budget amount, upsert the Budget record.
          If 'recurring' checkbox is checked, also update user.recurring_budget.
    """
    if not is_logged_in():
        flash('Please log in to set your budget.', 'error')
        return redirect(url_for('auth.login'))

    user_id = session['user_id']
    current_year_month = get_current_year_month()
    current_budget = get_user_budget(user_id, current_year_month)

    if request.method == 'POST':
        try:
            month = request.form['month']
            year = request.form['year']
            year_month = f"{year}-{month}"
            new_budget_amount = float(request.form['budget'])

            if new_budget_amount <= 0:
                flash('Budget must be a positive number.', 'error')
                return redirect(url_for('budget.set_budget'))

            budget = Budget.query.filter_by(user_id=user_id, year_month=year_month).first()
            if budget:
                budget.amount = new_budget_amount
            else:
                budget = Budget(user_id=user_id, year_month=year_month, amount=new_budget_amount)
                db.session.add(budget)

            if 'recurring' in request.form:
                user = db.session.get(User, user_id)
                if user:
                    user.recurring_budget = new_budget_amount
                flash(f'Recurring budget set to ₹{new_budget_amount:.2f}! This will be applied to future months.', 'success')
            else:
                flash(f'Budget for {month}/{year} updated successfully!', 'success')

            db.session.commit()
            return redirect(url_for('auth.dashboard'))

        except ValueError:
            flash('Please enter a valid budget amount.', 'error')
            return redirect(url_for('budget.set_budget'))

    return render_template('set_budget.html',
                         current_budget=current_budget,
                         current_month=datetime.now().strftime("%m"),
                         current_year=datetime.now().year)
