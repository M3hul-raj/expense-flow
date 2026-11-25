import logging
import os
from flask import Flask, render_template
from models import db
from extensions import csrf, limiter

# ── Logging setup ─────────────────────────────────────────────────────────────
# Logs to a rotating file in the project root; INFO level for general events,
# WARNING/ERROR for security/DB issues.
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
    handlers=[
        logging.FileHandler('expenseflow.log', encoding='utf-8'),
        logging.StreamHandler(),         # also print to console in dev
    ]
)
logger = logging.getLogger('expenseflow')

DEV_FALLBACK_SECRET_KEY = 'dev-only-fallback-change-in-prod-!@#'
TEST_FALLBACK_SECRET_KEY = 'test-only-secret-key'
VALID_ENVS = {'production', 'development', 'testing'}


def create_app(config_name=None):
    """
    Application factory.

    config_name='production'  →  requires EXPENSEFLOW_SECRET_KEY, secure cookies, file/env DB.
    config_name='development' →  allows dev fallback secret key and HTTP cookies.
    config_name='testing'     →  in-memory SQLite, CSRF + rate-limit disabled.

    If config_name is omitted, EXPENSEFLOW_ENV (defaulting to 'production') is used.
    """
    env = config_name if config_name is not None else os.environ.get('EXPENSEFLOW_ENV', 'production')
    if env not in VALID_ENVS:
        raise ValueError(f"Unknown ExpenseFlow environment: {env!r}")

    if env == 'production':
        secret_key = os.environ.get('EXPENSEFLOW_SECRET_KEY', '')
        if not secret_key.strip() or secret_key in {DEV_FALLBACK_SECRET_KEY, TEST_FALLBACK_SECRET_KEY}:
            raise RuntimeError(
                'EXPENSEFLOW_SECRET_KEY environment variable must be set to a strong, '
                'non-default value in production.'
            )
    elif env == 'development':
        secret_key = os.environ.get('EXPENSEFLOW_SECRET_KEY') or DEV_FALLBACK_SECRET_KEY
    else:
        secret_key = os.environ.get('EXPENSEFLOW_SECRET_KEY') or TEST_FALLBACK_SECRET_KEY

    app = Flask(__name__)

    # ── Jinja filters ─────────────────────────────────────────────────────
    @app.template_filter('currency')
    def format_currency(value):
        """Formats a float/int into Indian Rupee format."""
        try:
            value = float(value)
            return f"₹{value:,.2f}"
        except (ValueError, TypeError):
            return "₹0.00"

    # ── Config ────────────────────────────────────────────────────────────
    app.secret_key = secret_key
    app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
    # WTF_CSRF_TIME_LIMIT: how long (seconds) a CSRF token stays valid; None = session lifetime
    app.config['WTF_CSRF_TIME_LIMIT'] = None
    app.config['SESSION_COOKIE_HTTPONLY'] = True
    app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'
    app.config['SESSION_COOKIE_SECURE'] = (env != 'development')

    if env == 'testing':
        app.config.update({
            'TESTING': True,
            'SQLALCHEMY_DATABASE_URI': 'sqlite:///:memory:',
            'WTF_CSRF_ENABLED': False,
            'WTF_CSRF_CHECK_DEFAULT': False,
            'RATELIMIT_ENABLED': False,
        })
    else:
        app.config['SQLALCHEMY_DATABASE_URI'] = os.environ.get(
            'EXPENSEFLOW_DATABASE_URI',
            'sqlite:///expenses.db',
        )

    # ── Extensions ────────────────────────────────────────────────────────
    db.init_app(app)
    csrf.init_app(app)
    limiter.init_app(app)

    # ── Blueprints ────────────────────────────────────────────────────────
    from blueprints.main      import main_bp
    from blueprints.auth      import auth_bp
    from blueprints.expenses  import expenses_bp
    from blueprints.budget    import budget_bp
    from blueprints.analytics import analytics_bp

    app.register_blueprint(main_bp)
    app.register_blueprint(auth_bp)
    app.register_blueprint(expenses_bp)
    app.register_blueprint(budget_bp)
    app.register_blueprint(analytics_bp)

    # ── Error handlers ────────────────────────────────────────────────────
    @app.errorhandler(404)
    def page_not_found(e):
        """Render a branded 404 page for any missing route."""
        return render_template('404.html'), 404

    @app.errorhandler(500)
    def internal_error(e):
        """Render a branded 500 page for any unhandled server error."""
        db.session.rollback()  # safety: clear any broken transaction
        return render_template('500.html'), 500

    # ── Database ──────────────────────────────────────────────────────────
    with app.app_context():
        db.create_all()

    return app


# ── Lazy module-level app ─────────────────────────────────────────────────────
# Preserved so that  gunicorn app:app  and  from app import app as application
# fail fast in production without breaking `from app import create_app` in tests.
_app = None


def __getattr__(name):
    global _app
    if name == 'app':
        if _app is None:
            _app = create_app()
        return _app
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


if __name__ == '__main__':
    run_env = os.environ.get('EXPENSEFLOW_ENV', 'development')
    create_app(run_env).run(debug=(run_env == 'development'), host='0.0.0.0', port=5000)
