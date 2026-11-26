"""
test_migration_verification.py — synthetic nasty-data fixture generator and
pre/post migration verification tests for scripts/verify_migration.py.
"""
import os
from pathlib import Path
import sqlite3
import subprocess
import sys

import pytest
import sqlalchemy as sa

from models import db


REPO_ROOT = Path(__file__).resolve().parent.parent


def create_nasty_pre1b_sqlite_fixture(db_file: Path) -> None:
    """Create an intentionally dirty Stage-1A SQLite DB exercising every planned constraint & edge case."""
    conn = sqlite3.connect(db_file)
    try:
        conn.execute('PRAGMA foreign_keys=OFF')
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
            """
        )
        # Users: case collisions (Alice vs alice, bob@test.com vs Bob@Test.com), NULL avatar/registration_date, bad recurring_budget
        conn.executemany(
            "INSERT INTO user (id, username, email, password_hash, recurring_budget, registration_date, avatar) VALUES (?, ?, ?, ?, ?, ?, ?)",
            [
                (1, 'Alice', 'alice@example.com', 'h1', 5000.0, '2026-01-01 00:00:00', 'avatar_1'),
                (2, 'alice', 'Alice@Example.com', 'h2', 0.0, None, None),
                (3, 'Bob', 'Bob@Test.com', 'h3', -100.0, '2026-01-02 00:00:00', 'avatar_2'),
                (4, 'carol', 'bob@test.com', 'h4', None, '2026-01-03 00:00:00', None),
            ],
        )
        # Expenses: leap date valid + 5 unparseable dates + IEEE float + >2 decimals + amount<=0 + NULL created_at/payment_method + 1 orphan FK
        conn.executemany(
            "INSERT INTO expense (id, user_id, date, category, amount, description, payment_method, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (1, 1, '2024-02-29', 'Food', 0.1 + 0.2, 'SECRET_DESC_FLOAT', 'UPI', '2026-01-01 10:00:00'),
                (2, 1, '2026-02-30', 'Food', 12.345, 'SECRET_DESC_BAD_DATE_1', 'Cash', '2026-01-01 10:00:00'),
                (3, 1, '2026-1-5', 'Food', 0.0, 'SECRET_DESC_BAD_DATE_2', None, None),
                (4, 2, '2026/01/05', 'Food', -25.0, 'SECRET_DESC_BAD_DATE_3', 'Cash', '2026-01-01 10:00:00'),
                (5, 2, '٢٠٢٦-٠١-٠٥', 'Food', 19.99, 'SECRET_DESC_ARABIC_DATE', None, '2026-01-01 10:00:00'),
                (6, 3, 'not-a-date', 'Food', 50.0, 'SECRET_DESC_TEXT_DATE', 'Card', None),
                (7, 9999, '2026-07-09', 'Food', 10.0, 'SECRET_DESC_ORPHAN', 'Cash', '2026-01-01 10:00:00'),
            ],
        )
        # Budgets: 1 valid + 4 malformed year_month + 2 amount<=0 + 1 orphan FK
        conn.executemany(
            "INSERT INTO budget (id, user_id, year_month, amount) VALUES (?, ?, ?, ?)",
            [
                (1, 1, '2026-11', 5000.0),
                (2, 1, '2026-13', 0.0),
                (3, 2, '2026-00', -10.0),
                (4, 2, '2026-5', 2000.0),
                (5, 3, '٢٠٢٦-٠٥', 1500.0),
                (6, 9999, '2026-11', 1000.0),
            ],
        )
        conn.commit()
    finally:
        conn.close()


def create_clean_edge_case_pre1b_sqlite_fixture(db_file: Path) -> None:
    """Create a constraint-clean SQLite DB with tricky valid edge cases (float rounding, leap dates, duplicate same-day rows)."""
    conn = sqlite3.connect(db_file)
    try:
        conn.execute('PRAGMA foreign_keys=ON')
        conn.executescript(
            """
            CREATE TABLE user (
                id INTEGER NOT NULL PRIMARY KEY,
                username VARCHAR(80) NOT NULL UNIQUE,
                email VARCHAR(120) NOT NULL UNIQUE,
                password_hash VARCHAR(256) NOT NULL,
                recurring_budget FLOAT,
                registration_date DATETIME,
                avatar VARCHAR(50) DEFAULT 'avatar_1',
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
            """
        )
        conn.executemany(
            "INSERT INTO user (id, username, email, password_hash, recurring_budget, registration_date, avatar) VALUES (?, ?, ?, ?, ?, ?, ?)",
            [
                (1, 'alice', 'alice@example.com', 'h1', 5000.0, '2026-01-01 00:00:00', 'avatar_1'),
                (2, 'bob', 'bob@example.com', 'h2', None, '2026-01-02 00:00:00', 'avatar_2'),
            ],
        )
        conn.executemany(
            "INSERT INTO expense (id, user_id, date, category, amount, description, payment_method, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (1, 1, '2024-02-29', 'Food', 0.1 + 0.2, 'Leap coffee', 'UPI', '2024-02-29 10:00:00'),
                (2, 1, '2024-02-29', 'Food', 0.1 + 0.2, 'Identical same-day coffee', 'UPI', '2024-02-29 10:05:00'),
                (3, 1, '2026-01-01', 'Shopping', 19.99, 'Book', 'Card', '2026-01-01 09:00:00'),
                (4, 2, '2026-12-31', 'Bills', 9999999.99, 'Big bill', 'NetBanking', '2026-12-31 12:00:00'),
            ],
        )
        conn.executemany(
            "INSERT INTO budget (id, user_id, year_month, amount) VALUES (?, ?, ?, ?)",
            [
                (1, 1, '2026-01', 10000.50),
                (2, 2, '2026-12', 100000000.0),
            ],
        )
        conn.commit()
    finally:
        conn.close()


class TestMigrationVerificationScript:
    """Tests for scripts/verify_migration.py counts, constraints, privacy, and conservation checks."""

    def test_nasty_fixture_reports_exact_counts_and_never_prints_row_data(self, tmp_path):
        from scripts.verify_migration import audit_database

        db_file = tmp_path / 'nasty.db'
        create_nasty_pre1b_sqlite_fixture(db_file)
        uri = f"sqlite:///{db_file.as_posix()}"

        report = audit_database(uri)
        assert report['row_counts'] == {'user': 4, 'expense': 7, 'budget': 6}
        assert report['unparseable_dates'] == 5
        assert report['case_collisions'] == {'username': 1, 'email': 2}
        assert report['non_positive_amounts'] == {
            'expense': 2,
            'budget': 2,
            'recurring_budget': 2,
        }
        assert report['float_gt_2_decimals'] == 1
        assert report['malformed_year_month'] == 4
        assert report['planned_not_null_nulls'] == {
            'user.registration_date': 1,
            'user.avatar': 2,
            'expense.payment_method': 2,
            'expense.created_at': 2,
        }
        assert report['orphan_foreign_keys']['expense'] == 1
        assert report['orphan_foreign_keys']['budget'] == 1
        assert report['orphan_foreign_keys']['sqlite_fk_check'] == 2

        proc = subprocess.run(
            [sys.executable, str(REPO_ROOT / 'scripts' / 'verify_migration.py'), uri, '--strict'],
            capture_output=True,
            text=True,
        )
        assert proc.returncode != 0
        # Never print PII, emails, usernames, or expense descriptions
        combined_out = proc.stdout + proc.stderr
        for secret_sample in (
            'Alice',
            'alice@example.com',
            'Bob@Test.com',
            'SECRET_DESC_FLOAT',
            'SECRET_DESC_ORPHAN',
        ):
            assert secret_sample not in combined_out

    def test_clean_edge_cases_strict_passes_and_before_after_conserves_paise(self, tmp_path):
        from scripts.verify_migration import audit_database, compare_reports

        db_file = tmp_path / 'clean_edge.db'
        create_clean_edge_case_pre1b_sqlite_fixture(db_file)
        uri = f"sqlite:///{db_file.as_posix()}"

        before = audit_database(uri)
        assert before['row_counts'] == {'user': 2, 'expense': 4, 'budget': 2}
        assert before['unparseable_dates'] == 0
        assert before['case_collisions'] == {'username': 0, 'email': 0}
        assert before['non_positive_amounts'] == {'expense': 0, 'budget': 0, 'recurring_budget': 0}
        assert before['float_gt_2_decimals'] == 0
        assert before['malformed_year_month'] == 0
        assert before['planned_not_null_nulls'] == {
            'user.registration_date': 0,
            'user.avatar': 0,
            'expense.payment_method': 0,
            'expense.created_at': 0,
        }
        assert before['orphan_foreign_keys'] == {'expense': 0, 'budget': 0, 'sqlite_fk_check': 0}
        # Exact paise conservation: 30 + 30 + 1999 = 2059 paise for user 1; 999999999 for user 2
        assert before['per_user_expense_paise'] == {1: 2059, 2: 999999999}
        assert before['total_expense_paise'] == 1000002058
        assert before['per_user_budget_paise'] == {1: 1000050, 2: 10000000000}

        proc = subprocess.run(
            [sys.executable, str(REPO_ROOT / 'scripts' / 'verify_migration.py'), uri, '--strict', '--check-not-null'],
            capture_output=True,
            text=True,
        )
        assert proc.returncode == 0, proc.stderr

        # Matching report passes comparison
        mismatches = compare_reports(before, dict(before))
        assert mismatches == []

        # Simulated 1-paisa drift or dropped user triggers mismatch
        tampered = {
            **before,
            'per_user_expense_paise': {1: 2058, 2: 999999999},
            'total_expense_paise': 1000002057,
        }
        assert compare_reports(before, tampered)

    def test_audit_database_works_against_postgres_too(self, tmp_path):
        from alembic import command
        from alembic.config import Config
        from scripts.verify_migration import audit_database

        override = os.environ.get('EXPENSEFLOW_TEST_DATABASE_URI', '').strip()
        if not override:
            pytest.skip('EXPENSEFLOW_TEST_DATABASE_URI not set (Postgres audit check runs in Postgres/CI)')

        cfg = Config(str(REPO_ROOT / 'alembic.ini'))
        cfg.set_main_option('script_location', str(REPO_ROOT / 'alembic'))
        cfg.set_main_option('sqlalchemy.url', override)
        engine = sa.create_engine(override)
        try:
            with engine.begin() as conn:
                conn.execute(sa.text('DROP SCHEMA public CASCADE; CREATE SCHEMA public;'))
            command.upgrade(cfg, '0002_reconcile_prod_schema')
            with engine.begin() as conn:
                conn.execute(sa.text(
                    """INSERT INTO "user" (id, username, email, password_hash, registration_date, avatar)
                       VALUES (1, 'pguser', 'pg@example.com', 'h', CURRENT_TIMESTAMP, 'avatar_1')"""
                ))
                conn.execute(sa.text(
                    """INSERT INTO expense (id, user_id, date, category, amount, payment_method, created_at)
                       VALUES (1, 1, '2024-02-29', 'Food', 12.34, 'UPI', CURRENT_TIMESTAMP)"""
                ))
            rep = audit_database(override)
            assert rep['row_counts'] == {'user': 1, 'expense': 1, 'budget': 0}
            assert rep['unparseable_dates'] == 0
            assert rep['total_expense_paise'] == 1234
        finally:
            with engine.begin() as conn:
                conn.execute(sa.text('DROP SCHEMA public CASCADE; CREATE SCHEMA public;'))
            engine.dispose()
