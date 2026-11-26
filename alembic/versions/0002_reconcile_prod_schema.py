"""reconcile production schema drift without rebuilding user table

Revision ID: 0002_reconcile_prod_schema
Revises: 0001_baseline
Create Date: 2026-11-26 22:47:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0002_reconcile_prod_schema'
down_revision: Union[str, Sequence[str], None] = '0001_baseline'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_EXPECTED_INDEXES = (
    ('budget', 'ix_budget_user_id', ['user_id']),
    ('expense', 'ix_expense_user_id', ['user_id']),
    ('expense', 'ix_expense_date', ['date']),
    ('expense', 'idx_expense_user_date', ['user_id', 'date']),
)


def _has_index(inspector, table_name: str, index_name: str, cols: list[str]) -> bool:
    for idx in inspector.get_indexes(table_name):
        if idx['name'] == index_name or (
            not idx.get('unique') and idx.get('column_names') == cols
        ):
            return True
    return False


def _has_unique_column(inspector, table_name: str, col_name: str) -> bool:
    has_unique_idx = any(
        idx.get('unique') and idx.get('column_names') == [col_name]
        for idx in inspector.get_indexes(table_name)
    )
    has_unique_uq = any(
        uq.get('column_names') == [col_name]
        for uq in inspector.get_unique_constraints(table_name)
    )
    return has_unique_idx or has_unique_uq


def upgrade() -> None:
    """Create missing budget/expense indexes conditionally and align user.avatar server_default."""
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    for table_name, index_name, cols in _EXPECTED_INDEXES:
        if not _has_index(inspector, table_name, index_name, cols):
            op.create_index(index_name, table_name, cols, unique=False)

    for col_name in ('username', 'email'):
        if not _has_unique_column(inspector, 'user', col_name):
            op.create_index(f'ix_user_{col_name}', 'user', [col_name], unique=True)

    user_cols = {c['name']: c for c in inspector.get_columns('user')}
    if user_cols['avatar'].get('default') is None:
        with op.batch_alter_table('user', schema=None) as batch_op:
            batch_op.alter_column(
                'avatar',
                existing_type=sa.String(length=50),
                existing_nullable=True,
                server_default=sa.text("'avatar_1'"),
            )


def downgrade() -> None:
    """Revert user.avatar server_default back to 0001_baseline state (keeps baseline indexes)."""
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    user_cols = {c['name']: c for c in inspector.get_columns('user')}
    if user_cols['avatar'].get('default') is not None:
        with op.batch_alter_table('user', schema=None) as batch_op:
            batch_op.alter_column(
                'avatar',
                existing_type=sa.String(length=50),
                existing_nullable=True,
                server_default=None,
            )
