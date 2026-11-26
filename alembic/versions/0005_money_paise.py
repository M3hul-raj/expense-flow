"""convert money columns from Float to BigInteger paise

Revision ID: 0005_money_paise
Revises: 0004_expense_ordering_index
Create Date: 2026-11-26 23:55:00.000000

"""
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import math
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0005_money_paise'
down_revision: Union[str, Sequence[str], None] = '0004_expense_ordering_index'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_MAX_BIGINT = (1 << 63) - 1


def _float_to_paise(raw_value) -> int | None:
    """Convert a positive finite float/numeric rupee value to integer paise (ROUND_HALF_UP)."""
    if raw_value is None or isinstance(raw_value, bool):
        return None
    try:
        num = float(raw_value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(num) or num <= 0:
        return None
    try:
        dec = Decimal(str(raw_value))
    except InvalidOperation:
        return None
    paise = int((dec * Decimal('100')).quantize(Decimal('1'), rounding=ROUND_HALF_UP))
    if paise <= 0 or paise > _MAX_BIGINT:
        return None
    return paise


def _collect_paise_updates(bind):
    """Validate and pre-compute paise conversions before any DDL runs."""
    bad_count = 0
    user_updates = []
    expense_updates = []
    budget_updates = []

    for row_id, raw_val in bind.execute(
        sa.text('SELECT id, recurring_budget FROM "user" WHERE recurring_budget IS NOT NULL')
    ):
        paise = _float_to_paise(raw_val)
        if paise is None:
            bad_count += 1
        else:
            user_updates.append({'row_id': row_id, 'paise': paise})

    for row_id, raw_val in bind.execute(sa.text('SELECT id, amount FROM expense')):
        paise = _float_to_paise(raw_val)
        if paise is None:
            bad_count += 1
        else:
            expense_updates.append({'row_id': row_id, 'paise': paise})

    for row_id, raw_val in bind.execute(sa.text('SELECT id, amount FROM budget')):
        paise = _float_to_paise(raw_val)
        if paise is None:
            bad_count += 1
        else:
            budget_updates.append({'row_id': row_id, 'paise': paise})

    if bad_count:
        raise ValueError(
            f'Cannot migrate monetary columns to paise: found {bad_count} invalid amount rows'
        )
    return user_updates, expense_updates, budget_updates


def upgrade() -> None:
    """Convert User.recurring_budget, Expense.amount, and Budget.amount to BigInteger paise."""
    bind = op.get_bind()
    user_updates, expense_updates, budget_updates = _collect_paise_updates(bind)

    op.add_column('user', sa.Column('recurring_budget_paise', sa.BigInteger(), nullable=True))
    op.add_column('expense', sa.Column('amount_paise', sa.BigInteger(), nullable=True))
    op.add_column('budget', sa.Column('amount_paise', sa.BigInteger(), nullable=True))

    if user_updates:
        bind.execute(
            sa.text('UPDATE "user" SET recurring_budget_paise = :paise WHERE id = :row_id'),
            user_updates,
        )
    if expense_updates:
        bind.execute(
            sa.text('UPDATE expense SET amount_paise = :paise WHERE id = :row_id'),
            expense_updates,
        )
    if budget_updates:
        bind.execute(
            sa.text('UPDATE budget SET amount_paise = :paise WHERE id = :row_id'),
            budget_updates,
        )

    # Rebuild/alter tables cleanly while preserving legacy sqlite_autoindex_* constraints on user
    with op.batch_alter_table('user', schema=None) as batch_op:
        batch_op.drop_column('recurring_budget')

    with op.batch_alter_table('expense', schema=None) as batch_op:
        batch_op.alter_column('amount_paise', existing_type=sa.BigInteger(), nullable=False)
        batch_op.drop_column('amount')

    with op.batch_alter_table('budget', schema=None) as batch_op:
        batch_op.alter_column('amount_paise', existing_type=sa.BigInteger(), nullable=False)
        batch_op.drop_column('amount')


def downgrade() -> None:
    """Revert paise BigInteger columns back to Float rupee columns."""
    bind = op.get_bind()

    user_updates = [
        {'row_id': row_id, 'amt': float(Decimal(paise) / Decimal('100'))}
        for row_id, paise in bind.execute(
            sa.text('SELECT id, recurring_budget_paise FROM "user" WHERE recurring_budget_paise IS NOT NULL')
        )
    ]
    expense_updates = [
        {'row_id': row_id, 'amt': float(Decimal(paise) / Decimal('100'))}
        for row_id, paise in bind.execute(sa.text('SELECT id, amount_paise FROM expense'))
    ]
    budget_updates = [
        {'row_id': row_id, 'amt': float(Decimal(paise) / Decimal('100'))}
        for row_id, paise in bind.execute(sa.text('SELECT id, amount_paise FROM budget'))
    ]

    op.add_column('user', sa.Column('recurring_budget', sa.Float(), nullable=True))
    op.add_column('expense', sa.Column('amount', sa.Float(), nullable=True))
    op.add_column('budget', sa.Column('amount', sa.Float(), nullable=True))

    if user_updates:
        bind.execute(
            sa.text('UPDATE "user" SET recurring_budget = :amt WHERE id = :row_id'),
            user_updates,
        )
    if expense_updates:
        bind.execute(
            sa.text('UPDATE expense SET amount = :amt WHERE id = :row_id'),
            expense_updates,
        )
    if budget_updates:
        bind.execute(
            sa.text('UPDATE budget SET amount = :amt WHERE id = :row_id'),
            budget_updates,
        )

    with op.batch_alter_table('user', schema=None) as batch_op:
        batch_op.drop_column('recurring_budget_paise')

    with op.batch_alter_table('expense', schema=None) as batch_op:
        batch_op.alter_column('amount', existing_type=sa.Float(), nullable=False)
        batch_op.drop_column('amount_paise')

    with op.batch_alter_table('budget', schema=None) as batch_op:
        batch_op.alter_column('amount', existing_type=sa.Float(), nullable=False)
        batch_op.drop_column('amount_paise')
