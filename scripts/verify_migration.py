"""
scripts/verify_migration.py — Read-only migration pre-flight & verification tool.

Computes row counts, per-user paise totals, unparseable dates, case collisions,
planned constraint violation counts, and orphan foreign keys without printing row data.

Usage:
    python scripts/verify_migration.py <database_uri> [--strict] [--check-not-null]
"""
from __future__ import annotations

import argparse
from datetime import date, datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import hashlib
import json
import math
from pathlib import Path
import re
import sys

import sqlalchemy as sa

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from app import normalize_database_uri  # noqa: E402

_ASCII_DATE_RE = re.compile(r'[0-9]{4}-[0-9]{2}-[0-9]{2}')
_ASCII_YM_RE = re.compile(r'[0-9]{4}-(0[1-9]|1[0-2])')


def _to_paise_and_precision_flag(raw_value, is_paise_column: bool) -> tuple[int | None, bool, bool]:
    """
    Convert a monetary DB value to integer paise.
    Returns (paise_or_None, is_positive_valid, has_gt_2_decimals).
    """
    if raw_value is None:
        return None, False, False

    if is_paise_column:
        if isinstance(raw_value, bool) or not isinstance(raw_value, int):
            return None, False, False
        return (raw_value if raw_value > 0 else None), raw_value > 0, False

    try:
        num = float(raw_value)
    except (TypeError, ValueError):
        return None, False, False

    if not math.isfinite(num) or num <= 0:
        return None, False, False

    try:
        dec = Decimal(str(raw_value))
    except InvalidOperation:
        return None, False, False

    quantized_rupees = dec.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
    has_gt_2_decimals = abs(dec - quantized_rupees) > Decimal('1e-6')
    paise = int((dec * 100).quantize(Decimal('1'), rounding=ROUND_HALF_UP))
    if paise <= 0:
        return None, False, has_gt_2_decimals
    return paise, True, has_gt_2_decimals


def _is_valid_date_value(raw_value) -> bool:
    if isinstance(raw_value, datetime):
        return True
    if isinstance(raw_value, date):
        return True
    if not isinstance(raw_value, str) or not _ASCII_DATE_RE.fullmatch(raw_value):
        return False
    try:
        datetime.strptime(raw_value, '%Y-%m-%d')
        return True
    except ValueError:
        return False


def _is_valid_year_month(raw_value) -> bool:
    if not isinstance(raw_value, str) or not _ASCII_YM_RE.fullmatch(raw_value):
        return False
    year = int(raw_value[:4])
    return 2000 <= year <= 2100


def _digest_user_totals(totals: dict[int, int]) -> str:
    payload = json.dumps(sorted(totals.items()), separators=(',', ':')).encode('utf-8')
    return hashlib.sha256(payload).hexdigest()


def audit_database(database_uri: str) -> dict:
    """Inspect a database and return aggregate counts and conservation checksums only."""
    uri = normalize_database_uri(database_uri)
    engine = sa.create_engine(uri)
    try:
        with engine.connect() as conn:
            insp = sa.inspect(conn)
            user_cols = {c['name'] for c in insp.get_columns('user')}
            expense_cols = {c['name'] for c in insp.get_columns('expense')}
            budget_cols = {c['name'] for c in insp.get_columns('budget')}

            expense_amt_col = 'amount_paise' if 'amount_paise' in expense_cols else 'amount'
            budget_amt_col = 'amount_paise' if 'amount_paise' in budget_cols else 'amount'
            recurring_col = (
                'recurring_budget_paise'
                if 'recurring_budget_paise' in user_cols
                else 'recurring_budget'
            )

            user_count = int(conn.execute(sa.text('SELECT COUNT(*) FROM "user"')).scalar_one())
            expense_count = int(conn.execute(sa.text('SELECT COUNT(*) FROM expense')).scalar_one())
            budget_count = int(conn.execute(sa.text('SELECT COUNT(*) FROM budget')).scalar_one())

            username_collisions = int(
                conn.execute(
                    sa.text(
                        'SELECT COUNT(*) FROM ('
                        'SELECT lower(username) FROM "user" GROUP BY lower(username) HAVING COUNT(*) > 1'
                        ') AS t'
                    )
                ).scalar_one()
            )
            email_collisions = int(
                conn.execute(
                    sa.text(
                        'SELECT COUNT(*) FROM ('
                        'SELECT lower(email) FROM "user" GROUP BY lower(email) HAVING COUNT(*) > 1'
                        ') AS t'
                    )
                ).scalar_one()
            )

            recurring_le_zero = 0
            user_reg_nulls = 0
            user_avatar_nulls = 0
            for rec_val, reg_val, avatar_val in conn.execute(
                sa.text(f'SELECT {recurring_col}, registration_date, avatar FROM "user"')
            ):
                if rec_val is not None:
                    _, valid_rec, _ = _to_paise_and_precision_flag(
                        rec_val, recurring_col == 'recurring_budget_paise'
                    )
                    if not valid_rec:
                        recurring_le_zero += 1
                if reg_val is None:
                    user_reg_nulls += 1
                if avatar_val is None:
                    user_avatar_nulls += 1

            unparseable_dates = 0
            expense_le_zero = 0
            float_gt_2_decimals = 0
            expense_pm_nulls = 0
            expense_created_nulls = 0
            per_user_expense_paise: dict[int, int] = {}
            total_expense_paise = 0

            for uid, dt_val, amt_val, pm_val, created_val in conn.execute(
                sa.text(
                    f'SELECT user_id, date, {expense_amt_col}, payment_method, created_at FROM expense'
                )
            ):
                if not _is_valid_date_value(dt_val):
                    unparseable_dates += 1
                paise, valid_amt, gt2 = _to_paise_and_precision_flag(
                    amt_val, expense_amt_col == 'amount_paise'
                )
                if not valid_amt:
                    expense_le_zero += 1
                elif paise is not None:
                    per_user_expense_paise[uid] = per_user_expense_paise.get(uid, 0) + paise
                    total_expense_paise += paise
                if gt2:
                    float_gt_2_decimals += 1
                if pm_val is None:
                    expense_pm_nulls += 1
                if created_val is None:
                    expense_created_nulls += 1

            malformed_year_month = 0
            budget_le_zero = 0
            per_user_budget_paise: dict[int, int] = {}
            total_budget_paise = 0

            for uid, ym_val, b_amt in conn.execute(
                sa.text(f'SELECT user_id, year_month, {budget_amt_col} FROM budget')
            ):
                if not _is_valid_year_month(ym_val):
                    malformed_year_month += 1
                paise, valid_amt, gt2 = _to_paise_and_precision_flag(
                    b_amt, budget_amt_col == 'amount_paise'
                )
                if not valid_amt:
                    budget_le_zero += 1
                elif paise is not None:
                    per_user_budget_paise[uid] = per_user_budget_paise.get(uid, 0) + paise
                    total_budget_paise += paise
                if gt2:
                    float_gt_2_decimals += 1

            orphan_expense = int(
                conn.execute(
                    sa.text(
                        'SELECT COUNT(*) FROM expense e '
                        'WHERE NOT EXISTS (SELECT 1 FROM "user" u WHERE u.id = e.user_id)'
                    )
                ).scalar_one()
            )
            orphan_budget = int(
                conn.execute(
                    sa.text(
                        'SELECT COUNT(*) FROM budget b '
                        'WHERE NOT EXISTS (SELECT 1 FROM "user" u WHERE u.id = b.user_id)'
                    )
                ).scalar_one()
            )
            sqlite_fk_check = 0
            if conn.dialect.name == 'sqlite':
                sqlite_fk_check = sum(1 for _ in conn.execute(sa.text('PRAGMA foreign_key_check')))

        return {
            'row_counts': {
                'user': user_count,
                'expense': expense_count,
                'budget': budget_count,
            },
            'total_expense_paise': total_expense_paise,
            'total_budget_paise': total_budget_paise,
            'per_user_expense_paise': per_user_expense_paise,
            'per_user_budget_paise': per_user_budget_paise,
            'per_user_expense_digest': _digest_user_totals(per_user_expense_paise),
            'per_user_budget_digest': _digest_user_totals(per_user_budget_paise),
            'unparseable_dates': unparseable_dates,
            'case_collisions': {
                'username': username_collisions,
                'email': email_collisions,
            },
            'non_positive_amounts': {
                'expense': expense_le_zero,
                'budget': budget_le_zero,
                'recurring_budget': recurring_le_zero,
            },
            'float_gt_2_decimals': float_gt_2_decimals,
            'malformed_year_month': malformed_year_month,
            'planned_not_null_nulls': {
                'user.registration_date': user_reg_nulls,
                'user.avatar': user_avatar_nulls,
                'expense.payment_method': expense_pm_nulls,
                'expense.created_at': expense_created_nulls,
            },
            'orphan_foreign_keys': {
                'expense': orphan_expense,
                'budget': orphan_budget,
                'sqlite_fk_check': sqlite_fk_check,
            },
        }
    finally:
        engine.dispose()


def compare_reports(before: dict, after: dict) -> list[str]:
    """Verify row counts and per-user paise totals match identically between snapshots."""
    errors: list[str] = []
    if before['row_counts'] != after['row_counts']:
        errors.append('row_counts_mismatch')
    if before['total_expense_paise'] != after['total_expense_paise']:
        errors.append('total_expense_paise_mismatch')
    if before['total_budget_paise'] != after['total_budget_paise']:
        errors.append('total_budget_paise_mismatch')
    if before['per_user_expense_paise'] != after['per_user_expense_paise']:
        errors.append('per_user_expense_paise_mismatch')
    if before['per_user_budget_paise'] != after['per_user_budget_paise']:
        errors.append('per_user_budget_paise_mismatch')
    return errors


def _summary_for_stdout(report: dict) -> dict:
    """Strip per-user dicts when printing so stdout emits aggregate counts & checksums only."""
    return {
        'row_counts': report['row_counts'],
        'total_expense_paise': report['total_expense_paise'],
        'total_budget_paise': report['total_budget_paise'],
        'users_with_expenses_count': len(report['per_user_expense_paise']),
        'users_with_budgets_count': len(report['per_user_budget_paise']),
        'per_user_expense_digest': report['per_user_expense_digest'],
        'per_user_budget_digest': report['per_user_budget_digest'],
        'unparseable_dates': report['unparseable_dates'],
        'case_collisions': report['case_collisions'],
        'non_positive_amounts': report['non_positive_amounts'],
        'float_gt_2_decimals': report['float_gt_2_decimals'],
        'malformed_year_month': report['malformed_year_month'],
        'planned_not_null_nulls': report['planned_not_null_nulls'],
        'orphan_foreign_keys': report['orphan_foreign_keys'],
    }


def _has_blocking_violations(report: dict, check_not_null: bool) -> bool:
    if (
        report['unparseable_dates'] > 0
        or any(v > 0 for v in report['case_collisions'].values())
        or any(v > 0 for v in report['non_positive_amounts'].values())
        or report['float_gt_2_decimals'] > 0
        or report['malformed_year_month'] > 0
        or any(v > 0 for v in report['orphan_foreign_keys'].values())
    ):
        return True
    if check_not_null and any(v > 0 for v in report['planned_not_null_nulls'].values()):
        return True
    return False


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description='Audit ExpenseFlow DB counts and migration invariants (prints counts only).'
    )
    parser.add_argument('database_uri', help='SQLAlchemy database URI to inspect')
    parser.add_argument('--strict', action='store_true', help='Exit non-zero if any violation count > 0')
    parser.add_argument(
        '--check-not-null',
        action='store_true',
        help='Treat NULLs in planned NOT NULL columns as strict failures',
    )
    args = parser.parse_args(argv)

    report = audit_database(args.database_uri)
    print(json.dumps(_summary_for_stdout(report), indent=2, sort_keys=True))

    if args.strict and _has_blocking_violations(report, args.check_not_null):
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
