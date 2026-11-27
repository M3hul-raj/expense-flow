<p align="center">
  <img src="static/icons/icon-192x192.png" alt="ExpenseFlow Logo" width="80" height="80" style="border-radius: 16px;">
</p>

<h1 align="center">ExpenseFlow</h1>

<p align="center">
  <strong>A premium, full-stack personal finance tracker built with Flask</strong><br>
  Track expenses · Set budgets · Deep Analytics · Smart Dark/Light Modes · Install as PWA
</p>

<p align="center">
  <a href="https://m3hulraj.pythonanywhere.com"><strong>🌐 Live Demo</strong></a> &nbsp;·&nbsp;
  <a href="#features"><strong>Features</strong></a> &nbsp;·&nbsp;
  <a href="#tech-stack"><strong>Tech Stack</strong></a> &nbsp;·&nbsp;
  <a href="#setup"><strong>Setup</strong></a>
</p>

---

## What is ExpenseFlow?

ExpenseFlow is a production-grade personal finance management application designed with a modern **premium fintech aesthetic**. It goes far beyond basic expense tracking — offering real-time budget alerts, 9-section analytics dashboards with GitHub-style heatmaps, beautiful Dark and Light modes, PDF report generation, and Progressive Web App support for mobile installation.

Built as a full-stack Flask application with SQLAlchemy ORM and the **application factory pattern**, it features CSRF protection, login rate limiting, server-side input validation, indexed database queries, comprehensive error handling, session-based authentication with hashed passwords, and a full automated test suite with CI.

---

## Features

### Core — Expense Management
- **Add Expenses** — Quick-add shortcuts, category/payment method selection, date picker with future-date prevention
- **Bank CSV Statement Import** — In-memory CSV parsing (`2 MB` / `2,000` row caps, UTF-8/UTF-16/CP1252) via declarative bank format configs, staging review screen in Jinja, idempotent SHA-256 deduplication preserving identical same-day repeats, manual-expense duplicate flags, deterministic `"Always apply"` keyword rules (`else Uncategorised`), and atomic single-transaction Confirm & Undo
- **Edit & Delete** — Full CRUD with server-side validation on every field
- **View All Transactions** — Searchable, filterable, sortable table with pagination
- **Multi-filter System** — Filter by category, month, payment method, date range, and amount range simultaneously
- **Savings Tips** — Random financial tip displayed on the Transactions page

### Budgeting
- **Monthly Budgets** — Set a specific budget for any month/year combination
- **Recurring Budgets** — Auto-apply the same budget to all future months
- **Budget Progress Bar** — Dynamic visual progress indicators on Dashboard and Add Expense pages
- **Smart Alerts** — Dismissible banners: amber at 90%+ usage, red at 100%+ (persisted per session via localStorage)

### Analytics & Reporting
- **9-Section Analytics Dashboard** — Month selector, MoM comparison, category breakdown, daily spending bar chart, top 5 expenses, budget history, payment method breakdown, and spending trend
- **GitHub-Style Calendar Heatmap** — Dynamic visual spending legend scaling relative to your highest spending day of the month
- **Dashboard Overview** — 4 stat cards (Total Spent, This Month, True Velocity Daily Average, Top Category), category donut chart, 6-month spending trend line chart
- **True Velocity Analytics** — The daily average dynamically tracks exactly how many days have passed in the current month to enforce real-time budgeting accountability.
- **PDF Export** — Professional ReportLab-generated PDF with indigo header, summary stats, category breakdown table, full paginated expense list, and page numbers
- **CSV Export** — Formula-injection-safe client-side filtered CSV export and server-side `/export/csv` download

### Design & UX
- **Flawless Dark & Light Modes** — Precision-engineered themes that respect OS preferences with a dedicated manual toggle switch
- **Fintech Aesthetic** — Indigo color system, Inter font, premium glassmorphic frosted cards, and smooth micro-animations
- **Responsive Layout** — Full-width dashboard-style layouts optimized for desktop, tablet, and mobile
- **Active Navbar** — Current page highlighted with indigo bottom border
- **Auto-dismissing Flash Messages** — 4-second fade-out with smooth CSS animations
- **Custom Error Pages** — Branded 404 and 500 pages matching the app design
- **Animated Landing Page** — "Vercel-style" glassmorphic stat cards with staggered slide-up entrance animations, infinite floating keyframes, and a dynamic glow orb background.

### User Management
- **Registration & Login** — Secure authentication with Werkzeug password hashing
- **Profile Editing** — Update username, email, and password with duplicate checks
- **Session Management** — Protected routes with login-required redirects

### Progressive Web App (PWA)
- **Installable** — Add to home screen on mobile and desktop
- **Service Worker** — Cache-first for static assets and `/offline`; network-only for HTML pages so authenticated pages are never cached
- **Offline Support** — Branded `/offline` fallback page when network is unavailable
- **App Icons** — Custom 192x192 and 512x512 indigo icons with stacked-layers logo

### Security & Infrastructure
- **CSRF Protection** — All POST forms validated via Flask-WTF CSRF tokens
- **Rate Limiting** — Flask-Limiter enforces 5 POST attempts per 15 minutes on `/login` and 10 uploads per 15 minutes on `/imports` POST
- **SQLAlchemy ORM & Migrations** — SQLite and PostgreSQL 16 parity via Alembic (`0001`–`0007`), `BigInteger` paise money columns, `CHECK`/`NOT NULL` constraints, `ON DELETE CASCADE` foreign keys, and statement-import staging/rule tables
- **Application Factory** — `create_app()` pattern supporting `production`, `development`, and `testing` configurations (with per-connection SQLite `PRAGMA foreign_keys=ON`)
- **Blueprint Architecture** — Routes split into 6 blueprints (`auth`, `expenses`, `budget`, `analytics`, `imports`, `main`) for modularity
- **Indexed Queries** — Composite `(user_id, date, id)` ordering index, half-open `DATE` range queries, and functional `lower(username)` / `lower(email)` unique indexes
- **Server-side Validation** — `Decimal` (`ROUND_HALF_UP`) money parsing, case-insensitive auth queries, whitelist validation for categories/payment methods/avatars, password re-authentication for email changes, untrusted CSV HTML/formula-injection neutralization, and future-date blocking
- **Automated Testing** — 192 pytest tests covering CRUD, budget logic, validation, CSRF enforcement, security regressions, Alembic migrations, database constraints, and CSV statement import
- **CI/CD** — GitHub Actions workflow runs `pytest tests/ -v` across Python 3.10–3.13 (SQLite) and PostgreSQL 16 on every push and PR to `main`
- **Structured Logging** — All events logged to `expenseflow.log` with timestamps
- **Environment Configuration** — Fail-fast `EXPENSEFLOW_SECRET_KEY` (32+ chars) in production; optional `EXPENSEFLOW_ENV` and `EXPENSEFLOW_DATABASE_URI`

---

## Screenshots

<p align="center">
  <img src="static/screenshots/dashboard.png" width="800" alt="ExpenseFlow dark-mode dashboard showing summary stat cards, Monthly Budget progress bar, Spending by Category donut chart, and 6-month Spending Trend line chart">
</p>

### Detailed Views

<details>
<summary><b>1. Landing Page</b> (Click to expand)</summary>
<img src="static/screenshots/landing.png" alt="ExpenseFlow dark-mode landing page featuring hero preview cards, feature highlights, and a 3-step onboarding flow">
</details>

<details>
<summary><b>2. Analytics & Heatmap</b> (Click to expand)</summary>
<img src="static/screenshots/analytics.png" alt="ExpenseFlow 9-section Spending Analytics view with month-over-month comparison, Category Breakdown, Budget vs Actual chart, Daily Spending bars, Payment Methods, Top 5 Expenses, and Spending Calendar heatmap">
</details>

<details>
<summary><b>3. Transactions & Filters</b> (Click to expand)</summary>
<img src="static/screenshots/transactions.png" alt="ExpenseFlow Transactions page showing search and multi-filter controls, Tip of the Day banner, PDF/CSV/Print actions, and sorted expense table with Filtered Total">
</details>

<details>
<summary><b>4. Add Expense</b> (Click to expand)</summary>
<img src="static/screenshots/add_expense.png" alt="ExpenseFlow Add New Expense page with Quick Add category presets, validated form inputs, and live Budget Status card">
</details>

---

## Tech Stack

| Layer | Technology |
|-------|-----------|
| **Backend** | Python 3.10–3.13, Flask 3.1, Flask-SQLAlchemy, Alembic |
| **Security** | Flask-WTF (CSRF), Flask-Limiter (rate limiting) |
| **Database** | SQLite & PostgreSQL 16 (`psycopg` v3), Alembic migrations |
| **Authentication** | Werkzeug security (pbkdf2 password hashing) |
| **Testing** | pytest (192 tests), GitHub Actions CI (Python 3.10–3.13 + PostgreSQL 16) |
| **Containers** | Docker Compose (`web` Flask/Gunicorn + `db` PostgreSQL 16) |
| **Frontend** | HTML5, CSS3, JavaScript, Jinja2 templating |
| **UI Framework** | Bootstrap 5.3, Font Awesome 6.4 |
| **Charts** | Chart.js 4.4 |
| **Typography** | Google Fonts (Inter) |
| **PDF Generation** | ReportLab (Platypus layout engine) |
| **PWA** | Service Worker, Web App Manifest |
| **Date Handling** | python-dateutil (relativedelta) |
| **Deployment** | PythonAnywhere (WSGI) |

---

## What Makes This Different?

Most expense trackers are basic CRUD apps with minimal styling. ExpenseFlow is built to **production standards**:

- **Real fintech UI** — Not Bootstrap defaults. Custom design system with an indigo palette, frosted glassmorphic cards, micro-animations, and perfect Dark/Light modes that look like a real SaaS product.
- **Smart budgeting** — Not just "set a number". Recurring budgets, visual progress bars, contextual warnings at 90% and 100% thresholds, and a budget sidebar visible while adding expenses.
- **Deep analytics** — 9 analytical sections including GitHub-style calendar heatmaps, month-over-month comparisons, and payment method breakdowns. Not just "total spent this month".
- **PDF reports** — Professional, paginated PDF exports with styled tables, category breakdowns, and branded headers. Not a plain text dump.
- **PWA installable** — Service worker with intelligent caching strategies. Works offline. Installable on any device.
- **Production security** — CSRF token validation on every form, login rate limiting (5 per 15 min), server-side validation on every input, future-date blocking, category whitelists, duplicate email checks, environment-variable secrets, and comprehensive error handling.

---

<h2 id="setup">Local Setup</h2>

### Prerequisites
- Python 3.10+ installed

### Installation

```bash
# Clone the repository
git clone https://github.com/M3hul-raj/expense-flow.git
cd expense-flow

# Create and activate virtual environment
python -m venv venv

# Windows
.\venv\Scripts\activate

# Mac/Linux
source venv/bin/activate

# Install locked dependencies (or requirements-postgres.lock for PostgreSQL)
pip install -r requirements.lock

# Initialize / migrate the SQLite schema
alembic upgrade head

# Run the development server (defaults EXPENSEFLOW_ENV to 'development' on 127.0.0.1:5000)
python app.py
```

Or run the Docker Compose stack (Flask/Gunicorn + PostgreSQL 16):

```bash
cp .env.example .env  # set local POSTGRES_* values
docker compose up --build -d
```

Open your browser and navigate to **http://127.0.0.1:5000/**

### Environment Variables & App Modes

| Variable | Purpose | Requirement & Default |
|----------|---------|-----------------------|
| `EXPENSEFLOW_ENV` | Runtime mode: `production`, `development`, or `testing` | `create_app()` (and lazy `app` WSGI/Gunicorn entrypoint) defaults to `production`; `python app.py` defaults to `development` |
| `EXPENSEFLOW_SECRET_KEY` | Flask session signing key | **Required in `production`** (minimum 32 characters after stripping whitespace, non-default). `development` mode uses a built-in fallback and requires no manually configured key |
| `EXPENSEFLOW_DATABASE_URI` | Optional SQLAlchemy database URI | Optional in `production` and `development`; defaults to `sqlite:///expenses.db` (`postgres://`/`postgresql://` normalize to `postgresql+psycopg://`) |
| `EXPENSEFLOW_TEST_DATABASE_URI` | Optional pytest database URI | Used by `pytest`; defaults to isolated in-memory SQLite (`sqlite:///:memory:`) when unset |

Generate a strong production key with:

```bash
python -c "import secrets; print(secrets.token_hex(32))"
```

---

## Deployment (PythonAnywhere)

1. Create a free account at [PythonAnywhere](https://www.pythonanywhere.com/)
2. Open a **Bash Console** and clone the repo:
   ```bash
   git clone https://github.com/M3hul-raj/expense-flow.git
   cd expense-flow
   ```
3. Create a virtual environment and initialize the database:
   ```bash
   mkvirtualenv --python=/usr/bin/python3.10 myvenv
   pip install -r requirements.lock
   export EXPENSEFLOW_DATABASE_URI="sqlite:///$PWD/instance/expenses.db"
   alembic upgrade head
   ```
   *(For repeat deploys on an existing DB, use `bash deploy/pa_deploy.sh --upgrade-to <REVISION>`.)*
4. Generate a production secret key in Bash:
   ```bash
   python -c "import secrets; print(secrets.token_hex(32))"
   ```
5. Go to **Web** tab → **Add a new web app** → **Manual configuration** → Python 3.10
6. Set **Source code** to `/home/YOUR_USERNAME/expense-flow`
7. Set **Virtualenv** to `myvenv`
8. Edit the **WSGI configuration file** (set environment variables *before* importing `app`):
   ```python
   import sys, os
   path = '/home/YOUR_USERNAME/expense-flow'
   if path not in sys.path:
       sys.path.append(path)
   os.chdir(path)
   os.environ['EXPENSEFLOW_ENV'] = 'production'
   os.environ['EXPENSEFLOW_SECRET_KEY'] = 'paste-your-generated-key-here'
   from app import app as application
   ```
9. Click **Reload** — your app is live!

---

## Project Structure

```
expense-flow/
├── .github/workflows/
│   └── ci.yml                  # GitHub Actions CI (SQLite 3.10–3.13 + PostgreSQL 16)
├── alembic/
│   ├── env.py                  # Alembic environment (batch mode on SQLite)
│   └── versions/               # Schema migrations (0001_baseline .. 0007_statement_import)
├── alembic.ini                 # Alembic configuration
├── app.py                      # Application factory (create_app)
├── compose.yaml                # Docker Compose (Gunicorn web + PostgreSQL 16)
├── Dockerfile                  # Container build for web service
├── extensions.py               # Flask extension instances (CSRF, Limiter)
├── models.py                   # SQLAlchemy models (User, Expense, Budget, ImportBatch, ImportRow, CategoryRule)
├── statement_import.py         # Bank CSV mapping configs, in-memory parser, dedupe, deterministic rules
├── utils.py                    # Shared helpers, Decimal/paise utilities, CSV sanitization, commit_session
├── requirements*.in / *.lock   # Universal uv dependency lockfiles (base + postgres)
├── blueprints/
│   ├── __init__.py
│   ├── auth.py                 # Auth routes (login, register, dashboard, profile)
│   ├── expenses.py             # Expense CRUD & CSV export routes (add, view, edit, delete, export/csv)
│   ├── budget.py               # Budget management routes
│   ├── analytics.py            # Analytics dashboard and PDF export
│   ├── imports.py              # Statement import staging, review, rules, confirm, and undo
│   └── main.py                 # PWA routes (service worker, manifest, offline)
├── deploy/
│   └── pa_deploy.sh            # Safe PythonAnywhere deploy (--stamp / --upgrade-to)
├── docs/
│   ├── RESUME_FACTS.md         # Verifiable claim -> command engineering reference
│   └── learning/
│       └── stage-1b.md         # Stage 1B engineering notes
├── scripts/
│   ├── smoke_test.py           # Stdlib end-to-end HTTP smoke test (incl. import -> confirm -> undo)
│   └── verify_migration.py     # Pre/post migration audit & paise-conservation check
├── tests/
│   ├── conftest.py             # Shared pytest fixtures (SQLite or EXPENSEFLOW_TEST_DATABASE_URI)
│   ├── test_app.py             # 27 functional tests (CRUD, budget, Decimal, CSRF)
│   ├── test_migrations.py      # 38 Alembic migration & EXPLAIN query-plan tests
│   ├── test_migration_verification.py # 4 audit & paise-conservation tests
│   ├── test_regressions.py     # 102 regression tests (security, constraints, case auth, SW, UI)
│   └── test_statement_import.py # 21 statement import, dedupe, rules, atomicity, security & smoke tests
├── static/
│   ├── main.css                # Design system (fintech theme)
│   ├── avatars.css             # Avatar styling
│   ├── sw.js                   # Service worker (PWA caching)
│   ├── manifest.json           # PWA web app manifest
│   ├── samples/
│   │   └── synthetic_indian_bank_statement.csv # Synthetic Indian bank CSV sample fixture
│   └── icons/
│       ├── icon-192x192.png    # PWA icon (small)
│       └── icon-512x512.png    # PWA icon (large)
├── templates/
│   ├── base.html               # Base layout (navbar, flash, PWA)
│   ├── index.html              # Landing page
│   ├── login.html              # Sign in
│   ├── register.html           # Create account
│   ├── dashboard.html          # Main dashboard
│   ├── analytics.html          # 9-section analytics
│   ├── view_expense.html       # Transactions list
│   ├── add_expense.html        # Add expense form
│   ├── edit_expense.html       # Edit expense form
│   ├── import_upload.html      # Statement CSV upload, recent batches & saved rules
│   ├── import_review.html      # Staging review, duplicate flags, invalid rows, confirm & undo
│   ├── set_budget.html         # Budget configuration
│   ├── edit_profile.html       # Profile management
│   ├── offline.html            # PWA offline fallback
│   ├── 404.html                # Custom 404 page
│   └── 500.html                # Custom 500 page
└── instance/
    └── expenses.db             # SQLite database (auto-created)
```

---

## License

This project is licensed under the [MIT License](LICENSE).

---

<p align="center">
  Built with Flask · Designed with care · <a href="https://m3hulraj.pythonanywhere.com">Try it live</a>
</p>
