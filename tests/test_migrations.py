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
            assert {'ix_expense_user_id', 'ix_expense_date', 'idx_expense_user_date'}.issubset(expense_indexes)

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
