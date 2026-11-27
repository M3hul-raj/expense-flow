# ExpenseFlow: Developer & Architecture Guide

Welcome to the internal documentation for **ExpenseFlow**. This guide is designed for developers, engineering managers, and technical recruiters to understand the underlying architecture, data flow, and design decisions of the application. 

If you are a developer looking to contribute or understand the codebase, this document serves as your complete "under the hood" manual.

---

## 1. Executive Architecture Summary

ExpenseFlow is a production-grade, full-stack web application built on a modern Python backend (Flask) with a custom, highly interactive vanilla JavaScript/CSS frontend. It uses the **application factory pattern** (`create_app()`) with **Flask Blueprints** and strictly implements the **MVC (Model-View-Controller)** pattern through server-side rendering (SSR):

*   **Model (`models.py` + `alembic/`)**: Defines the relational schema (`User`, `Expense`, `Budget`) using SQLAlchemy ORM and versioned Alembic migrations (`0001_baseline` through `0006_constraints_case_indexes`) across SQLite and PostgreSQL 16.
*   **View (`templates/`, `static/`)**: Jinja2 HTML templates styled with a completely custom CSS design system (`main.css`), featuring dynamic JavaScript interactivity without heavy frontend frameworks.
*   **Controller (`app.py` + `blueprints/`)**: The application factory in `app.py` wires extensions, per-connection SQLite `PRAGMA foreign_keys=ON`, and registers 5 blueprints (`auth`, `expenses`, `budget`, `analytics`, `main`) that contain all route logic. Shared utilities (including `Decimal`/paise helpers and `commit_session()` rollback hygiene) live in `utils.py`, and extension instances (CSRF, rate limiter) are defined in `extensions.py` to avoid circular imports.

---

## 2. Complete File Directory Index

```text
expense-flow/
├── .github/workflows/
│   └── ci.yml                  # GitHub Actions CI: Python 3.10–3.13 (SQLite) + PostgreSQL 16
├── alembic/
│   ├── env.py                  # Alembic environment (batch rebuilds on SQLite)
│   └── versions/               # Revisions 0001_baseline .. 0006_constraints_case_indexes
├── alembic.ini                 # Alembic config
├── app.py                      # Application factory (create_app): config, FK hook, blueprints
├── compose.yaml                # Two-container dev stack (Gunicorn web + PostgreSQL 16)
├── Dockerfile                  # Production-like container build
├── extensions.py               # Bare Flask extension instances (CSRFProtect, Limiter)
├── models.py                   # SQLAlchemy models + NAMING_CONVENTION (User, Expense, Budget)
├── utils.py                    # Shared helpers, Decimal/paise conversions, commit_session
├── requirements*.in / *.lock   # Universal uv lockfiles (base + postgres)
├── DEVELOPER_GUIDE.md          # This documentation file
├── docs/learning/stage-1b.md   # Stage 1B engineering notes
├── blueprints/
│   ├── __init__.py             # Package marker
│   ├── auth.py                 # Auth routes: login, register, logout, dashboard, edit_profile
│   ├── expenses.py             # Expense CRUD: add, view, edit, delete
│   ├── budget.py               # Budget management: set_budget
│   ├── analytics.py            # Analytics dashboard (9 sections) and PDF export
│   └── main.py                 # PWA routes: /sw.js, /manifest.json, /offline
├── deploy/
│   └── pa_deploy.sh            # PythonAnywhere deploy script (--stamp / --upgrade-to)
├── scripts/
│   ├── smoke_test.py           # Stdlib end-to-end smoke test (CSRF, cookies, CRUD, PDF)
│   └── verify_migration.py     # Pre/post migration audit & paise-conservation tool
├── tests/
│   ├── conftest.py             # Shared pytest fixtures (SQLite or EXPENSEFLOW_TEST_DATABASE_URI)
│   ├── test_app.py             # 27 functional tests: CRUD, budget logic, Decimal, CSRF
│   ├── test_migrations.py      # 36 Alembic migration, compatibility & EXPLAIN plan tests
│   ├── test_migration_verification.py # 4 audit & paise-conservation tests
│   └── test_regressions.py     # 99 regression tests: config, security, constraints, case auth, SW
├── static/
│   ├── main.css                # The core CSS Design System (CSS Variables, Dark/Light modes).
│   ├── avatars.css             # Avatar styling for profile selection.
│   ├── sw.js                   # Service Worker script handling PWA offline caching.
│   ├── manifest.json           # Web App Manifest for PWA installation.
│   └── icons/                  # Application icons.
├── templates/
│   ├── base.html               # The master Jinja2 wrapper (Navbar, Theme Toggle, Flash messages).
│   ├── index.html              # Animated landing page.
│   ├── login.html / register.html # Authentication views.
│   ├── dashboard.html          # Main overview (Stat cards, donut chart, trend lines).
│   ├── analytics.html          # Complex 9-section analytics (Heatmap, MoM trends).
│   ├── view_expense.html       # Transactions list with multi-parameter filtering.
│   ├── add_expense.html        # Form to add a new expense with server-side validation.
│   ├── set_budget.html         # Form to configure monthly or recurring budgets.
│   ├── edit_profile.html       # Account management page.
│   ├── 404.html / 500.html     # Custom error pages.
│   └── offline.html            # PWA offline fallback.
└── instance/
    └── expenses.db             # The SQLite database file (auto-generated).
```

---

## 3. Database Schema & Indexing

The application relies on three relational tables mapped by SQLAlchemy (`models.py`) and managed via Alembic (`alembic/`), with deterministic constraint naming (`NAMING_CONVENTION`):

1.  **User (`id`, `username`, `email`, `password_hash`, `recurring_budget_paise`, `registration_date`, `avatar`, `display_name`)**
    *   Utilizes Werkzeug `pbkdf2:sha256` password hashing and `BigInteger` paise storage (`recurring_budget_paise`, `CHECK (recurring_budget_paise > 0)`).
    *   `registration_date` and `avatar` are `NOT NULL`; functional unique indexes `ix_user_username_lower` and `ix_user_email_lower` enforce case-insensitive uniqueness alongside `func.lower()` queries in `auth.py`.
2.  **Expense (`id`, `user_id`, `date`, `category`, `amount_paise`, `description`, `payment_method`, `created_at`)**
    *   Linked to `User` via `user_id` (`ON DELETE CASCADE`); `date` is stored as SQL `DATE`, `amount_paise` is `BigInteger` with `CHECK (amount_paise > 0)`, and `payment_method`/`created_at` are `NOT NULL`.
    *   **Optimization:** Utilizes the composite ordering index `idx_expense_user_date_id` on `(user_id, date, id)` with half-open `[start, next_month_start)` date range filters for sort-free `ORDER BY date DESC, id DESC` index scans.
    *   Categories and payment methods are validated server-side against strict whitelists; monetary inputs are parsed via `Decimal` (`ROUND_HALF_UP`).
3.  **Budget (`id`, `user_id`, `year_month`, `amount_paise`)**
    *   Linked to `User` via `user_id` (`ON DELETE CASCADE`) with a unique constraint on `(user_id, year_month)` and `CHECK (amount_paise > 0)`.
    *   If a specific month's budget is not set, the backend dynamically falls back to `User.recurring_budget_paise` or `DEFAULT_BUDGET_PAISE` (`200000` paise / ₹2,000.00).

> [!NOTE]
> **Foreign Keys & Rollback Hygiene:** `create_app()` attaches a `PRAGMA foreign_keys=ON` connect hook to SQLite app engines so `ON DELETE CASCADE` and FK checks are enforced at runtime alongside PostgreSQL, and write routes commit through `commit_session()` to guarantee clean session rollback on `SQLAlchemyError`.

---

## 4. Key Technical Implementations (The "Magic")

### A. Flawless Dark/Light Mode via CSS Variables
Instead of relying on utility-first frameworks (like Tailwind) which clutter HTML, ExpenseFlow uses a highly scalable CSS Variable Architecture.
*   The `:root` pseudo-class defines the default Light Mode design tokens (e.g., `--bg-color: #F8FAFC`).
*   The `[data-theme="dark"]` attribute overrides these exact variables with dark variants.
*   This structural isolation prevents "mode contamination" and allows smooth `transition` animations across the entire application simultaneously.

### B. Algorithmic "GitHub-Style" Calendar Heatmap
Generating the 7-column calendar heatmap is done entirely from scratch without heavy charting libraries:
*   **Backend (`app.py`):** The python controller finds the user's maximum spending day for the selected month. It then calculates an `intensity` ratio (0.0 to 1.0) for every other day relative to that maximum.
*   **Frontend (`analytics.html`):** CSS Grid structures the days, and the Jinja2 template dynamically injects the calculated intensity into the `opacity` style attribute of each cell, creating a visually accurate, data-driven heatmap.

### C. Dynamic PDF Document Generation
The `/export/pdf` route utilizes the **ReportLab (`Platypus` engine)** to generate professional financial reports.
*   The PDF is generated dynamically in server memory using `io.BytesIO()`, ensuring zero disk-space bloat on the server.
*   It features custom table styling, pagination, and dynamic headers based on the current user's session data.

### D. Progressive Web App (PWA) Offline Resilience
The application utilizes a custom Service Worker (`sw.js`, cache `expenseflow-v4`) to intercept network requests:
*   **Cache-First Strategy:** Static assets (CSS, icons, `/manifest.json`, CDN JS/fonts) and `/offline` are served from the local cache (with a valid `503` `Response` fallback if an uncached static fetch fails offline).
*   **Network-Only Strategy for HTML & Dynamic Routes:** HTML navigations always hit the server directly and are never stored in Cache Storage (preventing cached private pages after logout), falling back to the pre-cached `/offline` page when offline, while non-navigation dynamic requests fall back to a valid `503` `Response` so no `FetchEvent` rejects unhandled.

### E. High-Fidelity Glassmorphism & Animations
The landing page implements high-end visual design techniques seen in top-tier SaaS platforms.
*   **Glow Orb & Frosted Glass:** A heavily blurred, absolutely positioned `.hero-glow-orb` div sits behind the cards. The cards utilize `backdrop-filter: blur(24px)` to allow the gradient to organically bleed through the UI.
*   **Staggered Entrance Velocity:** Cards trigger a `slideUpFadeIn` entrance animation on load with staggered delays (`--enter-delay`), before gracefully transitioning into an infinite, multi-layered CSS keyframe float.

### F. True Velocity "Burn Rate" Analytics
Instead of simply dividing total monthly spend by 30 days, the Dashboard dynamically computes the user's "True Velocity".
*   It calculates the exact number of days that have passed in the current month to date (`max(1, datetime.now().day)`).
*   Dividing current spending by this exact integer gives an aggressive, highly accurate daily burn rate that forces real-time financial accountability.

### G. CSRF Protection & Login Rate Limiting
All POST forms include a hidden `csrf_token()` field validated by **Flask-WTF's CSRFProtect**. Extension instances are defined in `extensions.py` (not in `app.py`) to avoid circular imports with blueprints.
*   **Rate Limiting:** Flask-Limiter enforces **5 POST requests per 15 minutes** on `/login`. The `on_breach` callback returns a proper `make_response(render_template('login.html'), 429)` — not a raw string — so the styled login page renders correctly on the 429 response.
*   **Testing:** CSRF is disabled in the test configuration (`WTF_CSRF_ENABLED=False`) so functional tests can POST without tokens. Two dedicated tests re-enable CSRF to verify rejection without a valid token.

### H. Automated Testing & CI
The `tests/` directory contains **166 pytest tests** across `test_app.py`, `test_regressions.py`, `test_migrations.py`, and `test_migration_verification.py` covering:
*   **Expense CRUD & Precision** — add, edit, delete with `Decimal` (`ROUND_HALF_UP`) paise parsing, server-side validation, and integer paise arithmetic
*   **Cross-user isolation** — one user cannot edit/delete another user's expenses (returns 404)
*   **Budget logic** — default budget, explicit budget row, recurring fallback, override priority, and strict ASCII input validation
*   **Database Integrity & Migrations** — `0001_baseline` → `0006_constraints_case_indexes` upgrade/downgrade parity, legacy Stage-1A `sqlite_autoindex_*` compatibility, `EXPLAIN` plan checks, paise-conservation audits (`verify_migration.py`), `CHECK`/`NOT NULL`/`ON DELETE CASCADE`, SQLite `PRAGMA foreign_keys=ON`, `commit_session()` rollback hygiene, and case-insensitive auth
*   **Security & regressions** — fail-fast production `EXPENSEFLOW_SECRET_KEY` (32+ chars), HTTPS `Set-Cookie` flags (`HttpOnly`, `Secure`, `SameSite=Lax`), generic login errors, PII-free auth logs, `/edit_profile` password gate for email changes, ReportLab XML escaping, dashboard recent-transactions ordering, and `sw.js` fetch resilience
*   **CSRF enforcement** — POST without token returns 400

Tests run against an isolated **in-memory SQLite database** (`sqlite:///:memory:`) by default or against PostgreSQL via `EXPENSEFLOW_TEST_DATABASE_URI`. **GitHub Actions CI** (`.github/workflows/ci.yml`) runs `pytest tests/ -v` across Python 3.10–3.13 (SQLite) and a PostgreSQL 16 service container on every push and pull request to `main`.

---

## 5. Deployment, Configuration & Local Setup

### Environment Variables & Application Factory Behavior
*   **`EXPENSEFLOW_ENV`** (`production`, `development`, or `testing`):
    *   `create_app()` (and the lazy module-level `app` attribute accessed by Gunicorn/WSGI) defaults `EXPENSEFLOW_ENV` to **`production`**.
    *   Running `python app.py` directly (`__main__`) defaults `EXPENSEFLOW_ENV` to **`development`** and binds Flask to `127.0.0.1:5000` with `debug=True`.
    *   `testing` mode always uses an in-memory SQLite database (`sqlite:///:memory:`) and disables CSRF and rate limiting.
*   **`EXPENSEFLOW_SECRET_KEY`**:
    *   **Required in `production`**: must be non-default and at least **32 characters** after stripping surrounding whitespace; otherwise `create_app('production')` fails fast with `RuntimeError`.
    *   Generate a strong production key with: `python -c "import secrets; print(secrets.token_hex(32))"`
    *   In `development` mode, a built-in fallback key is used automatically so no manually configured key is required locally.
*   **`EXPENSEFLOW_DATABASE_URI`**:
    *   Optional SQLAlchemy database URI used in `production` and `development`. Defaults to `sqlite:///expenses.db` when unset.

### Local Development Setup
1. Clone the repository: `git clone https://github.com/M3hul-raj/expense-flow.git`
2. Create and activate a virtual environment: `python -m venv venv`
3. Install dependencies: `pip install -r requirements.lock` (plus `requirements-postgres.lock` if targeting PostgreSQL)
4. Apply schema migrations: `alembic upgrade head`
5. Run the Flask development server: `python app.py` (defaults to `development` mode on `http://127.0.0.1:5000/`), or launch the Docker Compose stack (`docker compose up --build -d`) with `.env` copied from `.env.example`.

### Production Deployment (PythonAnywhere)

#### One-Time Setup
1. Open the **Web** tab on PythonAnywhere and enable **Force HTTPS** (required because production sets `SESSION_COOKIE_SECURE = True`).
2. Generate a random production key in the PythonAnywhere Bash console (do **not** commit or share the output):
   ```bash
   python3 -c "import secrets; print(secrets.token_hex(32))"
   ```
3. Open your **WSGI configuration file** from the **Web** tab and set `EXPENSEFLOW_ENV` and `EXPENSEFLOW_SECRET_KEY` **before** importing `app`:
   ```python
   import sys, os
   path = '/home/YOUR_USERNAME/ExpenseFlow'
   if path not in sys.path:
       sys.path.append(path)
   os.chdir(path)
   os.environ['EXPENSEFLOW_ENV'] = 'production'
   os.environ['EXPENSEFLOW_SECRET_KEY'] = '<paste-generated-32+-char-secret-here>'
   from app import app as application
   ```

#### Repeatable Deploy & Smoke Test Flow
1. **Audit** the existing database pre-flight (`python scripts/verify_migration.py sqlite:///$PWD/instance/expenses.db --strict --check-not-null`).
2. **Run the deploy script** in the PythonAnywhere Bash console with an explicit revision (preview first with `--dry-run`):
   ```bash
   cd ~/ExpenseFlow
   bash deploy/pa_deploy.sh --upgrade-to 0006_constraints_case_indexes --dry-run
   bash deploy/pa_deploy.sh --upgrade-to 0006_constraints_case_indexes
   ```
   *(Use `--stamp 0001_baseline` only for initial baseline tracking on an un-versioned DB.)* The script enforces branch `main`, backs up `instance/expenses.db` into `~/backups/` via `sqlite3 .backup`, verifies `PRAGMA integrity_check` and row counts, runs `git pull --ff-only origin main`, installs `requirements.lock`, executes the requested Alembic command, runs `pytest`, and prints the rollback command (`git reset --hard <old_hash>`). It never touches the WSGI file.
3. **Reload**: Click **Reload** on the PythonAnywhere **Web** tab.
4. **Run the end-to-end smoke test** against the live HTTPS URL using a dedicated test/throwaway account via environment variables:
   ```bash
   SMOKE_USER='<test-username>' SMOKE_PASS='<test-password>' \
     python scripts/smoke_test.py https://your_username.pythonanywhere.com
   ```

---

## 6. Portfolio & Resume Quick-Reference

For technical recruiters reviewing this repository, here is a summary of the engineering skills demonstrated in this codebase:

*   **Full Stack Architecture:** Engineered a modular MVC backend with Flask's application factory pattern and 5 Blueprints, using SQLAlchemy ORM + Alembic migrations across SQLite and PostgreSQL 16 with `BigInteger` paise precision, `CHECK`/`NOT NULL`/`ON DELETE CASCADE` constraints, functional `lower()` unique indexes, and composite `(user_id, date, id)` ordering indexing.
*   **Security Hardening:** Implemented CSRF token validation on all POST forms via Flask-WTF, brute-force login protection via Flask-Limiter (5 req / 15 min rate limit with custom 429 response rendering), fail-fast `EXPENSEFLOW_SECRET_KEY` checks, `HttpOnly`/`Secure`/`SameSite=Lax` cookies, and PII-free auth logging.
*   **Advanced Data Processing:** Developed complex data aggregation algorithms to feed a real-time 9-section analytics engine, calculating dynamic budget thresholds, MoM variances, and heatmap intensities.
*   **Testing & CI/CD:** Built a 166-test pytest suite with isolated SQLite & PostgreSQL 16 fixtures, migration verification tooling, and a stdlib end-to-end smoke test, integrated into a GitHub Actions CI pipeline across Python 3.10–3.13 and PostgreSQL 16.
*   **UI/UX Engineering:** Designed a premium, glassmorphic UI with a scalable CSS variable architecture, implementing flawless, OS-aware Dark and Light modes.
*   **Web APIs & PWA:** Programmed a custom Service Worker with network-only HTML navigations (zero private page caching) and cache-first static asset caching for offline resilience and mobile installation.
*   **Document Generation:** Built a custom pipeline using ReportLab to dynamically render and serve formatted, paginated PDF financial reports from memory.

---
*Built by Mehul Raj*
