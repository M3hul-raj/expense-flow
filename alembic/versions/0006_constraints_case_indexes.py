"""add DB CHECK constraints, NOT NULL tightening, ON DELETE CASCADE, and functional lower() unique indexes

Revision ID: 0006_constraints_case_indexes
Revises: 0005_money_paise
Create Date: 2026-11-27 00:20:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0006_constraints_case_indexes'
down_revision: Union[str, Sequence[str], None] = '0005_money_paise'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_NAMING_CONVENTION = {
    'ix': 'ix_%(column_0_label)s',
    'uq': 'uq_%(table_name)s_%(column_0_name)s',
    'ck': 'ck_%(table_name)s_%(constraint_name)s',
    'fk': 'fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s',
    'pk': 'pk_%(table_name)s',
}


def _validate_preconditions(bind) -> None:
    """Abort before DDL if case collisions, non-positive amounts, or orphan FKs exist."""
    username_collisions = int(
        bind.execute(
            sa.text(
                'SELECT COUNT(*) FROM ('
                'SELECT lower(username) FROM "user" GROUP BY lower(username) HAVING COUNT(*) > 1'
                ') AS t'
            )
        ).scalar_one()
    )
    email_collisions = int(
        bind.execute(
            sa.text(
                'SELECT COUNT(*) FROM ('
                'SELECT lower(email) FROM "user" GROUP BY lower(email) HAVING COUNT(*) > 1'
                ') AS t'
            )
        ).scalar_one()
    )
    if username_collisions or email_collisions:
        raise ValueError(
            'Cannot add case-insensitive unique indexes: '
            f'found {username_collisions} username and {email_collisions} email collisions'
        )

    bad_amounts = (
        int(bind.execute(sa.text('SELECT COUNT(*) FROM expense WHERE amount_paise <= 0')).scalar_one())
        + int(bind.execute(sa.text('SELECT COUNT(*) FROM budget WHERE amount_paise <= 0')).scalar_one())
        + int(
            bind.execute(
                sa.text(
                    'SELECT COUNT(*) FROM "user" '
                    'WHERE recurring_budget_paise IS NOT NULL AND recurring_budget_paise <= 0'
                )
            ).scalar_one()
        )
    )
    if bad_amounts:
        raise ValueError(
            f'Cannot enforce positive amount CHECK constraints: found {bad_amounts} non-positive rows'
        )

    orphan_rows = int(
        bind.execute(
            sa.text(
                'SELECT COUNT(*) FROM expense e '
                'WHERE NOT EXISTS (SELECT 1 FROM "user" u WHERE u.id = e.user_id)'
            )
        ).scalar_one()
    ) + int(
        bind.execute(
            sa.text(
                'SELECT COUNT(*) FROM budget b '
                'WHERE NOT EXISTS (SELECT 1 FROM "user" u WHERE u.id = b.user_id)'
            )
        ).scalar_one()
    )
    if orphan_rows:
        raise ValueError(
            f'Cannot enforce ON DELETE CASCADE foreign keys: found {orphan_rows} orphan rows'
        )


def _backfill_nullable_defaults(bind) -> None:
    """Fill safe legacy NULL defaults before tightening NOT NULL constraints."""
    bind.execute(sa.text('UPDATE "user" SET registration_date = CURRENT_TIMESTAMP WHERE registration_date IS NULL'))
    bind.execute(sa.text("""UPDATE "user" SET avatar = 'avatar_1' WHERE avatar IS NULL"""))
    bind.execute(sa.text("""UPDATE expense SET payment_method = 'Cash' WHERE payment_method IS NULL"""))
    bind.execute(sa.text('UPDATE expense SET created_at = CURRENT_TIMESTAMP WHERE created_at IS NULL'))


def _drop_user_id_fk(batch_op, inspector, table_name: str, default_name: str) -> None:
    for fk in inspector.get_foreign_keys(table_name):
        if fk.get('constrained_columns') == ['user_id']:
            batch_op.drop_constraint(fk.get('name') or default_name, type_='foreignkey')


def upgrade() -> None:
    """Apply CHECK, NOT NULL, ON DELETE CASCADE FKs, then lower() functional unique indexes."""
    bind = op.get_bind()
    _validate_preconditions(bind)
    _backfill_nullable_defaults(bind)

    inspector = sa.inspect(bind)
    user_uqs = inspector.get_unique_constraints('user')
    user_idxs = inspector.get_indexes('user')

    with op.batch_alter_table('user', schema=None, naming_convention=_NAMING_CONVENTION) as batch_op:
        for uq in user_uqs:
            cols = tuple(uq.get('column_names') or ())
            if cols in {('username',), ('email',)}:
                batch_op.drop_constraint(uq.get('name') or f'uq_user_{cols[0]}', type_='unique')
        for idx in user_idxs:
            if idx['name'] in {'ix_user_username', 'ix_user_email'}:
                batch_op.drop_index(idx['name'])
        batch_op.alter_column('registration_date', existing_type=sa.DateTime(), nullable=False)
        batch_op.alter_column(
            'avatar',
            existing_type=sa.String(length=50),
            nullable=False,
            existing_server_default=sa.text("'avatar_1'"),
        )
        batch_op.create_check_constraint(
            op.f('ck_user_recurring_budget_paise_positive'),
            'recurring_budget_paise > 0',
        )

    with op.batch_alter_table('expense', schema=None, naming_convention=_NAMING_CONVENTION) as batch_op:
        _drop_user_id_fk(batch_op, inspector, 'expense', 'fk_expense_user_id_user')
        batch_op.create_foreign_key(
            op.f('fk_expense_user_id_user'),
            'user',
            ['user_id'],
            ['id'],
            ondelete='CASCADE',
        )
        batch_op.alter_column('payment_method', existing_type=sa.String(length=50), nullable=False)
        batch_op.alter_column('created_at', existing_type=sa.DateTime(), nullable=False)
        batch_op.create_check_constraint(
            op.f('ck_expense_amount_paise_positive'),
            'amount_paise > 0',
        )

    with op.batch_alter_table('budget', schema=None, naming_convention=_NAMING_CONVENTION) as batch_op:
        _drop_user_id_fk(batch_op, inspector, 'budget', 'fk_budget_user_id_user')
        batch_op.create_foreign_key(
            op.f('fk_budget_user_id_user'),
            'user',
            ['user_id'],
            ['id'],
            ondelete='CASCADE',
        )
        batch_op.create_check_constraint(
            op.f('ck_budget_amount_paise_positive'),
            'amount_paise > 0',
        )

    # Functional expression indexes are created strictly AFTER all SQLite batch rebuilds
    op.create_index('ix_user_username_lower', 'user', [sa.text('lower(username)')], unique=True)
    op.create_index('ix_user_email_lower', 'user', [sa.text('lower(email)')], unique=True)


def downgrade() -> None:
    """Revert functional unique indexes, CASCADE FKs, NOT NULL tightening, and CHECK constraints."""
    op.drop_index('ix_user_email_lower', table_name='user')
    op.drop_index('ix_user_username_lower', table_name='user')

    with op.batch_alter_table('budget', schema=None, naming_convention=_NAMING_CONVENTION) as batch_op:
        batch_op.drop_constraint(op.f('ck_budget_amount_paise_positive'), type_='check')
        batch_op.drop_constraint(op.f('fk_budget_user_id_user'), type_='foreignkey')
        batch_op.create_foreign_key(
            op.f('fk_budget_user_id_user'),
            'user',
            ['user_id'],
            ['id'],
        )

    with op.batch_alter_table('expense', schema=None, naming_convention=_NAMING_CONVENTION) as batch_op:
        batch_op.drop_constraint(op.f('ck_expense_amount_paise_positive'), type_='check')
        batch_op.alter_column('created_at', existing_type=sa.DateTime(), nullable=True)
        batch_op.alter_column('payment_method', existing_type=sa.String(length=50), nullable=True)
        batch_op.drop_constraint(op.f('fk_expense_user_id_user'), type_='foreignkey')
        batch_op.create_foreign_key(
            op.f('fk_expense_user_id_user'),
            'user',
            ['user_id'],
            ['id'],
        )

    with op.batch_alter_table('user', schema=None, naming_convention=_NAMING_CONVENTION) as batch_op:
        batch_op.drop_constraint(op.f('ck_user_recurring_budget_paise_positive'), type_='check')
        batch_op.alter_column(
            'avatar',
            existing_type=sa.String(length=50),
            nullable=True,
            existing_server_default=sa.text("'avatar_1'"),
        )
        batch_op.alter_column('registration_date', existing_type=sa.DateTime(), nullable=True)
        batch_op.create_index('ix_user_username', ['username'], unique=True)
        batch_op.create_index('ix_user_email', ['email'], unique=True)
