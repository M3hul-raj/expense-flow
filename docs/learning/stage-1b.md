# Stage 1B Learning Notes

## M1 — PostgreSQL + Docker Compose + Env-Based DB Config
- **What it is:** A two-container dev stack (`web` Flask/Gunicorn + `db` PostgreSQL 16) configured via `EXPENSEFLOW_DATABASE_URI`, with `psycopg[binary]` isolated in `requirements-postgres.txt` so PythonAnywhere's `requirements.txt` stays SQLite-only.
- **Why it exists:** Developing on local SQLite hides real Postgres constraints, connection drops, and query-plan differences. Normalizing `postgres://`/`postgresql://` URLs to `postgresql+psycopg://` with `pool_pre_ping=True` prevents stale-socket crashes, and routing tests through `EXPENSEFLOW_TEST_DATABASE_URI` ensures `pytest` never touches dev/prod data.
- **Interview Q:** *Why isn't `depends_on: [db]` alone enough before starting the Flask container?* Compose only waits for container startup, not PostgreSQL initialization; pairing `depends_on: condition: service_healthy` with a `pg_isready` healthcheck guarantees Postgres is accepting connections before Gunicorn boots.
