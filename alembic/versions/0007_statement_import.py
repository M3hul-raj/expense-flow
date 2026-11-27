"""add statement import staging tables, category rules, and expense import provenance

Revision ID: 0007_statement_import
Revises: 0006_constraints_case_indexes
Create Date: 2026-11-27 15:10:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0007_statement_import'
down_revision: Union[str, Sequence[str], None] = '0006_constraints_case_indexes'
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
    """Create import_batch, category_rule, import_row and add import provenance columns to expense."""
    op.create_table(
        'import_batch',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=False),
        sa.Column('bank_profile', sa.String(length=64), nullable=False),
        sa.Column('original_filename', sa.String(length=255), nullable=False),
        sa.Column('file_sha256', sa.String(length=64), nullable=False),
        sa.Column('detected_encoding', sa.String(length=32), nullable=False),
        sa.Column('status', sa.String(length=20), nullable=False),
        sa.Column('total_rows', sa.Integer(), nullable=False),
        sa.Column('ready_count', sa.Integer(), nullable=False),
        sa.Column('duplicate_manual_count', sa.Integer(), nullable=False),
        sa.Column('already_imported_count', sa.Integer(), nullable=False),
        sa.Column('skipped_credit_count', sa.Integer(), nullable=False),
        sa.Column('invalid_count', sa.Integer(), nullable=False),
        sa.Column('imported_count', sa.Integer(), nullable=False),
        sa.Column('skipped_count', sa.Integer(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('confirmed_at', sa.DateTime(), nullable=True),
        sa.Column('undone_at', sa.DateTime(), nullable=True),
        sa.CheckConstraint("status IN ('staged', 'confirmed', 'undone')", name=op.f('ck_import_batch_status_valid')),
        sa.ForeignKeyConstraint(['user_id'], ['user.id'], name=op.f('fk_import_batch_user_id_user'), ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_import_batch')),
    )
    op.create_index('idx_import_batch_user_created', 'import_batch', ['user_id', 'created_at', 'id'], unique=False)
    op.create_index(op.f('ix_import_batch_user_id'), 'import_batch', ['user_id'], unique=False)

    op.create_table(
        'category_rule',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=False),
        sa.Column('pattern', sa.String(length=120), nullable=False),
        sa.Column('pattern_normalized', sa.String(length=120), nullable=False),
        sa.Column('category', sa.String(length=50), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.CheckConstraint("pattern_normalized <> ''", name=op.f('ck_category_rule_pattern_nonempty')),
        sa.ForeignKeyConstraint(['user_id'], ['user.id'], name=op.f('fk_category_rule_user_id_user'), ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_category_rule')),
        sa.UniqueConstraint('user_id', 'pattern_normalized', name='uq_category_rule_user_pattern'),
    )
    op.create_index(op.f('ix_category_rule_user_id'), 'category_rule', ['user_id'], unique=False)

    with op.batch_alter_table('expense', schema=None, naming_convention=_NAMING_CONVENTION) as batch_op:
        batch_op.add_column(sa.Column('import_batch_id', sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column('import_fingerprint', sa.String(length=64), nullable=True))
        batch_op.create_index(op.f('ix_expense_import_batch_id'), ['import_batch_id'], unique=False)
        batch_op.create_unique_constraint('uq_expense_user_import_fingerprint', ['user_id', 'import_fingerprint'])
        batch_op.create_foreign_key(
            op.f('fk_expense_import_batch_id_import_batch'),
            'import_batch',
            ['import_batch_id'],
            ['id'],
            ondelete='SET NULL',
        )

    op.create_table(
        'import_row',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('batch_id', sa.Integer(), nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=False),
        sa.Column('line_number', sa.Integer(), nullable=False),
        sa.Column('row_status', sa.String(length=32), nullable=False),
        sa.Column('txn_date', sa.Date(), nullable=True),
        sa.Column('amount_paise', sa.BigInteger(), nullable=True),
        sa.Column('raw_narration', sa.String(length=500), nullable=False),
        sa.Column('description', sa.String(length=255), nullable=False),
        sa.Column('ref_no', sa.String(length=100), nullable=False),
        sa.Column('category', sa.String(length=50), nullable=False),
        sa.Column('payment_method', sa.String(length=50), nullable=False),
        sa.Column('fingerprint', sa.String(length=64), nullable=True),
        sa.Column('error_reason', sa.String(length=255), nullable=True),
        sa.Column('raw_preview', sa.String(length=500), nullable=False),
        sa.Column('matched_expense_id', sa.Integer(), nullable=True),
        sa.Column('created_expense_id', sa.Integer(), nullable=True),
        sa.Column('include_on_confirm', sa.Boolean(), nullable=False),
        sa.CheckConstraint('amount_paise IS NULL OR amount_paise > 0', name=op.f('ck_import_row_amount_paise_positive')),
        sa.CheckConstraint(
            "row_status IN ('ready', 'possible_duplicate', 'already_imported', 'skipped_credit', 'invalid', 'imported', 'skipped_user', 'undone')",
            name=op.f('ck_import_row_row_status_valid'),
        ),
        sa.ForeignKeyConstraint(['batch_id'], ['import_batch.id'], name=op.f('fk_import_row_batch_id_import_batch'), ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['created_expense_id'], ['expense.id'], name=op.f('fk_import_row_created_expense_id_expense'), ondelete='SET NULL'),
        sa.ForeignKeyConstraint(['matched_expense_id'], ['expense.id'], name=op.f('fk_import_row_matched_expense_id_expense'), ondelete='SET NULL'),
        sa.ForeignKeyConstraint(['user_id'], ['user.id'], name=op.f('fk_import_row_user_id_user'), ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_import_row')),
    )
    op.create_index('idx_import_row_batch_line', 'import_row', ['batch_id', 'line_number'], unique=False)
    op.create_index(op.f('ix_import_row_batch_id'), 'import_row', ['batch_id'], unique=False)
    op.create_index(op.f('ix_import_row_user_id'), 'import_row', ['user_id'], unique=False)


def downgrade() -> None:
    """Remove import staging tables, category rules, and expense import provenance columns."""
    op.drop_index(op.f('ix_import_row_user_id'), table_name='import_row')
    op.drop_index(op.f('ix_import_row_batch_id'), table_name='import_row')
    op.drop_index('idx_import_row_batch_line', table_name='import_row')
    op.drop_table('import_row')

    with op.batch_alter_table('expense', schema=None, naming_convention=_NAMING_CONVENTION) as batch_op:
        batch_op.drop_constraint(op.f('fk_expense_import_batch_id_import_batch'), type_='foreignkey')
        batch_op.drop_constraint('uq_expense_user_import_fingerprint', type_='unique')
        batch_op.drop_index(op.f('ix_expense_import_batch_id'))
        batch_op.drop_column('import_fingerprint')
        batch_op.drop_column('import_batch_id')

    op.drop_index(op.f('ix_category_rule_user_id'), table_name='category_rule')
    op.drop_table('category_rule')

    op.drop_index(op.f('ix_import_batch_user_id'), table_name='import_batch')
    op.drop_index('idx_import_batch_user_created', table_name='import_batch')
    op.drop_table('import_batch')
