# Stage 1B Learning Notes

## M1 — PostgreSQL + Docker Compose + Env-Based DB Config
- **What it is:** A two-container dev stack (`web` Flask/Gunicorn + `db` PostgreSQL 16) configured via `EXPENSEFLOW_DATABASE_URI`, with `psycopg[binary]` isolated in `requirements-postgres.txt` so PythonAnywhere's `requirements.txt` stays SQLite-only.
- **Why it exists:** Developing on local SQLite hides real Postgres constraints, connection drops, and query-plan differences. Normalizing `postgres://`/`postgresql://` URLs to `postgresql+psycopg://` with `pool_pre_ping=True` prevents stale-socket crashes, and routing tests through `EXPENSEFLOW_TEST_DATABASE_URI` ensures `pytest` never touches dev/prod data.
- **Interview Q:** *Why isn't `depends_on: [db]` alone enough before starting the Flask container?* Compose only waits for container startup, not PostgreSQL initialization; pairing `depends_on: condition: service_healthy` with a `pg_isready` healthcheck guarantees Postgres is accepting connections before Gunicorn boots.

## M2 — GitHub Actions CI on SQLite & PostgreSQL 16
- **What it is:** A dual-engine CI pipeline running the test matrix on SQLite (`3.10`–`3.13`) alongside an ephemeral `postgres:16-alpine` service container on every branch push and PR.
- **Why it exists:** SQLite silently accepts practices that fail in PostgreSQL (strict `VARCHAR` lengths, foreign keys, `ORDER BY` index behavior, case-sensitive `=`). Catching Postgres bugs on branch pushes stops schema regressions before they ever touch `main` or production.
- **Interview Q:** *What breaks first when moving a Flask test suite from SQLite to PostgreSQL in CI?* Unenforced foreign keys (`user_id` orphans), `db.session.rollback()` after `IntegrityError`, connection reuse across tests (`db.engine.dispose()`), and SQL syntax/index behavior differences (`startswith` vs date ranges).
