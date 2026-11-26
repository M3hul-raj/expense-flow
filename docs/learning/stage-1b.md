# Stage 1B Learning Notes

## M1 — PostgreSQL + Docker Compose + Env-Based DB Config
- **What it is:** A two-container dev stack (`web` Flask/Gunicorn + `db` PostgreSQL 16) configured via `EXPENSEFLOW_DATABASE_URI`, with `psycopg[binary]` isolated in `requirements-postgres.txt` so PythonAnywhere's `requirements.txt` stays SQLite-only.
- **Why it exists:** Developing on local SQLite hides real Postgres constraints, connection drops, and query-plan differences. Normalizing `postgres://`/`postgresql://` URLs to `postgresql+psycopg://` with `pool_pre_ping=True` prevents stale-socket crashes, and routing tests through `EXPENSEFLOW_TEST_DATABASE_URI` ensures `pytest` never touches dev/prod data.
- **Interview Q:** *Why isn't `depends_on: [db]` alone enough before starting the Flask container?* Compose only waits for container startup, not PostgreSQL initialization; pairing `depends_on: condition: service_healthy` with a `pg_isready` healthcheck guarantees Postgres is accepting connections before Gunicorn boots.

## M2 — GitHub Actions CI on SQLite & PostgreSQL 16
- **What it is:** A dual-engine CI pipeline running the test matrix on SQLite (`3.10`–`3.13`) alongside an ephemeral `postgres:16-alpine` service container on every branch push and PR.
- **Why it exists:** SQLite silently accepts practices that fail in PostgreSQL (strict `VARCHAR` lengths, foreign keys, `ORDER BY` index behavior, case-sensitive `=`). Catching Postgres bugs on branch pushes stops schema regressions before they ever touch `main` or production.
- **Interview Q:** *What breaks first when moving a Flask test suite from SQLite to PostgreSQL in CI?* Unenforced foreign keys (`user_id` orphans), `db.session.rollback()` after `IntegrityError`, connection reuse across tests (`db.engine.dispose()`), and SQL syntax/index behavior differences (`startswith` vs date ranges).

## M2B — Universal Dependency Lock Files (`uv`)
- **What it is:** Direct dependencies in `requirements.in` and `requirements-postgres.in`, compiled with `uv pip compile --universal --python-version 3.10` into exact `requirements.lock` and `requirements-postgres.lock` files.
- **Why it exists:** Pinning only direct packages lets Python `3.10` (PythonAnywhere) and `3.13` (local/Docker) pull incompatible transitive versions (e.g., SQLAlchemy `2.1.x` dropped Python `3.10`, while `uv` locks `2.0.58` for `<3.11` and `2.1.9` for `>=3.11`), while keeping `psycopg` completely out of PythonAnywhere installs.
- **Interview Q:** *Why choose `uv pip compile --universal` over `pip freeze` or plain `pip-compile`?* `pip freeze` dumps OS- and Python-specific packages (like dev tools or Windows `colorama`), and standard `pip-compile` locks only the host's Python/OS; `--universal` resolves one cross-OS, cross-version lock file using PEP 508 markers that `pip install` still reads natively.

## M3 — Alembic Baseline, Naming Convention & Controlled Rollout
- **What it is:** Version-controlled schema history (`alembic/` + `0001_baseline`), deterministic SQLAlchemy `NAMING_CONVENTION`, SQLite `render_as_batch=True`, removal of `db.create_all()` from `create_app()`, and explicit `--stamp REVISION` vs `--upgrade-to REVISION` deploy controls.
- **Why it exists:** `db.create_all()` only creates missing tables and never alters existing ones; Gunicorn workers booting on an empty DB also race if startup runs DDL. Naming conventions let SQLite batch rebuilds drop constraints cleanly, while stamping baselines an existing production DB without re-creating tables or running unverified upgrades.
- **Interview Q:** *Why does Alembic require explicit constraint naming conventions and `render_as_batch=True` for SQLite?* SQLite lacks full `ALTER TABLE` support for dropping constraints or altering column types, so Alembic recreates the table (`batch_alter_table`) and needs deterministic names to recreate/drop constraints safely without manual SQL edits.
