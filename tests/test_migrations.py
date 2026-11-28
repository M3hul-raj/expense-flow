"""
test_migrations.py — Alembic migration, baseline, and startup schema tests.
"""
import os
from pathlib import Path
import sqlite3

from alembic import command
from alembic.config import Config
import pytest
import sqlalchemy as sa

from app import create_app
from models import NAMING_CONVENTION, db


REPO_ROOT = Path(__file__).resolve().parent.parent


def _alembic_config(db_uri):
    cfg = Config(str(REPO_ROOT / 'alembic.ini'))
    cfg.set_main_option('script_location', str(REPO_ROOT / 'alembic'))
    cfg.set_main_option('sqlalchemy.url', db_uri)
    return cfg


def _get_test_uri(tmp_path, name='mig.db'):
    override = os.environ.get('EXPENSEFLOW_TEST_DATABASE_URI', '').strip()
    if override:
        return override, True
    db_file = tmp_path / name
    return f"sqlite:///{db_file.as_posix()}", False


class TestAlembicBaseline:
    """Verify startup never runs create_all() and 0001_baseline works on SQLite and Postgres."""

    def test_create_app_does_not_auto_create_tables(self, monkeypatch, tmp_path):
        """create_app() must rely on Alembic rather than running db.create_all() at startup."""
        db_file = tmp_path / 'empty_startup.db'
        uri = f"sqlite:///{db_file.as_posix()}"
        monkeypatch.setenv('EXPENSEFLOW_DATABASE_URI', uri)

        dev_app = create_app('development')
        try:
            with dev_app.app_context():
                tables = sa.inspect(db.engine).get_table_names()
            assert tables == []
        finally:
            with dev_app.app_context():
                db.engine.dispose()

    def test_alembic_upgrade_head_and_downgrade_base(self, tmp_path):
        """0001_baseline must create all baseline tables/indexes cleanly and downgrade back to empty."""
        uri, is_postgres = _get_test_uri(tmp_path, 'baseline.db')
        cfg = _alembic_config(uri)
        engine = sa.create_engine(uri)
        try:
            if is_postgres:
                # Ensure a clean target DB when re-using CI/Docker Postgres database
                with engine.begin() as conn:
                    conn.execute(sa.text('DROP SCHEMA public CASCADE; CREATE SCHEMA public;'))

            command.upgrade(cfg, 'head')
            insp = sa.inspect(engine)
            assert {'user', 'expense', 'budget', 'alembic_version'}.issubset(set(insp.get_table_names()))

            expense_indexes = {idx['name'] for idx in insp.get_indexes('expense')}
            assert {'ix_expense_user_id', 'ix_expense_date', 'idx_expense_user_date_id'}.issubset(expense_indexes)
            assert 'idx_expense_user_date' not in expense_indexes

            budget_uqs = {uq['name'] for uq in insp.get_unique_constraints('budget')}
            assert 'unique_user_month_budget' in budget_uqs

            command.downgrade(cfg, 'base')
            insp_after = sa.inspect(engine)
            remaining = set(insp_after.get_table_names())
            assert not ({'user', 'expense', 'budget'} & remaining)

            command.upgrade(cfg, '0001_baseline')
            assert {'user', 'expense', 'budget'}.issubset(set(sa.inspect(engine).get_table_names()))
        finally:
            if is_postgres:
                with engine.begin() as conn:
                    conn.execute(sa.text('DROP SCHEMA public CASCADE; CREATE SCHEMA public;'))
            engine.dispose()

    def test_stamp_preexisting_sqlite_db_preserves_rows_and_tracks_revision(self, tmp_path):
        """Stamping an existing Stage-1A SQLite DB records 0001_baseline without altering rows."""
        db_file = tmp_path / 'legacy_stage1a.db'
        conn = sqlite3.connect(db_file)
        try:
            conn.executescript(
                """
                CREATE TABLE user (
                    id INTEGER NOT NULL PRIMARY KEY,
                    username VARCHAR(80) NOT NULL UNIQUE,
                    email VARCHAR(120) NOT NULL UNIQUE,
                    password_hash VARCHAR(256) NOT NULL,
                    recurring_budget FLOAT,
                    registration_date DATETIME,
                    avatar VARCHAR(50),
                    display_name VARCHAR(80)
                );
                CREATE INDEX ix_user_username ON user (username);
                CREATE INDEX ix_user_email ON user (email);
                CREATE TABLE expense (
                    id INTEGER NOT NULL PRIMARY KEY,
                    user_id INTEGER NOT NULL REFERENCES user(id),
                    date VARCHAR(10) NOT NULL,
                    category VARCHAR(50) NOT NULL,
                    amount FLOAT NOT NULL,
                    description VARCHAR(255),
                    payment_method VARCHAR(50),
                    created_at DATETIME
                );
                CREATE INDEX ix_expense_user_id ON expense (user_id);
                CREATE INDEX ix_expense_date ON expense (date);
                CREATE INDEX idx_expense_user_date ON expense (user_id, date);
                CREATE TABLE budget (
                    id INTEGER NOT NULL PRIMARY KEY,
                    user_id INTEGER NOT NULL REFERENCES user(id),
                    year_month VARCHAR(7) NOT NULL,
                    amount FLOAT NOT NULL,
                    CONSTRAINT unique_user_month_budget UNIQUE (user_id, year_month)
                );
                CREATE INDEX ix_budget_user_id ON budget (user_id);
                INSERT INTO user (id, username, email, password_hash) VALUES (1, 'alice', 'alice@example.com', 'hash');
                """
            )
            conn.commit()
        finally:
            conn.close()

        uri = f"sqlite:///{db_file.as_posix()}"
        cfg = _alembic_config(uri)
        command.stamp(cfg, '0001_baseline')

        engine = sa.create_engine(uri)
        try:
            with engine.connect() as sa_conn:
                ver = sa_conn.execute(sa.text('SELECT version_num FROM alembic_version')).scalar_one()
                user_count = sa_conn.execute(sa.text('SELECT COUNT(*) FROM "user"')).scalar_one()
            assert ver == '0001_baseline'
            assert user_count == 1
        finally:
            engine.dispose()

    def test_metadata_uses_explicit_naming_convention(self):
        """SQLAlchemy MetaData must define deterministic constraint naming conventions."""
        assert db.metadata.naming_convention == NAMING_CONVENTION
        assert {'ix', 'uq', 'ck', 'fk', 'pk'}.issubset(set(NAMING_CONVENTION))


class TestDeployScriptModes:
    """Tests for explicit --stamp / --upgrade-to CLI modes in deploy/pa_deploy.sh."""

    @staticmethod
    def _find_bash():
        import shutil
        import subprocess

        candidates = [
            r'C:\Program Files\Git\bin\bash.exe',
            r'C:\Program Files\Git\usr\bin\bash.exe',
            shutil.which('bash'),
        ]
        for candidate in candidates:
            if candidate and Path(candidate).is_file():
                probe = subprocess.run([candidate, '-c', 'echo ok'], capture_output=True, text=True)
                if probe.returncode == 0 and probe.stdout.strip() == 'ok':
                    return candidate
        pytest.skip('Working bash binary not available')

    def _run_deploy(self, *args):
        import subprocess

        bash = self._find_bash()
        script = (REPO_ROOT / 'deploy' / 'pa_deploy.sh').as_posix()
        return subprocess.run([bash, script, *args], capture_output=True, text=True, cwd=str(REPO_ROOT))

    @pytest.mark.parametrize(
        'bad_args',
        [
            (),
            ('--dry-run',),
            ('--unknown',),
            ('--stamp',),
            ('--stamp', '--dry-run'),
            ('--upgrade-to',),
            ('--upgrade-to', '--dry-run'),
            ('--stamp', '0001_baseline', '--upgrade-to', '0002_dates'),
            ('--stamp', 'head'),
            ('--upgrade-to', 'head'),
            ('--upgrade-to', 'heads'),
            ('--upgrade-to', 'base'),
            ('--stamp', '0001_baseline', 'extra_arg'),
        ],
    )
    def test_rejects_missing_target_or_invalid_modes(self, bad_args):
        """deploy/pa_deploy.sh must reject missing mode, missing revision, extra args, or head/base."""
        res = self._run_deploy(*bad_args)
        assert res.returncode != 0

    def test_stamp_dry_run_outputs_alembic_stamp_and_locked_install(self):
        res = self._run_deploy('--stamp', '0001_baseline', '--dry-run')
        assert res.returncode == 0, res.stderr
        assert 'alembic stamp "0001_baseline"' in res.stdout or 'alembic stamp 0001_baseline' in res.stdout
        assert 'requirements.lock' in res.stdout
        assert 'alembic upgrade' not in res.stdout

    def test_upgrade_to_dry_run_outputs_explicit_target_and_locked_install(self):
        res = self._run_deploy('--upgrade-to', '0002_dates', '--dry-run')
        assert res.returncode == 0, res.stderr
        assert 'alembic upgrade "0002_dates"' in res.stdout or 'alembic upgrade 0002_dates' in res.stdout
        assert 'requirements.lock' in res.stdout
        assert 'alembic stamp' not in res.stdout
        assert 'upgrade head' not in res.stdout


class TestSchemaReconciliation:
    """Acceptance tests for 0002_reconcile_prod_schema across legacy SQLite, fresh SQLite, and Postgres."""

    @staticmethod
    def _create_legacy_stage1a_db(db_file, seed_data=False):
        conn = sqlite3.connect(db_file)
        try:
            conn.execute('PRAGMA foreign_keys=ON')
            conn.executescript(
                """
                CREATE TABLE user (
                    id INTEGER NOT NULL,
                    username VARCHAR(80) NOT NULL,
                    email VARCHAR(120) NOT NULL,
                    password_hash VARCHAR(256) NOT NULL,
                    recurring_budget FLOAT,
                    registration_date DATETIME,
                    avatar VARCHAR(50) DEFAULT 'avatar_1',
                    display_name VARCHAR(80),
                    PRIMARY KEY (id),
                    UNIQUE (username),
                    UNIQUE (email)
                );
                CREATE TABLE expense (
                    id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    date VARCHAR(10) NOT NULL,
                    category VARCHAR(50) NOT NULL,
                    amount FLOAT NOT NULL,
                    description VARCHAR(255),
                    payment_method VARCHAR(50),
                    created_at DATETIME,
                    PRIMARY KEY (id),
                    FOREIGN KEY(user_id) REFERENCES user (id)
                );
                CREATE TABLE budget (
                    id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    year_month VARCHAR(7) NOT NULL,
                    amount FLOAT NOT NULL,
                    PRIMARY KEY (id),
                    CONSTRAINT unique_user_month_budget UNIQUE (user_id, year_month),
                    FOREIGN KEY(user_id) REFERENCES user (id)
                );
                """
            )
            if seed_data:
                conn.execute(
                    "INSERT INTO user (id, username, email, password_hash, recurring_budget) VALUES (1, 'Alice', 'Alice@Test.com', 'hash1', 3000.0)"
                )
                conn.execute(
                    "INSERT INTO budget (id, user_id, year_month, amount) VALUES (1, 1, '2026-11', 5000.0)"
                )
                conn.execute(
                    "INSERT INTO expense (id, user_id, date, category, amount, description, payment_method) VALUES (1, 1, '2026-11-26', 'Food', 149.5, 'Dinner', 'UPI')"
                )
            conn.commit()
        finally:
            conn.close()

    @staticmethod
    def _unexplained_diffs(engine):
        from alembic.autogenerate import compare_metadata
        from alembic.migration import MigrationContext

        with engine.connect() as conn:
            insp = sa.inspect(conn)
            user_unique_cols = (
                {tuple(u['column_names']) for u in insp.get_unique_constraints('user')}
                | {tuple(i['column_names']) for i in insp.get_indexes('user') if i.get('unique')}
            )
            ctx = MigrationContext.configure(
                conn, opts={'compare_type': True, 'compare_server_default': True}
            )
            raw_diffs = compare_metadata(ctx, db.metadata)

        filtered = []
        for diff in raw_diffs:
            if (
                isinstance(diff, tuple)
                and diff[0] == 'add_index'
                and diff[1].table.name == 'user'
                and diff[1].unique
                and tuple(c.name for c in diff[1].columns) in user_unique_cols
            ):
                continue
            filtered.append(diff)
        return filtered

    def test_reconcile_legacy_stage1a_sqlite_preserves_user_and_adds_missing_indexes(self, tmp_path):
        db_file = tmp_path / 'legacy_prod_copy.db'
        self._create_legacy_stage1a_db(db_file, seed_data=True)
        uri = f"sqlite:///{db_file.as_posix()}"
        cfg = _alembic_config(uri)

        command.stamp(cfg, '0001_baseline')
        command.upgrade(cfg, '0002_reconcile_prod_schema')

        engine = sa.create_engine(uri)
        try:
            insp = sa.inspect(engine)
            assert 'ix_budget_user_id' in {i['name'] for i in insp.get_indexes('budget')}
            assert {'ix_expense_user_id', 'ix_expense_date', 'idx_expense_user_date'}.issubset(
                {i['name'] for i in insp.get_indexes('expense')}
            )
            # Preserved existing UNIQUE constraints on user without rebuilding or creating duplicate indexes
            assert insp.get_indexes('user') == []
            assert {tuple(u['column_names']) for u in insp.get_unique_constraints('user')} == {
                ('username',),
                ('email',),
            }
            assert len(insp.get_unique_constraints('user')) == 2
            command.upgrade(cfg, 'head')
            assert self._unexplained_diffs(engine) == []

            with engine.connect() as conn:
                fk_violations = list(conn.execute(sa.text('PRAGMA foreign_key_check')))
                assert fk_violations == []
                assert conn.execute(sa.text('SELECT COUNT(*) FROM "user"')).scalar_one() == 1
                assert conn.execute(sa.text('SELECT COUNT(*) FROM budget')).scalar_one() == 1
                assert conn.execute(sa.text('SELECT COUNT(*) FROM expense')).scalar_one() == 1
                assert conn.execute(sa.text('SELECT amount_paise FROM expense WHERE id = 1')).scalar_one() == 14950
                assert conn.execute(sa.text('SELECT amount_paise FROM budget WHERE id = 1')).scalar_one() == 500000
                assert conn.execute(sa.text('SELECT recurring_budget_paise FROM "user" WHERE id = 1')).scalar_one() == 300000
        finally:
            engine.dispose()

    def test_reconcile_fresh_sqlite_and_postgres_upgrade_downgrade(self, tmp_path):
        from alembic.autogenerate import compare_metadata
        from alembic.migration import MigrationContext

        uri, is_postgres = _get_test_uri(tmp_path, 'reconcile_fresh.db')
        cfg = _alembic_config(uri)
        engine = sa.create_engine(uri)
        try:
            if is_postgres:
                with engine.begin() as conn:
                    conn.execute(sa.text('DROP SCHEMA public CASCADE; CREATE SCHEMA public;'))

            command.upgrade(cfg, '0001_baseline')
            command.upgrade(cfg, '0002_reconcile_prod_schema')

            insp = sa.inspect(engine)
            avatar_col = next(c for c in insp.get_columns('user') if c['name'] == 'avatar')
            assert avatar_col.get('default') is not None
            user_indexes = insp.get_indexes('user')
            assert {i['name'] for i in user_indexes} == {'ix_user_username', 'ix_user_email'}
            assert len(user_indexes) == 2

            command.downgrade(cfg, '0001_baseline')
            insp_down = sa.inspect(engine)
            assert next(c for c in insp_down.get_columns('user') if c['name'] == 'avatar').get('default') is None
            assert 'idx_expense_user_date' in {i['name'] for i in insp_down.get_indexes('expense')}
            assert 'ix_budget_user_id' in {i['name'] for i in insp_down.get_indexes('budget')}

            command.upgrade(cfg, '0002_reconcile_prod_schema')
            command.upgrade(cfg, 'head')
            with engine.connect() as conn:
                ctx = MigrationContext.configure(
                    conn, opts={'compare_type': True, 'compare_server_default': True}
                )
                assert compare_metadata(ctx, db.metadata) == []
        finally:
            if is_postgres:
                with engine.begin() as conn:
                    conn.execute(sa.text('DROP SCHEMA public CASCADE; CREATE SCHEMA public;'))
            engine.dispose()

    def test_m4a_expense_alter_succeeds_after_reconciliation(self, tmp_path):
        from alembic.migration import MigrationContext
        from alembic.operations import Operations

        db_file = tmp_path / 'reconcile_then_m4a.db'
        self._create_legacy_stage1a_db(db_file, seed_data=True)
        uri = f"sqlite:///{db_file.as_posix()}"
        cfg = _alembic_config(uri)

        command.stamp(cfg, '0001_baseline')
        command.upgrade(cfg, '0002_reconcile_prod_schema')

        engine = sa.create_engine(uri)
        try:
            with engine.begin() as conn:
                op = Operations(MigrationContext.configure(conn))
                with op.batch_alter_table('expense') as batch_op:
                    batch_op.alter_column(
                        'date',
                        existing_type=sa.String(length=10),
                        type_=sa.Date(),
                        existing_nullable=False,
                    )
                    batch_op.drop_index('idx_expense_user_date')
                    batch_op.create_index('idx_expense_user_date_id', ['user_id', 'date', 'id'], unique=False)

            insp = sa.inspect(engine)
            expense_indexes = {i['name'] for i in insp.get_indexes('expense')}
            assert 'idx_expense_user_date_id' in expense_indexes
            assert 'idx_expense_user_date' not in expense_indexes
            assert insp.get_foreign_keys('expense')[0]['referred_table'] == 'user'
            with engine.connect() as conn:
                assert list(conn.execute(sa.text('PRAGMA foreign_key_check'))) == []
                assert conn.execute(sa.text('SELECT COUNT(*) FROM expense')).scalar_one() == 1
        finally:
            engine.dispose()


class TestExpenseDateMigration:
    """Tests for 0003_expense_date_type (String(10) -> DATE) upgrade, validation guard, and downgrade."""

    def test_0003_migrates_expense_date_to_date_preserves_counts_and_downgrades(self, tmp_path):
        from datetime import date as py_date
        from scripts.verify_migration import audit_database, compare_reports

        uri, is_postgres = _get_test_uri(tmp_path, 'date_mig.db')
        cfg = _alembic_config(uri)
        engine = sa.create_engine(uri)
        try:
            if is_postgres:
                with engine.begin() as conn:
                    conn.execute(sa.text('DROP SCHEMA public CASCADE; CREATE SCHEMA public;'))

            command.upgrade(cfg, '0002_reconcile_prod_schema')
            with engine.begin() as conn:
                conn.execute(sa.text(
                    """INSERT INTO "user" (id, username, email, password_hash, registration_date, avatar)
                       VALUES (1, 'dateuser', 'date@example.com', 'h', CURRENT_TIMESTAMP, 'avatar_1')"""
                ))
                conn.execute(sa.text(
                    """INSERT INTO expense (id, user_id, date, category, amount, payment_method, created_at)
                       VALUES
                       (1, 1, '2024-02-29', 'Food', 100.0, 'UPI', CURRENT_TIMESTAMP),
                       (2, 1, '2026-01-01', 'Bills', 250.5, 'Cash', CURRENT_TIMESTAMP),
                       (3, 1, '2026-12-31', 'Travel', 75.25, 'Card', CURRENT_TIMESTAMP)"""
                ))
                if is_postgres:
                    conn.execute(sa.text("""SELECT setval(pg_get_serial_sequence('expense', 'id'), 3)"""))
                    conn.execute(sa.text("""SELECT setval(pg_get_serial_sequence('"user"', 'id'), 1)"""))

            before = audit_database(uri)
            assert before['unparseable_dates'] == 0

            command.upgrade(cfg, '0003_expense_date_type')
            insp = sa.inspect(engine)
            date_col = next(c for c in insp.get_columns('expense') if c['name'] == 'date')
            assert isinstance(date_col['type'], sa.Date)
            assert {'ix_expense_user_id', 'ix_expense_date', 'idx_expense_user_date'}.issubset(
                {i['name'] for i in insp.get_indexes('expense')}
            )
            assert insp.get_foreign_keys('expense')[0]['referred_table'] == 'user'

            after_up = audit_database(uri)
            assert after_up['unparseable_dates'] == 0
            assert compare_reports(before, after_up) == []

            with engine.connect() as conn:
                reflected_exp = sa.Table('expense', sa.MetaData(), autoload_with=conn)
                dates = [
                    r[0]
                    for r in conn.execute(sa.select(reflected_exp.c.date).order_by(reflected_exp.c.id.asc()))
                ]
                if not is_postgres:
                    raw_types = [r[0] for r in conn.execute(sa.text('SELECT typeof(date) FROM expense'))]
                    assert raw_types == ['text', 'text', 'text']
            assert dates == [py_date(2024, 2, 29), py_date(2026, 1, 1), py_date(2026, 12, 31)]

            command.downgrade(cfg, '0002_reconcile_prod_schema')
            insp_down = sa.inspect(engine)
            date_col_down = next(c for c in insp_down.get_columns('expense') if c['name'] == 'date')
            assert isinstance(date_col_down['type'], sa.String)
            with engine.connect() as conn:
                str_dates = [
                    r[0]
                    for r in conn.execute(sa.text('SELECT date FROM expense ORDER BY id ASC'))
                ]
            assert str_dates == ['2024-02-29', '2026-01-01', '2026-12-31']
            assert compare_reports(before, audit_database(uri)) == []

            command.upgrade(cfg, '0003_expense_date_type')
        finally:
            if is_postgres:
                with engine.begin() as conn:
                    conn.execute(sa.text('DROP SCHEMA public CASCADE; CREATE SCHEMA public;'))
            engine.dispose()

    def test_0003_aborts_when_unparseable_date_present(self, tmp_path):
        db_file = tmp_path / 'bad_date_abort.db'
        uri = f"sqlite:///{db_file.as_posix()}"
        cfg = _alembic_config(uri)
        command.upgrade(cfg, '0002_reconcile_prod_schema')

        engine = sa.create_engine(uri)
        try:
            with engine.begin() as conn:
                conn.execute(sa.text(
                    """INSERT INTO "user" (id, username, email, password_hash, registration_date, avatar)
                       VALUES (1, 'baduser', 'bad@example.com', 'h', CURRENT_TIMESTAMP, 'avatar_1')"""
                ))
                conn.execute(sa.text(
                    """INSERT INTO expense (id, user_id, date, category, amount, payment_method, created_at)
                       VALUES (1, 1, '2026-02-30', 'Food', 100.0, 'Cash', CURRENT_TIMESTAMP)"""
                ))

            with pytest.raises(Exception, match='unparseable'):
                command.upgrade(cfg, '0003_expense_date_type')

            with engine.connect() as conn:
                ver = conn.execute(sa.text('SELECT version_num FROM alembic_version')).scalar_one()
                assert ver == '0002_reconcile_prod_schema'
        finally:
            engine.dispose()


class TestExpenseOrderingIndex:
    """Tests for 0004_expense_ordering_index replacing idx_expense_user_date and EXPLAIN plans."""

    def test_0004_replaces_expense_date_index_with_user_date_id_and_downgrades(self, tmp_path):
        from alembic.autogenerate import compare_metadata
        from alembic.migration import MigrationContext

        uri, is_postgres = _get_test_uri(tmp_path, 'ord_idx.db')
        cfg = _alembic_config(uri)
        engine = sa.create_engine(uri)
        try:
            if is_postgres:
                with engine.begin() as conn:
                    conn.execute(sa.text('DROP SCHEMA public CASCADE; CREATE SCHEMA public;'))

            command.upgrade(cfg, '0003_expense_date_type')
            insp_before = sa.inspect(engine)
            names_before = {i['name'] for i in insp_before.get_indexes('expense')}
            assert 'idx_expense_user_date' in names_before
            assert 'idx_expense_user_date_id' not in names_before

            command.upgrade(cfg, '0004_expense_ordering_index')
            insp_up = sa.inspect(engine)
            indexes_up = {i['name']: i for i in insp_up.get_indexes('expense')}
            assert 'idx_expense_user_date_id' in indexes_up
            assert 'idx_expense_user_date' not in indexes_up
            assert indexes_up['idx_expense_user_date_id']['column_names'] == ['user_id', 'date', 'id']
            assert not indexes_up['idx_expense_user_date_id']['unique']
            assert {'ix_expense_user_id', 'ix_expense_date'}.issubset(indexes_up.keys())

            command.downgrade(cfg, '0003_expense_date_type')
            insp_down = sa.inspect(engine)
            indexes_down = {i['name']: i for i in insp_down.get_indexes('expense')}
            assert 'idx_expense_user_date' in indexes_down
            assert 'idx_expense_user_date_id' not in indexes_down
            assert indexes_down['idx_expense_user_date']['column_names'] == ['user_id', 'date']

            command.upgrade(cfg, '0004_expense_ordering_index')
            command.upgrade(cfg, 'head')
            with engine.connect() as conn:
                ctx = MigrationContext.configure(
                    conn, opts={'compare_type': True, 'compare_server_default': True}
                )
                assert compare_metadata(ctx, db.metadata) == []
        finally:
            if is_postgres:
                with engine.begin() as conn:
                    conn.execute(sa.text('DROP SCHEMA public CASCADE; CREATE SCHEMA public;'))
            engine.dispose()

    def test_0004_explain_plans_use_ordering_index_without_sort(self, tmp_path):
        from datetime import date as py_date, timedelta

        uri, is_postgres = _get_test_uri(tmp_path, 'explain_idx.db')
        cfg = _alembic_config(uri)
        engine = sa.create_engine(uri)
        try:
            if is_postgres:
                with engine.begin() as conn:
                    conn.execute(sa.text('DROP SCHEMA public CASCADE; CREATE SCHEMA public;'))

            command.upgrade(cfg, 'head')

            user_rows = [
                {
                    'id': u,
                    'username': f'planuser{u}',
                    'email': f'plan{u}@example.com',
                    'password_hash': 'h',
                    'avatar': 'avatar_1',
                }
                for u in range(1, 21)
            ]
            base_day = py_date(2026, 1, 1)
            exp_rows = [
                {
                    'user_id': (i % 20) + 1,
                    'date': (
                        base_day + timedelta(days=(i // 40))
                        if is_postgres
                        else (base_day + timedelta(days=(i // 40))).isoformat()
                    ),
                    'category': 'Food',
                    'amount_paise': 5000 + ((i % 10) * 100),
                    'description': f'row {i}',
                    'payment_method': 'UPI',
                }
                for i in range(1000)
            ]

            with engine.begin() as conn:
                conn.execute(
                    sa.text(
                        """INSERT INTO "user" (id, username, email, password_hash, registration_date, avatar)
                           VALUES (:id, :username, :email, :password_hash, CURRENT_TIMESTAMP, :avatar)"""
                    ),
                    user_rows,
                )
                conn.execute(
                    sa.text(
                        """INSERT INTO expense (user_id, date, category, amount_paise, description, payment_method, created_at)
                           VALUES (:user_id, :date, :category, :amount_paise, :description, :payment_method, CURRENT_TIMESTAMP)"""
                    ),
                    exp_rows,
                )
                conn.execute(sa.text('ANALYZE expense'))

            query_full = (
                'SELECT id, user_id, date, category, amount_paise, description, payment_method, created_at '
                'FROM expense WHERE user_id = :uid ORDER BY date DESC, id DESC'
            )
            query_limit = f'{query_full} LIMIT 5'
            query_month_limit = (
                'SELECT id, user_id, date, category, amount_paise, description, payment_method, created_at '
                'FROM expense WHERE user_id = :uid AND date >= :start_d AND date < :end_d '
                'ORDER BY date DESC, id DESC LIMIT 10'
            )
            start_d = py_date(2026, 1, 1) if is_postgres else '2026-01-01'
            end_d = py_date(2026, 2, 1) if is_postgres else '2026-02-01'

            with engine.connect() as conn:
                if is_postgres:
                    for sql, params in (
                        (query_limit, {'uid': 1}),
                        (query_month_limit, {'uid': 1, 'start_d': start_d, 'end_d': end_d}),
                    ):
                        plan_lines = [
                            r[0]
                            for r in conn.execute(sa.text(f'EXPLAIN (FORMAT TEXT) {sql}'), params)
                        ]
                        plan_text = '\n'.join(plan_lines)
                        assert 'Index Scan Backward using idx_expense_user_date_id' in plan_text, plan_text
                        assert not any('Sort' in line for line in plan_lines), plan_text
                else:
                    for sql, params in (
                        (query_full, {'uid': 1}),
                        (query_limit, {'uid': 1}),
                        (query_month_limit, {'uid': 1, 'start_d': start_d, 'end_d': end_d}),
                    ):
                        plan_rows = [
                            r[-1]
                            for r in conn.execute(sa.text(f'EXPLAIN QUERY PLAN {sql}'), params)
                        ]
                        plan_text = '\n'.join(plan_rows)
                        assert 'idx_expense_user_date_id' in plan_text, plan_text
                        assert 'USE TEMP B-TREE FOR ORDER BY' not in plan_text, plan_text
        finally:
            if is_postgres:
                with engine.begin() as conn:
                    conn.execute(sa.text('DROP SCHEMA public CASCADE; CREATE SCHEMA public;'))
            engine.dispose()


class TestMoneyPaiseMigration:
    """Tests for 0005_money_paise conversion, paise conservation, downgrade, and legacy sqlite_autoindex_* user rebuild."""

    def test_0005_converts_money_to_bigint_paise_conserves_totals_and_downgrades(self, tmp_path):
        from datetime import date as py_date
        from alembic.autogenerate import compare_metadata
        from alembic.migration import MigrationContext
        from scripts.verify_migration import audit_database, compare_reports

        uri, is_postgres = _get_test_uri(tmp_path, 'paise_mig.db')
        cfg = _alembic_config(uri)
        engine = sa.create_engine(uri)
        try:
            if is_postgres:
                with engine.begin() as conn:
                    conn.execute(sa.text('DROP SCHEMA public CASCADE; CREATE SCHEMA public;'))

            command.upgrade(cfg, '0004_expense_ordering_index')
            with engine.begin() as conn:
                conn.execute(sa.text(
                    """INSERT INTO "user" (id, username, email, password_hash, recurring_budget, registration_date, avatar)
                       VALUES
                       (1, 'paise1', 'paise1@example.com', 'h1', 3500.75, CURRENT_TIMESTAMP, 'avatar_1'),
                       (2, 'paise2', 'paise2@example.com', 'h2', NULL, CURRENT_TIMESTAMP, 'avatar_2')"""
                ))
                conn.execute(sa.text(
                    """INSERT INTO expense (id, user_id, date, category, amount, payment_method, created_at)
                       VALUES
                       (1, 1, :d1, 'Food', :a1, 'UPI', CURRENT_TIMESTAMP),
                       (2, 1, :d2, 'Food', :a2, 'Cash', CURRENT_TIMESTAMP),
                       (3, 1, :d2, 'Shopping', :a3, 'Card', CURRENT_TIMESTAMP),
                       (4, 2, :d3, 'Bills', :a4, 'NetBanking', CURRENT_TIMESTAMP)"""
                ), {
                    'd1': py_date(2024, 2, 29) if is_postgres else '2024-02-29',
                    'd2': py_date(2026, 1, 1) if is_postgres else '2026-01-01',
                    'd3': py_date(2026, 12, 31) if is_postgres else '2026-12-31',
                    'a1': 0.1 + 0.2,
                    'a2': 1.005,
                    'a3': 19.99,
                    'a4': 9999999.99,
                })
                conn.execute(sa.text(
                    """INSERT INTO budget (id, user_id, year_month, amount)
                       VALUES
                       (1, 1, '2026-01', 10000.50),
                       (2, 2, '2026-12', 100000000.0)"""
                ))
                if is_postgres:
                    conn.execute(sa.text("""SELECT setval(pg_get_serial_sequence('"user"', 'id'), 2)"""))
                    conn.execute(sa.text("""SELECT setval(pg_get_serial_sequence('expense', 'id'), 4)"""))
                    conn.execute(sa.text("""SELECT setval(pg_get_serial_sequence('budget', 'id'), 2)"""))

            before = audit_database(uri)
            assert before['total_expense_paise'] == 30 + 101 + 1999 + 999_999_999
            assert before['total_budget_paise'] == 1_000_050 + 10_000_000_000
            assert before['total_recurring_budget_paise'] == 350_075

            command.upgrade(cfg, '0005_money_paise')
            insp = sa.inspect(engine)

            user_cols = {c['name']: c for c in insp.get_columns('user')}
            exp_cols = {c['name']: c for c in insp.get_columns('expense')}
            bud_cols = {c['name']: c for c in insp.get_columns('budget')}

            assert 'recurring_budget' not in user_cols
            assert isinstance(user_cols['recurring_budget_paise']['type'], sa.BigInteger)
            assert user_cols['recurring_budget_paise']['nullable'] is True

            assert 'amount' not in exp_cols
            assert isinstance(exp_cols['amount_paise']['type'], sa.BigInteger)
            assert exp_cols['amount_paise']['nullable'] is False
            assert isinstance(exp_cols['date']['type'], sa.Date)

            assert 'amount' not in bud_cols
            assert isinstance(bud_cols['amount_paise']['type'], sa.BigInteger)
            assert bud_cols['amount_paise']['nullable'] is False

            assert {'ix_expense_user_id', 'ix_expense_date', 'idx_expense_user_date_id'}.issubset(
                {i['name'] for i in insp.get_indexes('expense')}
            )
            assert insp.get_foreign_keys('expense')[0]['referred_table'] == 'user'
            assert insp.get_foreign_keys('budget')[0]['referred_table'] == 'user'

            with engine.connect() as conn:
                assert conn.execute(sa.text('SELECT amount_paise FROM expense ORDER BY id ASC')).scalars().all() == [
                    30, 101, 1999, 999_999_999
                ]
                assert conn.execute(sa.text('SELECT amount_paise FROM budget ORDER BY id ASC')).scalars().all() == [
                    1_000_050, 10_000_000_000
                ]
                assert conn.execute(sa.text('SELECT recurring_budget_paise FROM "user" ORDER BY id ASC')).scalars().all() == [
                    350_075, None
                ]
                if not is_postgres:
                    assert conn.execute(sa.text('SELECT typeof(date) FROM expense WHERE id = 1')).scalar_one() == 'text'
                    assert conn.execute(sa.text('SELECT date FROM expense WHERE id = 1')).scalar_one() == '2024-02-29'

            after_up = audit_database(uri)
            assert compare_reports(before, after_up) == []

            command.downgrade(cfg, '0004_expense_ordering_index')
            insp_down = sa.inspect(engine)
            assert 'amount' in {c['name'] for c in insp_down.get_columns('expense')}
            assert 'amount_paise' not in {c['name'] for c in insp_down.get_columns('expense')}
            assert 'amount' in {c['name'] for c in insp_down.get_columns('budget')}
            assert 'recurring_budget' in {c['name'] for c in insp_down.get_columns('user')}

            after_down = audit_database(uri)
            assert compare_reports(before, after_down) == []

            command.upgrade(cfg, '0005_money_paise')
            assert compare_reports(before, audit_database(uri)) == []
            command.upgrade(cfg, 'head')
            with engine.connect() as conn:
                ctx = MigrationContext.configure(
                    conn, opts={'compare_type': True, 'compare_server_default': True}
                )
                assert compare_metadata(ctx, db.metadata) == []
        finally:
            if is_postgres:
                with engine.begin() as conn:
                    conn.execute(sa.text('DROP SCHEMA public CASCADE; CREATE SCHEMA public;'))
            engine.dispose()

    def test_0005_aborts_when_non_positive_or_non_finite_amount_present(self, tmp_path):
        db_file = tmp_path / 'bad_amt_abort.db'
        uri = f"sqlite:///{db_file.as_posix()}"
        cfg = _alembic_config(uri)
        command.upgrade(cfg, '0004_expense_ordering_index')

        engine = sa.create_engine(uri)
        try:
            with engine.begin() as conn:
                conn.execute(sa.text(
                    """INSERT INTO "user" (id, username, email, password_hash, registration_date, avatar)
                       VALUES (1, 'badamt', 'badamt@example.com', 'h', CURRENT_TIMESTAMP, 'avatar_1')"""
                ))
                conn.execute(sa.text(
                    """INSERT INTO expense (id, user_id, date, category, amount, payment_method, created_at)
                       VALUES (1, 1, '2026-07-09', 'Food', 0.0, 'Cash', CURRENT_TIMESTAMP)"""
                ))

            with pytest.raises(ValueError, match='invalid'):
                command.upgrade(cfg, '0005_money_paise')

            with engine.connect() as conn:
                ver = conn.execute(sa.text('SELECT version_num FROM alembic_version')).scalar_one()
                assert ver == '0004_expense_ordering_index'
                assert 'amount' in {c['name'] for c in sa.inspect(conn).get_columns('expense')}
        finally:
            engine.dispose()

    def test_0005_legacy_stage1a_sqlite_autoindex_user_batch_alter_compatibility(self, tmp_path):
        """
        Upgrading a legacy Stage-1A SQLite DB with sqlite_autoindex_* UNIQUE constraints
        (both table-level UNIQUE and inline column UNIQUE, with or without avatar default)
        through 0005 batch_alter_table('user') must preserve uniqueness, FKs, and paise totals.
        """
        from scripts.verify_migration import audit_database, compare_reports

        # Case 1: Table-level UNIQUE(username), UNIQUE(email) -> sqlite_autoindex_user_1/2
        db1 = tmp_path / 'legacy_autoindex_table_uq.db'
        conn1 = sqlite3.connect(db1)
        try:
            conn1.executescript(
                """
                CREATE TABLE user (
                    id INTEGER NOT NULL,
                    username VARCHAR(80) NOT NULL,
                    email VARCHAR(120) NOT NULL,
                    password_hash VARCHAR(256) NOT NULL,
                    recurring_budget FLOAT,
                    registration_date DATETIME,
                    avatar VARCHAR(50),
                    display_name VARCHAR(80),
                    PRIMARY KEY (id),
                    UNIQUE (username),
                    UNIQUE (email)
                );
                CREATE TABLE expense (
                    id INTEGER NOT NULL PRIMARY KEY,
                    user_id INTEGER NOT NULL REFERENCES user(id),
                    date VARCHAR(10) NOT NULL,
                    category VARCHAR(50) NOT NULL,
                    amount FLOAT NOT NULL,
                    description VARCHAR(255),
                    payment_method VARCHAR(50),
                    created_at DATETIME
                );
                CREATE TABLE budget (
                    id INTEGER NOT NULL PRIMARY KEY,
                    user_id INTEGER NOT NULL REFERENCES user(id),
                    year_month VARCHAR(7) NOT NULL,
                    amount FLOAT NOT NULL,
                    CONSTRAINT unique_user_month_budget UNIQUE (user_id, year_month)
                );
                INSERT INTO user (id, username, email, password_hash, recurring_budget, registration_date, avatar)
                VALUES (1, 'Alice', 'alice@example.com', 'h1', 4250.5, '2026-01-01 00:00:00', 'avatar_1');
                INSERT INTO expense (id, user_id, date, category, amount, payment_method, created_at)
                VALUES (1, 1, '2024-02-29', 'Food', 149.5, 'UPI', '2024-02-29 10:00:00');
                INSERT INTO budget (id, user_id, year_month, amount)
                VALUES (1, 1, '2026-11', 5000.0);
                """
            )
            conn1.commit()
        finally:
            conn1.close()

        uri1 = f"sqlite:///{db1.as_posix()}"
        cfg1 = _alembic_config(uri1)
        before1 = audit_database(uri1)

        command.stamp(cfg1, '0001_baseline')
        command.upgrade(cfg1, '0005_money_paise')

        eng1 = sa.create_engine(uri1)
        try:
            insp1 = sa.inspect(eng1)
            assert insp1.get_indexes('user') == []
            assert 'recurring_budget_paise' in {c['name'] for c in insp1.get_columns('user')}
            assert 'amount_paise' in {c['name'] for c in insp1.get_columns('expense')}
            assert 'amount_paise' in {c['name'] for c in insp1.get_columns('budget')}
            assert {tuple(u['column_names']) for u in insp1.get_unique_constraints('user')} == {
                ('username',),
                ('email',),
            }
            assert compare_reports(before1, audit_database(uri1)) == []

            with eng1.connect() as sa_conn:
                autoindexes = [
                    r[0]
                    for r in sa_conn.execute(
                        sa.text("SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='user' AND name LIKE 'sqlite_autoindex_%'")
                    )
                ]
                assert len(autoindexes) == 2
                assert list(sa_conn.execute(sa.text('PRAGMA foreign_key_check'))) == []

            # Duplicate username / email must still fail via preserved UNIQUE autoindexes
            with pytest.raises(sa.exc.IntegrityError):
                with eng1.begin() as sa_conn:
                    sa_conn.execute(sa.text(
                        """INSERT INTO "user" (username, email, password_hash)
                           VALUES ('Alice', 'unique2@example.com', 'h2')"""
                    ))
            with pytest.raises(sa.exc.IntegrityError):
                with eng1.begin() as sa_conn:
                    sa_conn.execute(sa.text(
                        """INSERT INTO "user" (username, email, password_hash)
                           VALUES ('UniqueUser2', 'alice@example.com', 'h2')"""
                    ))

            command.downgrade(cfg1, '0004_expense_ordering_index')
            assert compare_reports(before1, audit_database(uri1)) == []
            command.upgrade(cfg1, '0005_money_paise')
            assert compare_reports(before1, audit_database(uri1)) == []
            command.upgrade(cfg1, 'head')
            assert TestSchemaReconciliation._unexplained_diffs(eng1) == []
            assert compare_reports(before1, audit_database(uri1)) == []
        finally:
            eng1.dispose()

        # Case 2: Inline column UNIQUE without ix_user_* indexes
        db2 = tmp_path / 'legacy_autoindex_inline_uq.db'
        conn2 = sqlite3.connect(db2)
        try:
            conn2.executescript(
                """
                CREATE TABLE user (
                    id INTEGER NOT NULL PRIMARY KEY,
                    username VARCHAR(80) NOT NULL UNIQUE,
                    email VARCHAR(120) NOT NULL UNIQUE,
                    password_hash VARCHAR(256) NOT NULL,
                    recurring_budget FLOAT,
                    registration_date DATETIME,
                    avatar VARCHAR(50),
                    display_name VARCHAR(80)
                );
                CREATE TABLE expense (
                    id INTEGER NOT NULL PRIMARY KEY,
                    user_id INTEGER NOT NULL REFERENCES user(id),
                    date VARCHAR(10) NOT NULL,
                    category VARCHAR(50) NOT NULL,
                    amount FLOAT NOT NULL,
                    description VARCHAR(255),
                    payment_method VARCHAR(50),
                    created_at DATETIME
                );
                CREATE TABLE budget (
                    id INTEGER NOT NULL PRIMARY KEY,
                    user_id INTEGER NOT NULL REFERENCES user(id),
                    year_month VARCHAR(7) NOT NULL,
                    amount FLOAT NOT NULL,
                    CONSTRAINT unique_user_month_budget UNIQUE (user_id, year_month)
                );
                INSERT INTO user (id, username, email, password_hash, recurring_budget, registration_date, avatar)
                VALUES (1, 'Bob', 'bob@example.com', 'h1', 2500.25, '2026-01-01 00:00:00', 'avatar_1');
                INSERT INTO expense (id, user_id, date, category, amount, payment_method, created_at)
                VALUES (1, 1, '2026-07-09', 'Food', 19.99, 'Cash', '2026-07-09 10:00:00');
                INSERT INTO budget (id, user_id, year_month, amount)
                VALUES (1, 1, '2026-07', 4000.0);
                """
            )
            conn2.commit()
        finally:
            conn2.close()

        uri2 = f"sqlite:///{db2.as_posix()}"
        cfg2 = _alembic_config(uri2)
        before2 = audit_database(uri2)
        command.stamp(cfg2, '0001_baseline')
        command.upgrade(cfg2, '0005_money_paise')

        eng2 = sa.create_engine(uri2)
        try:
            assert compare_reports(before2, audit_database(uri2)) == []
            with pytest.raises(sa.exc.IntegrityError):
                with eng2.begin() as sa_conn:
                    sa_conn.execute(sa.text(
                        """INSERT INTO "user" (username, email, password_hash)
                           VALUES ('Bob', 'other@example.com', 'h2')"""
                    ))
            with pytest.raises(sa.exc.IntegrityError):
                with eng2.begin() as sa_conn:
                    sa_conn.execute(sa.text(
                        """INSERT INTO "user" (username, email, password_hash)
                           VALUES ('Other', 'bob@example.com', 'h2')"""
                    ))
            command.upgrade(cfg2, 'head')
            assert TestSchemaReconciliation._unexplained_diffs(eng2) == []
            assert compare_reports(before2, audit_database(uri2)) == []
        finally:
            eng2.dispose()


class TestM5ConstraintsAndCaseIndexes:
    """Tests for M5 CHECK, NOT NULL, ON DELETE CASCADE, and functional lower() unique indexes."""

    def test_0006_upgrade_and_downgrade_across_fresh_sqlite_and_postgres(self, tmp_path):
        from datetime import date as py_date
        from alembic.autogenerate import compare_metadata
        from alembic.migration import MigrationContext
        from scripts.verify_migration import audit_database, compare_reports

        uri, is_postgres = _get_test_uri(tmp_path, 'm5_fresh.db')
        cfg = _alembic_config(uri)
        engine = sa.create_engine(uri)
        try:
            if is_postgres:
                with engine.begin() as conn:
                    conn.execute(sa.text('DROP SCHEMA public CASCADE; CREATE SCHEMA public;'))

            command.upgrade(cfg, '0005_money_paise')
            with engine.begin() as conn:
                conn.execute(sa.text(
                    """INSERT INTO "user" (id, username, email, password_hash, recurring_budget_paise, registration_date, avatar)
                       VALUES
                       (1, 'Alice', 'Alice@Example.com', 'h1', 300000, NULL, NULL),
                       (2, 'Bob', 'Bob@Example.com', 'h2', NULL, CURRENT_TIMESTAMP, 'avatar_2')"""
                ))
                conn.execute(sa.text(
                    """INSERT INTO expense (id, user_id, date, category, amount_paise, payment_method, created_at)
                       VALUES (1, 1, :d1, 'Food', 14950, NULL, NULL)"""
                ), {'d1': py_date(2026, 7, 9) if is_postgres else '2026-07-09'})
                conn.execute(sa.text(
                    """INSERT INTO budget (id, user_id, year_month, amount_paise)
                       VALUES (1, 1, '2026-07', 500000)"""
                ))
                if is_postgres:
                    conn.execute(sa.text("""SELECT setval(pg_get_serial_sequence('"user"', 'id'), 2)"""))
                    conn.execute(sa.text("""SELECT setval(pg_get_serial_sequence('expense', 'id'), 1)"""))
                    conn.execute(sa.text("""SELECT setval(pg_get_serial_sequence('budget', 'id'), 1)"""))

            before = audit_database(uri)
            command.upgrade(cfg, '0006_constraints_case_indexes')

            insp = sa.inspect(engine)
            user_cols = {c['name']: c for c in insp.get_columns('user')}
            exp_cols = {c['name']: c for c in insp.get_columns('expense')}
            assert user_cols['registration_date']['nullable'] is False
            assert user_cols['avatar']['nullable'] is False
            assert exp_cols['payment_method']['nullable'] is False
            assert exp_cols['created_at']['nullable'] is False

            assert insp.get_foreign_keys('expense')[0]['options'].get('ondelete') == 'CASCADE'
            assert insp.get_foreign_keys('budget')[0]['options'].get('ondelete') == 'CASCADE'

            user_cks = {c['name'] for c in insp.get_check_constraints('user')}
            exp_cks = {c['name'] for c in insp.get_check_constraints('expense')}
            bud_cks = {c['name'] for c in insp.get_check_constraints('budget')}
            assert 'ck_user_recurring_budget_paise_positive' in user_cks
            assert 'ck_expense_amount_paise_positive' in exp_cks
            assert 'ck_budget_amount_paise_positive' in bud_cks

            with engine.connect() as conn:
                if not is_postgres:
                    idx_names = {
                        r[0]
                        for r in conn.execute(
                            sa.text("SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='user'")
                        )
                    }
                    assert {'ix_user_username_lower', 'ix_user_email_lower'} <= idx_names

            after_up = audit_database(uri)
            assert compare_reports(before, after_up) == []
            assert all(v == 0 for v in after_up['planned_not_null_nulls'].values())

            # Functional unique indexes reject case-insensitive duplicates at DB level
            with pytest.raises(sa.exc.IntegrityError):
                with engine.begin() as conn:
                    conn.execute(sa.text(
                        """INSERT INTO "user" (username, email, password_hash, registration_date, avatar)
                           VALUES ('alice', 'alice2@example.com', 'h3', CURRENT_TIMESTAMP, 'avatar_1')"""
                    ))
            with pytest.raises(sa.exc.IntegrityError):
                with engine.begin() as conn:
                    conn.execute(sa.text(
                        """INSERT INTO "user" (username, email, password_hash, registration_date, avatar)
                           VALUES ('alice2', 'alice@example.com', 'h3', CURRENT_TIMESTAMP, 'avatar_1')"""
                    ))

            # CHECK constraint rejects non-positive amounts at DB level
            with pytest.raises(sa.exc.IntegrityError):
                with engine.begin() as conn:
                    conn.execute(sa.text(
                        """INSERT INTO expense (user_id, date, category, amount_paise, payment_method, created_at)
                           VALUES (1, :d1, 'Food', 0, 'Cash', CURRENT_TIMESTAMP)"""
                    ), {'d1': py_date(2026, 7, 9) if is_postgres else '2026-07-09'})

            # ON DELETE CASCADE removes child expenses & budgets when user row is deleted
            with engine.begin() as conn:
                if not is_postgres:
                    conn.execute(sa.text('PRAGMA foreign_keys=ON'))
                conn.execute(sa.text('DELETE FROM "user" WHERE id = 1'))
                assert conn.execute(sa.text('SELECT COUNT(*) FROM expense WHERE user_id = 1')).scalar_one() == 0
                assert conn.execute(sa.text('SELECT COUNT(*) FROM budget WHERE user_id = 1')).scalar_one() == 0

            # Downgrade back to 0005 restores prior constraints cleanly
            command.downgrade(cfg, '0005_money_paise')
            insp_down = sa.inspect(engine)
            assert {c['name']: c for c in insp_down.get_columns('user')}['registration_date']['nullable'] is True
            assert {c['name']: c for c in insp_down.get_columns('expense')}['payment_method']['nullable'] is True
            assert not insp_down.get_foreign_keys('expense')[0]['options'].get('ondelete')
            assert {i['name'] for i in insp_down.get_indexes('user')} >= {'ix_user_username', 'ix_user_email'}

            command.upgrade(cfg, '0006_constraints_case_indexes')
            command.upgrade(cfg, 'head')
            with engine.connect() as conn:
                ctx = MigrationContext.configure(
                    conn, opts={'compare_type': True, 'compare_server_default': True}
                )
                assert compare_metadata(ctx, db.metadata) == []
        finally:
            if is_postgres:
                with engine.begin() as conn:
                    conn.execute(sa.text('DROP SCHEMA public CASCADE; CREATE SCHEMA public;'))
            engine.dispose()

    def test_0006_legacy_stage1a_sqlite_autoindex_migrates_to_functional_lower_indexes(self, tmp_path):
        from scripts.verify_migration import audit_database, compare_reports

        db_file = tmp_path / 'legacy_m5_autoindex.db'
        TestSchemaReconciliation._create_legacy_stage1a_db(db_file, seed_data=True)
        uri = f"sqlite:///{db_file.as_posix()}"
        cfg = _alembic_config(uri)
        before = audit_database(uri)

        command.stamp(cfg, '0001_baseline')
        command.upgrade(cfg, '0006_constraints_case_indexes')

        engine = sa.create_engine(uri)
        try:
            insp = sa.inspect(engine)
            assert insp.get_unique_constraints('user') == []
            assert compare_reports(before, audit_database(uri)) == []

            with engine.connect() as conn:
                idx_names = {
                    r[0]
                    for r in conn.execute(
                        sa.text("SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='user'")
                    )
                }
                assert idx_names == {'ix_user_username_lower', 'ix_user_email_lower'}
                assert list(conn.execute(sa.text('PRAGMA foreign_key_check'))) == []

            with pytest.raises(sa.exc.IntegrityError):
                with engine.begin() as conn:
                    conn.execute(sa.text(
                        """INSERT INTO "user" (username, email, password_hash, registration_date, avatar)
                           VALUES ('alice', 'unique@example.com', 'h2', CURRENT_TIMESTAMP, 'avatar_1')"""
                    ))

            command.downgrade(cfg, '0005_money_paise')
            command.upgrade(cfg, '0006_constraints_case_indexes')
            command.upgrade(cfg, 'head')
            assert TestSchemaReconciliation._unexplained_diffs(engine) == []
            assert compare_reports(before, audit_database(uri)) == []
        finally:
            engine.dispose()

    @pytest.mark.parametrize(
        'case_kind',
        ['username_collision', 'email_collision', 'non_positive_expense', 'non_positive_recurring', 'orphan_fk'],
    )
    def test_0006_aborts_on_blocking_data_violations(self, tmp_path, case_kind):
        db_file = tmp_path / f'abort_{case_kind}.db'
        uri = f"sqlite:///{db_file.as_posix()}"
        cfg = _alembic_config(uri)
        command.upgrade(cfg, '0005_money_paise')

        engine = sa.create_engine(uri)
        try:
            with engine.begin() as conn:
                conn.execute(sa.text(
                    """INSERT INTO "user" (id, username, email, password_hash, recurring_budget_paise, registration_date, avatar)
                       VALUES (1, 'Alice', 'alice@example.com', 'h1', 300000, CURRENT_TIMESTAMP, 'avatar_1')"""
                ))
                if case_kind == 'username_collision':
                    conn.execute(sa.text(
                        """INSERT INTO "user" (id, username, email, password_hash, registration_date, avatar)
                           VALUES (2, 'alice', 'alice2@example.com', 'h2', CURRENT_TIMESTAMP, 'avatar_1')"""
                    ))
                elif case_kind == 'email_collision':
                    conn.execute(sa.text(
                        """INSERT INTO "user" (id, username, email, password_hash, registration_date, avatar)
                           VALUES (2, 'bob', 'ALICE@example.com', 'h2', CURRENT_TIMESTAMP, 'avatar_1')"""
                    ))
                elif case_kind == 'non_positive_expense':
                    conn.execute(sa.text(
                        """INSERT INTO expense (id, user_id, date, category, amount_paise, payment_method, created_at)
                           VALUES (1, 1, '2026-07-09', 'Food', 0, 'Cash', CURRENT_TIMESTAMP)"""
                    ))
                elif case_kind == 'non_positive_recurring':
                    conn.execute(sa.text(
                        'UPDATE "user" SET recurring_budget_paise = -100 WHERE id = 1'
                    ))
                elif case_kind == 'orphan_fk':
                    conn.execute(sa.text(
                        """INSERT INTO expense (id, user_id, date, category, amount_paise, payment_method, created_at)
                           VALUES (1, 9999, '2026-07-09', 'Food', 1000, 'Cash', CURRENT_TIMESTAMP)"""
                    ))

            with pytest.raises(ValueError, match='Cannot'):
                command.upgrade(cfg, '0006_constraints_case_indexes')

            with engine.connect() as conn:
                ver = conn.execute(sa.text('SELECT version_num FROM alembic_version')).scalar_one()
                assert ver == '0005_money_paise'
        finally:
            engine.dispose()


class TestStatementImportMigration:
    """Tests for 0007_statement_import schema upgrade, downgrade, NULL-fingerprint semantics, and CASCADE."""

    def test_0007_upgrade_and_downgrade_across_sqlite_and_postgres(self, tmp_path):
        from datetime import date as py_date
        from alembic.autogenerate import compare_metadata
        from alembic.migration import MigrationContext

        uri, is_postgres = _get_test_uri(tmp_path, 'm7_import.db')
        cfg = _alembic_config(uri)
        engine = sa.create_engine(uri)
        try:
            if is_postgres:
                with engine.begin() as conn:
                    conn.execute(sa.text('DROP SCHEMA public CASCADE; CREATE SCHEMA public;'))

            command.upgrade(cfg, '0006_constraints_case_indexes')
            with engine.begin() as conn:
                conn.execute(sa.text(
                    """INSERT INTO "user" (id, username, email, password_hash, registration_date, avatar)
                       VALUES (1, 'impuser', 'imp@example.com', 'h1', CURRENT_TIMESTAMP, 'avatar_1')"""
                ))
                # Two manual expenses with NULL import_fingerprint must remain valid
                conn.execute(sa.text(
                    """INSERT INTO expense (user_id, date, category, amount_paise, payment_method, created_at)
                       VALUES
                       (1, :d1, 'Food', 1000, 'Cash', CURRENT_TIMESTAMP),
                       (1, :d1, 'Food', 2000, 'UPI', CURRENT_TIMESTAMP)"""
                ), {'d1': py_date(2026, 10, 1) if is_postgres else '2026-10-01'})
                if is_postgres:
                    conn.execute(sa.text("""SELECT setval(pg_get_serial_sequence('"user"', 'id'), 1)"""))
                    conn.execute(sa.text("""SELECT setval(pg_get_serial_sequence('expense', 'id'), 2)"""))

            command.upgrade(cfg, '0007_statement_import')
            insp = sa.inspect(engine)
            assert {'import_batch', 'import_row', 'category_rule'}.issubset(set(insp.get_table_names()))
            exp_cols = {c['name'] for c in insp.get_columns('expense')}
            assert {'import_batch_id', 'import_fingerprint'}.issubset(exp_cols)
            assert 'uq_expense_user_import_fingerprint' in {u['name'] for u in insp.get_unique_constraints('expense')}
            assert 'uq_category_rule_user_pattern' in {u['name'] for u in insp.get_unique_constraints('category_rule')}

            # Duplicate non-NULL import_fingerprint for same user is rejected at DB level
            fp = 'a' * 64
            with engine.begin() as conn:
                conn.execute(sa.text(
                    """INSERT INTO expense (user_id, date, category, amount_paise, payment_method, created_at, import_fingerprint)
                       VALUES (1, :d1, 'Food', 500, 'UPI', CURRENT_TIMESTAMP, :fp)"""
                ), {'d1': py_date(2026, 10, 2) if is_postgres else '2026-10-02', 'fp': fp})

            with pytest.raises(sa.exc.IntegrityError):
                with engine.begin() as conn:
                    conn.execute(sa.text(
                        """INSERT INTO expense (user_id, date, category, amount_paise, payment_method, created_at, import_fingerprint)
                           VALUES (1, :d1, 'Food', 500, 'UPI', CURRENT_TIMESTAMP, :fp)"""
                    ), {'d1': py_date(2026, 10, 2) if is_postgres else '2026-10-02', 'fp': fp})

            # Another manual NULL-fingerprint expense still succeeds alongside imported ones
            with engine.begin() as conn:
                conn.execute(sa.text(
                    """INSERT INTO expense (user_id, date, category, amount_paise, payment_method, created_at)
                       VALUES (1, :d1, 'Food', 750, 'Cash', CURRENT_TIMESTAMP)"""
                ), {'d1': py_date(2026, 10, 3) if is_postgres else '2026-10-03'})

            # Downgrade removes 0007 tables/columns while preserving all expenses
            command.downgrade(cfg, '0006_constraints_case_indexes')
            insp_down = sa.inspect(engine)
            assert not ({'import_batch', 'import_row', 'category_rule'} & set(insp_down.get_table_names()))
            assert 'import_fingerprint' not in {c['name'] for c in insp_down.get_columns('expense')}
            with engine.connect() as conn:
                assert conn.execute(sa.text('SELECT COUNT(*) FROM expense')).scalar_one() == 4

            command.upgrade(cfg, 'head')
            with engine.connect() as conn:
                ctx = MigrationContext.configure(
                    conn, opts={'compare_type': True, 'compare_server_default': True}
                )
                diffs = compare_metadata(ctx, db.metadata)
                if not is_postgres:
                    diffs = [
                        d for d in diffs
                        if not (isinstance(d, tuple) and d[0] == 'add_index' and d[1].name in {'ix_user_username_lower', 'ix_user_email_lower'})
                    ]
                assert diffs == []
        finally:
            if is_postgres:
                with engine.begin() as conn:
                    conn.execute(sa.text('DROP SCHEMA public CASCADE; CREATE SCHEMA public;'))
            engine.dispose()

    def test_0007_on_delete_cascade_removes_import_data_when_user_deleted(self, app, user):
        from models import CategoryRule, ImportBatch, ImportRow

        with app.app_context():
            b = ImportBatch(
                user_id=user.id,
                bank_profile='signed_iso',
                original_filename='test.csv',
                file_sha256='0' * 64,
                detected_encoding='utf-8',
                status='staged',
            )
            db.session.add(b)
            db.session.flush()
            db.session.add(ImportRow(
                batch_id=b.id,
                user_id=user.id,
                line_number=2,
                row_status='ready',
                amount_paise=1000,
            ))
            db.session.add(CategoryRule(
                user_id=user.id,
                pattern='TEST',
                pattern_normalized='TEST',
                category='Food',
            ))
            db.session.commit()

            db.session.execute(sa.text('DELETE FROM "user" WHERE id = :uid'), {'uid': user.id})
            db.session.commit()

            assert ImportBatch.query.filter_by(user_id=user.id).count() == 0
            assert ImportRow.query.filter_by(user_id=user.id).count() == 0
            assert CategoryRule.query.filter_by(user_id=user.id).count() == 0


class TestMultiformatBalanceMigration:
    """Tests for 0008_multiformat_balance upgrade, CHECK constraints, downgrade, and metadata parity."""

    def test_0008_upgrade_and_downgrade_across_sqlite_and_postgres(self, tmp_path):
        from alembic.autogenerate import compare_metadata
        from alembic.migration import MigrationContext

        uri, is_postgres = _get_test_uri(tmp_path, 'm8_balance.db')
        cfg = _alembic_config(uri)
        engine = sa.create_engine(uri)
        try:
            if is_postgres:
                with engine.begin() as conn:
                    conn.execute(sa.text('DROP SCHEMA public CASCADE; CREATE SCHEMA public;'))

            command.upgrade(cfg, '0007_statement_import')
            with engine.begin() as conn:
                conn.execute(sa.text(
                    """INSERT INTO "user" (id, username, email, password_hash, registration_date, avatar)
                       VALUES (1, 'baluser', 'bal@example.com', 'h1', CURRENT_TIMESTAMP, 'avatar_1')"""
                ))
                conn.execute(sa.text(
                    """INSERT INTO import_batch (
                           id, user_id, bank_profile, original_filename, file_sha256, detected_encoding,
                           status, total_rows, ready_count, duplicate_manual_count, already_imported_count,
                           skipped_credit_count, invalid_count, imported_count, skipped_count, created_at
                       ) VALUES (
                           1, 1, 'signed_iso', 'prior.csv', '0000000000000000000000000000000000000000000000000000000000000000',
                           'utf-8', 'staged', 1, 1, 0, 0, 0, 0, 0, 0, CURRENT_TIMESTAMP
                       )"""
                ))
                conn.execute(sa.text(
                    """INSERT INTO import_row (
                           id, batch_id, user_id, line_number, row_status, raw_narration, description,
                           ref_no, category, payment_method, raw_preview, include_on_confirm
                       ) VALUES (
                           1, 1, 1, 2, 'ready', 'Sample', 'Sample', '', 'Uncategorised', 'UPI', 'Sample', :inc
                       )"""
                ), {'inc': True if is_postgres else 1})

            command.upgrade(cfg, '0008_multiformat_balance')
            insp = sa.inspect(engine)
            batch_cols = {c['name'] for c in insp.get_columns('import_batch')}
            row_cols = {c['name'] for c in insp.get_columns('import_row')}
            assert {'detected_format_label', 'balance_check_status', 'balance_mismatch_count'}.issubset(batch_cols)
            assert {'running_balance_paise', 'balance_mismatch_note'}.issubset(row_cols)

            with engine.connect() as conn:
                status, m_count = conn.execute(
                    sa.text('SELECT balance_check_status, balance_mismatch_count FROM import_batch WHERE id = 1')
                ).one()
                assert status == 'unavailable'
                assert m_count == 0

                ctx = MigrationContext.configure(
                    conn, opts={'compare_type': True, 'compare_server_default': True}
                )
                diffs = compare_metadata(ctx, db.metadata)
                if not is_postgres:
                    diffs = [
                        d for d in diffs
                        if not (isinstance(d, tuple) and d[0] == 'add_index' and d[1].name in {'ix_user_username_lower', 'ix_user_email_lower'})
                    ]
                assert diffs == []

            with pytest.raises(sa.exc.IntegrityError):
                with engine.begin() as conn:
                    conn.execute(sa.text(
                        """UPDATE import_batch SET balance_check_status = 'bogus' WHERE id = 1"""
                    ))

            command.downgrade(cfg, '0007_statement_import')
            insp_down = sa.inspect(engine)
            assert 'balance_check_status' not in {c['name'] for c in insp_down.get_columns('import_batch')}
            assert 'running_balance_paise' not in {c['name'] for c in insp_down.get_columns('import_row')}
            with engine.connect() as conn:
                assert conn.execute(sa.text('SELECT COUNT(*) FROM import_batch')).scalar_one() == 1
                assert conn.execute(sa.text('SELECT COUNT(*) FROM import_row')).scalar_one() == 1

            command.upgrade(cfg, 'head')
        finally:
            if is_postgres:
                with engine.begin() as conn:
                    conn.execute(sa.text('DROP SCHEMA public CASCADE; CREATE SCHEMA public;'))
            engine.dispose()
