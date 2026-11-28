"""
scripts/cleanup_staged_imports.py — Purge expired unconfirmed staged import batches and rows.

Designed to run safely as a PythonAnywhere Scheduled Task (or manual CLI job) without
requiring EXPENSEFLOW_SECRET_KEY or booting the WSGI application. Prints aggregate counts only
(never usernames, filenames, narrations, or passwords).

Usage:
    python scripts/cleanup_staged_imports.py
    python scripts/cleanup_staged_imports.py --ttl-hours 24 --dry-run
    python scripts/cleanup_staged_imports.py --database-uri sqlite:///instance/expenses.db
"""
from __future__ import annotations

import argparse
from datetime import datetime
import json
import os
from pathlib import Path
import sys

import sqlalchemy as sa
from sqlalchemy import event
from sqlalchemy.orm import Session

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from app import normalize_database_uri  # noqa: E402
from statement_import import STAGED_BATCH_TTL_HOURS, purge_expired_staged_batches  # noqa: E402


def _default_database_uri() -> str:
    env_uri = os.environ.get('EXPENSEFLOW_DATABASE_URI', '').strip()
    if env_uri:
        return env_uri
    return f"sqlite:///{(_REPO_ROOT / 'instance' / 'expenses.db').as_posix()}"


def cleanup_staged_imports(
    database_uri: str | None = None,
    *,
    ttl_hours: int = STAGED_BATCH_TTL_HOURS,
    now_utc: datetime | None = None,
    dry_run: bool = False,
) -> dict:
    """Purge staged batches older than ttl_hours and return count-only summary metrics."""
    if ttl_hours < 1:
        raise ValueError('ttl_hours must be at least 1.')

    raw_uri = (database_uri or '').strip() or _default_database_uri()
    normalized_uri = normalize_database_uri(raw_uri)
    engine = sa.create_engine(normalized_uri)
    if normalized_uri.startswith('sqlite'):
        @event.listens_for(engine, 'connect')
        def _set_sqlite_pragma(dbapi_connection, _connection_record):
            cursor = dbapi_connection.cursor()
            cursor.execute('PRAGMA foreign_keys=ON')
            cursor.close()

    try:
        with Session(engine) as session:
            purged_batches, purged_rows = purge_expired_staged_batches(
                session,
                ttl_hours=ttl_hours,
                now_utc=now_utc,
                dry_run=dry_run,
            )
            if not dry_run and purged_batches > 0:
                session.commit()
    finally:
        engine.dispose()

    return {
        'dry_run': bool(dry_run),
        'ttl_hours': int(ttl_hours),
        'purged_batches': int(purged_batches),
        'purged_rows': int(purged_rows),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description='Purge expired unconfirmed staged statement import batches and rows.'
    )
    parser.add_argument(
        '--database-uri',
        default=None,
        help='SQLAlchemy database URI (defaults to EXPENSEFLOW_DATABASE_URI or instance/expenses.db).',
    )
    parser.add_argument(
        '--ttl-hours',
        type=int,
        default=STAGED_BATCH_TTL_HOURS,
        help=f'Staged batch expiry age in hours (default: {STAGED_BATCH_TTL_HOURS}).',
    )
    parser.add_argument(
        '--dry-run',
        action='store_true',
        help='Report count of expired staged batches/rows without deleting anything.',
    )
    args = parser.parse_args(argv)

    summary = cleanup_staged_imports(
        args.database_uri,
        ttl_hours=args.ttl_hours,
        dry_run=args.dry_run,
    )
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
