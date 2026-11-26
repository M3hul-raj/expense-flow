"""replace idx_expense_user_date with (user_id, date, id) ordering index

Revision ID: 0004_expense_ordering_index
Revises: 0003_expense_date_type
Create Date: 2026-11-26 23:16:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0004_expense_ordering_index'
down_revision: Union[str, Sequence[str], None] = '0003_expense_date_type'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Replace (user_id, date) index with plain ascending (user_id, date, id) index."""
    bind = op.get_bind()
    existing = {idx['name'] for idx in sa.inspect(bind).get_indexes('expense')}

    if 'idx_expense_user_date' in existing:
        op.drop_index('idx_expense_user_date', table_name='expense')
    if 'idx_expense_user_date_id' not in existing:
        op.create_index('idx_expense_user_date_id', 'expense', ['user_id', 'date', 'id'], unique=False)


def downgrade() -> None:
    """Restore the original (user_id, date) index."""
    bind = op.get_bind()
    existing = {idx['name'] for idx in sa.inspect(bind).get_indexes('expense')}

    if 'idx_expense_user_date_id' in existing:
        op.drop_index('idx_expense_user_date_id', table_name='expense')
    if 'idx_expense_user_date' not in existing:
        op.create_index('idx_expense_user_date', 'expense', ['user_id', 'date'], unique=False)
