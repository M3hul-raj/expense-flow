"""convert Expense.date from String(10) to DATE

Revision ID: 0003_expense_date_type
Revises: 0002_reconcile_prod_schema
Create Date: 2026-11-26 23:08:00.000000

"""
from datetime import date as date_type, datetime
import re
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0003_expense_date_type'
down_revision: Union[str, Sequence[str], None] = '0002_reconcile_prod_schema'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_ASCII_DATE_RE = re.compile(r'[0-9]{4}-[0-9]{2}-[0-9]{2}')


def _validate_existing_expense_dates(bind) -> None:
    bad_count = 0
    for (raw_val,) in bind.execute(sa.text('SELECT date FROM expense')):
        if isinstance(raw_val, date_type):
            continue
        if not isinstance(raw_val, str) or not _ASCII_DATE_RE.fullmatch(raw_val):
            bad_count += 1
            continue
        try:
            datetime.strptime(raw_val, '%Y-%m-%d')
        except ValueError:
            bad_count += 1
    if bad_count:
        raise ValueError(
            f'Cannot migrate Expense.date to DATE: found {bad_count} unparseable date rows'
        )


def _sqlite_rebuild_expense_with_date_type(target_type) -> None:
    """
    Rebuild SQLite `expense` without `CAST(date AS DATE)`.

    SQLite gives `DATE` NUMERIC affinity, so Alembic's default `CAST(date AS DATE)`
    truncates `'YYYY-MM-DD'` strings to the leading year integer (e.g. `2024`).
    Copying through a reflected `copy_from` table preserves the exact column order,
    foreign keys, indexes, and `'YYYY-MM-DD'` TEXT values.
    """
    bind = op.get_bind()
    reflected = sa.Table('expense', sa.MetaData(), autoload_with=bind)
    reflected.c.date.type = target_type
    with op.batch_alter_table('expense', copy_from=reflected, recreate='always'):
        pass


def upgrade() -> None:
    """Validate all stored YYYY-MM-DD strings and convert Expense.date to DATE."""
    bind = op.get_bind()
    _validate_existing_expense_dates(bind)

    if bind.dialect.name == 'sqlite':
        _sqlite_rebuild_expense_with_date_type(sa.Date())
        return

    with op.batch_alter_table('expense', schema=None) as batch_op:
        batch_op.alter_column(
            'date',
            existing_type=sa.String(length=10),
            type_=sa.Date(),
            existing_nullable=False,
            postgresql_using="to_date(date, 'YYYY-MM-DD')",
        )


def downgrade() -> None:
    """Revert Expense.date from DATE back to String(10) YYYY-MM-DD."""
    bind = op.get_bind()
    if bind.dialect.name == 'sqlite':
        _sqlite_rebuild_expense_with_date_type(sa.String(length=10))
        return

    with op.batch_alter_table('expense', schema=None) as batch_op:
        batch_op.alter_column(
            'date',
            existing_type=sa.Date(),
            type_=sa.String(length=10),
            existing_nullable=False,
            postgresql_using="to_char(date, 'YYYY-MM-DD')",
        )
