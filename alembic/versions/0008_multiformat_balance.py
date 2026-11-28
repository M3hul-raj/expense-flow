"""add multiformat metadata and running-balance reconciliation columns

Revision ID: 0008_multiformat_balance
Revises: 0007_statement_import
Create Date: 2026-11-28 11:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0008_multiformat_balance'
down_revision: Union[str, Sequence[str], None] = '0007_statement_import'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_NAMING_CONVENTION = {
    'ix': 'ix_%(column_0_label)s',
    'uq': 'uq_%(table_name)s_%(column_0_name)s',
    'ck': 'ck_%(table_name)s_%(constraint_name)s',
    'fk': 'fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s',
    'pk': 'pk_%(table_name)s',
}


def upgrade() -> None:
    """Add balance verification and detected format columns to import_batch and import_row."""
    with op.batch_alter_table('import_batch', schema=None, naming_convention=_NAMING_CONVENTION) as batch_op:
        batch_op.add_column(sa.Column('detected_format_label', sa.String(length=80), nullable=True))
        batch_op.add_column(
            sa.Column(
                'balance_check_status',
                sa.String(length=20),
                nullable=False,
                server_default=sa.text("'unavailable'"),
            )
        )
        batch_op.add_column(
            sa.Column(
                'balance_mismatch_count',
                sa.Integer(),
                nullable=False,
                server_default=sa.text('0'),
            )
        )
        batch_op.create_check_constraint(
            'balance_check_status_valid',
            "balance_check_status IN ('verified', 'mismatch', 'unavailable')",
        )

    with op.batch_alter_table('import_row', schema=None, naming_convention=_NAMING_CONVENTION) as batch_op:
        batch_op.add_column(sa.Column('running_balance_paise', sa.BigInteger(), nullable=True))
        batch_op.add_column(sa.Column('balance_mismatch_note', sa.String(length=255), nullable=True))


def downgrade() -> None:
    """Remove balance verification and detected format columns cleanly."""
    with op.batch_alter_table('import_row', schema=None, naming_convention=_NAMING_CONVENTION) as batch_op:
        batch_op.drop_column('balance_mismatch_note')
        batch_op.drop_column('running_balance_paise')

    with op.batch_alter_table('import_batch', schema=None, naming_convention=_NAMING_CONVENTION) as batch_op:
        batch_op.drop_constraint(op.f('ck_import_batch_balance_check_status_valid'), type_='check')
        batch_op.drop_column('balance_mismatch_count')
        batch_op.drop_column('balance_check_status')
        batch_op.drop_column('detected_format_label')
