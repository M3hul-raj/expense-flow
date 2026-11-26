"""
test_regressions.py — Stage 1A security and correctness regression tests.

Each test class targets a specific bug or hardening fix and was written
to fail before the corresponding fix was applied.
"""
import pytest

import app as app_module
from app import create_app
from models import db, User


class TestSecretKeyAndSessionConfig:
    """Tests for fail-fast SECRET_KEY handling and session cookie flags."""

    def test_production_requires_secret_key(self, monkeypatch, tmp_path):
        """create_app('production') must fail fast when EXPENSEFLOW_SECRET_KEY is missing."""
        monkeypatch.delenv('EXPENSEFLOW_SECRET_KEY', raising=False)
        monkeypatch.setenv(
            'EXPENSEFLOW_DATABASE_URI',
            f"sqlite:///{(tmp_path / 'prod_missing_key.db').as_posix()}",
        )

        with pytest.raises(RuntimeError, match='EXPENSEFLOW_SECRET_KEY'):
            create_app('production')

    @pytest.mark.parametrize(
        'bad_key',
        [
            '',
            '   ',
            'too-short-secret',
            'x' * 31,
            '  ' + ('x' * 31) + '  ',
            'dev-only-fallback-change-in-prod-!@#',
            '  dev-only-fallback-change-in-prod-!@#  ',
            '       test-only-secret-key       ',
        ],
    )
    def test_production_rejects_blank_short_or_fallback_secret_key(
        self, monkeypatch, tmp_path, bad_key
    ):
        """create_app('production') must reject blank, <32-char, or fallback keys (even padded)."""
        monkeypatch.setenv('EXPENSEFLOW_SECRET_KEY', bad_key)
        monkeypatch.setenv(
            'EXPENSEFLOW_DATABASE_URI',
            f"sqlite:///{(tmp_path / 'prod_bad_key.db').as_posix()}",
        )

        with pytest.raises(RuntimeError, match='EXPENSEFLOW_SECRET_KEY'):
            create_app('production')

    def test_production_strips_surrounding_whitespace_from_valid_secret_key(
        self, monkeypatch, tmp_path
    ):
        """Validated stripped key must match the active app.secret_key."""
        db_file = tmp_path / 'prod_stripped.db'
        strong_key = 'prod-secret-key-with-at-least-32-chars!!'
        monkeypatch.setenv('EXPENSEFLOW_SECRET_KEY', f'  {strong_key}\n')
        monkeypatch.setenv('EXPENSEFLOW_DATABASE_URI', f"sqlite:///{db_file.as_posix()}")

        prod_app = create_app('production')
        try:
            assert prod_app.secret_key == strong_key
        finally:
            with prod_app.app_context():
                db.engine.dispose()

    def test_production_sets_secure_session_cookie_flags(self, monkeypatch, tmp_path):
        """Production must set strict session cookie flags and use configured DB URI."""
        db_file = tmp_path / 'prod_cookies.db'
        expected_uri = f"sqlite:///{db_file.as_posix()}"
        strong_key = 'prod-secret-key-with-at-least-32-chars!!'
        assert len(strong_key) >= 32
        monkeypatch.setenv('EXPENSEFLOW_SECRET_KEY', strong_key)
        monkeypatch.setenv('EXPENSEFLOW_DATABASE_URI', expected_uri)

        prod_app = create_app('production')
        try:
            assert prod_app.secret_key == strong_key
            assert prod_app.config['SESSION_COOKIE_SECURE'] is True
            assert prod_app.config['SESSION_COOKIE_HTTPONLY'] is True
            assert prod_app.config['SESSION_COOKIE_SAMESITE'] == 'Lax'
            assert prod_app.config['SQLALCHEMY_DATABASE_URI'] == expected_uri
            assert not db_file.exists()
            with prod_app.app_context():
                db.create_all()
                assert db_file.exists()
        finally:
            with prod_app.app_context():
                db.engine.dispose()

    def test_development_allows_fallback_secret_key_and_http_cookies(
        self, monkeypatch, tmp_path
    ):
        """Development mode allows local fallback secret key and HTTP cookies."""
        db_file = tmp_path / 'dev.db'
        expected_uri = f"sqlite:///{db_file.as_posix()}"
        monkeypatch.delenv('EXPENSEFLOW_SECRET_KEY', raising=False)
        monkeypatch.setenv('EXPENSEFLOW_DATABASE_URI', expected_uri)

        dev_app = create_app('development')
        try:
            assert dev_app.secret_key
            assert dev_app.config['SESSION_COOKIE_SECURE'] is False
            assert dev_app.config['SESSION_COOKIE_HTTPONLY'] is True
            assert dev_app.config['SESSION_COOKIE_SAMESITE'] == 'Lax'
            assert dev_app.config['SQLALCHEMY_DATABASE_URI'] == expected_uri
        finally:
            with dev_app.app_context():
                db.engine.dispose()

    def test_module_app_attribute_fails_fast_without_secret_key(
        self, monkeypatch, tmp_path
    ):
        """Accessing app.app (WSGI entrypoint) without a secret key must fail fast."""
        monkeypatch.delenv('EXPENSEFLOW_SECRET_KEY', raising=False)
        monkeypatch.delenv('EXPENSEFLOW_ENV', raising=False)
        monkeypatch.setenv(
            'EXPENSEFLOW_DATABASE_URI',
            f"sqlite:///{(tmp_path / 'wsgi_fail.db').as_posix()}",
        )
        monkeypatch.setattr(app_module, '_app', None, raising=False)

        with pytest.raises(RuntimeError, match='EXPENSEFLOW_SECRET_KEY'):
            _ = app_module.app

    def test_production_login_set_cookie_header_flags(self, monkeypatch, tmp_path):
        """Real Set-Cookie header on HTTPS /login in production must include HttpOnly, Secure, SameSite=Lax."""
        import re

        db_file = tmp_path / 'prod_set_cookie.db'
        monkeypatch.setenv('EXPENSEFLOW_SECRET_KEY', 'prod-secret-key-with-at-least-32-chars!!')
        monkeypatch.setenv('EXPENSEFLOW_DATABASE_URI', f"sqlite:///{db_file.as_posix()}")

        prod_app = create_app('production')
        try:
            with prod_app.app_context():
                db.create_all()
                u = User(username='produser', email='produser@example.com')
                u.set_password('strongpass123')
                db.session.add(u)
                db.session.commit()

            c = prod_app.test_client()
            get_resp = c.get('/login', base_url='https://example.test')
            assert get_resp.status_code == 200
            html = get_resp.data.decode('utf-8')
            match = re.search(
                r'name=["\']csrf_token["\'][^>]+value=["\']([^"\']+)["\']'
                r'|value=["\']([^"\']+)["\'][^>]+name=["\']csrf_token["\']',
                html,
            )
            assert match
            token = match.group(1) or match.group(2)

            post_resp = c.post(
                '/login',
                base_url='https://example.test',
                headers={'Referer': 'https://example.test/login'},
                data={'username': 'produser', 'password': 'strongpass123', 'csrf_token': token},
                follow_redirects=False,
            )
            assert post_resp.status_code == 302
            set_cookies = post_resp.headers.getlist('Set-Cookie')
            session_cookies = [h for h in set_cookies if h.startswith('session=')]
            assert len(session_cookies) == 1
            header = session_cookies[0]
            attrs = {part.strip().lower() for part in header.split(';')[1:]}
            assert 'httponly' in attrs
            assert 'secure' in attrs
            assert 'samesite=lax' in attrs
        finally:
            with prod_app.app_context():
                db.engine.dispose()


class TestLoginEnumeration:
    """Tests ensuring /login does not reveal whether an account exists."""

    def test_unknown_user_and_wrong_password_return_identical_response(
        self, client, user
    ):
        """Both unknown user and wrong password must redirect to /login with the same error."""
        resp_unknown = client.post(
            '/login',
            data={'username': 'no_such_user', 'password': 'wrongpassword'},
            follow_redirects=False,
        )
        resp_wrong_pw = client.post(
            '/login',
            data={'username': user.username, 'password': 'wrongpassword'},
            follow_redirects=False,
        )

        assert resp_unknown.status_code == 302
        assert resp_wrong_pw.status_code == 302
        assert resp_unknown.headers['Location'].endswith('/login')
        assert resp_wrong_pw.headers['Location'].endswith('/login')

        page_unknown = client.post(
            '/login',
            data={'username': 'no_such_user', 'password': 'wrongpassword'},
            follow_redirects=True,
        )
        page_wrong_pw = client.post(
            '/login',
            data={'username': user.username, 'password': 'wrongpassword'},
            follow_redirects=True,
        )

        assert b'Invalid username/email or password.' in page_unknown.data
        assert b'Invalid username/email or password.' in page_wrong_pw.data
        assert b'User does not exist' not in page_unknown.data
        assert b'Invalid password' not in page_wrong_pw.data


class TestEditProfileValidation:
    """Tests for /edit_profile validation, email password gate, avatars, and IntegrityError."""

    def test_missing_form_keys_handled_without_error(self, auth_client, user):
        """Submitting empty form data must flash validation error, not raise 400/500."""
        resp = auth_client.post('/edit_profile', data={}, follow_redirects=True)
        assert resp.status_code == 200
        assert b'Username must be at least 3 characters long.' in resp.data
        db.session.expire_all()
        assert db.session.get(User, user.id).username == 'alice'

    @pytest.mark.parametrize('bad_username', ['ab', 'a' * 31, 'bad user!', 'alice-1'])
    def test_invalid_username_rejected(self, auth_client, user, bad_username):
        resp = auth_client.post(
            '/edit_profile',
            data={'username': bad_username, 'email': user.email},
            follow_redirects=True,
        )
        assert resp.status_code == 200
        db.session.expire_all()
        assert db.session.get(User, user.id).username == 'alice'

    @pytest.mark.parametrize('bad_email', ['notanemail', '@example.com', 'a@.com', 'a b@c.com'])
    def test_invalid_email_rejected(self, auth_client, user, bad_email):
        resp = auth_client.post(
            '/edit_profile',
            data={
                'username': user.username,
                'email': bad_email,
                'current_password': 'password123',
            },
            follow_redirects=True,
        )
        assert resp.status_code == 200
        assert b'valid email' in resp.data
        db.session.expire_all()
        assert db.session.get(User, user.id).email == 'alice@test.com'

    def test_email_change_requires_current_password(self, auth_client, user):
        """Changing email without valid current_password must be rejected."""
        resp_missing = auth_client.post(
            '/edit_profile',
            data={'username': user.username, 'email': 'alice_new@test.com'},
            follow_redirects=True,
        )
        assert resp_missing.status_code == 200
        assert b'Current password is required' in resp_missing.data

        resp_wrong = auth_client.post(
            '/edit_profile',
            data={
                'username': user.username,
                'email': 'alice_new@test.com',
                'current_password': 'wrong-password',
            },
            follow_redirects=True,
        )
        assert resp_wrong.status_code == 200
        assert b'Current password is required' in resp_wrong.data

        db.session.expire_all()
        assert db.session.get(User, user.id).email == 'alice@test.com'

    def test_email_change_succeeds_with_valid_current_password(self, auth_client, user):
        resp = auth_client.post(
            '/edit_profile',
            data={
                'username': user.username,
                'email': 'alice_new@test.com',
                'current_password': 'password123',
            },
            follow_redirects=True,
        )
        assert resp.status_code == 200
        db.session.expire_all()
        assert db.session.get(User, user.id).email == 'alice_new@test.com'

    @pytest.mark.parametrize('bad_avatar', ['avatar_0', 'avatar_41', 'avatar-1', 'evil'])
    def test_invalid_avatar_rejected(self, auth_client, user, bad_avatar):
        resp = auth_client.post(
            '/edit_profile',
            data={
                'username': user.username,
                'email': user.email,
                'avatar': bad_avatar,
            },
            follow_redirects=True,
        )
        assert resp.status_code == 200
        assert b'valid avatar' in resp.data
        db.session.expire_all()
        assert db.session.get(User, user.id).avatar == 'avatar_1'

    @pytest.mark.parametrize('stored_avatar', ['avatar_1', 'avatar_16', 'avatar_40'])
    def test_existing_stored_avatars_pass(self, auth_client, user, stored_avatar):
        resp = auth_client.post(
            '/edit_profile',
            data={
                'username': user.username,
                'email': user.email,
                'avatar': stored_avatar,
            },
            follow_redirects=True,
        )
        assert resp.status_code == 200
        db.session.expire_all()
        assert db.session.get(User, user.id).avatar == stored_avatar

    def test_avatars_css_defines_class_for_every_avatar_id(self):
        from pathlib import Path
        import re
        from blueprints.auth import AVATAR_COUNT

        css_path = Path(__file__).resolve().parent.parent / 'static' / 'avatars.css'
        css_text = css_path.read_text(encoding='utf-8')
        defined = set(re.findall(r'\.avatar-(\d+)\b', css_text))
        expected = {str(i) for i in range(1, AVATAR_COUNT + 1)}
        assert expected <= defined

    def test_duplicate_username_or_email_rejected(self, auth_client, user, user2):
        resp_u = auth_client.post(
            '/edit_profile',
            data={'username': user2.username, 'email': user.email},
            follow_redirects=True,
        )
        assert b'Username already exists' in resp_u.data

        resp_e = auth_client.post(
            '/edit_profile',
            data={
                'username': user.username,
                'email': user2.email,
                'current_password': 'password123',
            },
            follow_redirects=True,
        )
        assert b'Email already in use' in resp_e.data

    def test_integrity_error_handled_without_500(self, auth_client, user, monkeypatch):
        from sqlalchemy.exc import IntegrityError

        def raise_integrity_error():
            raise IntegrityError('UPDATE user', {}, Exception('UNIQUE constraint failed'))

        monkeypatch.setattr(db.session, 'commit', raise_integrity_error)
        resp = auth_client.post(
            '/edit_profile',
            data={'username': 'alice_renamed', 'email': user.email},
            follow_redirects=True,
        )
        assert resp.status_code == 200
        assert b'already in use' in resp.data


class TestSetBudgetValidation:
    """Tests for /set_budget validation while preserving recurring budgets."""

    def test_missing_form_keys_handled_without_error(self, auth_client, user):
        from models import Budget

        resp = auth_client.post('/set_budget', data={}, follow_redirects=True)
        assert resp.status_code == 200
        assert Budget.query.filter_by(user_id=user.id).count() == 0

    @pytest.mark.parametrize('bad_month', ['', '0', '00', '13', 'abc', '٠٥', '０５'])
    def test_invalid_month_rejected(self, auth_client, user, bad_month):
        from models import Budget

        resp = auth_client.post(
            '/set_budget',
            data={'month': bad_month, 'year': '2026', 'budget': '5000'},
            follow_redirects=True,
        )
        assert resp.status_code == 200
        assert b'valid month' in resp.data
        assert Budget.query.filter_by(user_id=user.id).count() == 0

    @pytest.mark.parametrize('bad_year', ['', '1999', '2101', '99999', 'abcd', '٢٠٢٦', '２０２６'])
    def test_invalid_year_rejected(self, auth_client, user, bad_year):
        from models import Budget

        resp = auth_client.post(
            '/set_budget',
            data={'month': '05', 'year': bad_year, 'budget': '5000'},
            follow_redirects=True,
        )
        assert resp.status_code == 200
        assert b'valid year' in resp.data
        assert Budget.query.filter_by(user_id=user.id).count() == 0

    @pytest.mark.parametrize('bad_amount', ['', '0', '-50', 'nan', 'inf', '-inf', 'abc', '200000000'])
    def test_invalid_budget_amount_rejected(self, auth_client, user, bad_amount):
        from models import Budget

        resp = auth_client.post(
            '/set_budget',
            data={'month': '05', 'year': '2026', 'budget': bad_amount},
            follow_redirects=True,
        )
        assert resp.status_code == 200
        assert Budget.query.filter_by(user_id=user.id).count() == 0

    def test_valid_monthly_and_recurring_budget_preserved(self, auth_client, user):
        from models import Budget

        resp1 = auth_client.post(
            '/set_budget',
            data={'month': '5', 'year': '2026', 'budget': '4500'},
            follow_redirects=True,
        )
        assert resp1.status_code == 200
        db.session.expire_all()
        row = Budget.query.filter_by(user_id=user.id, year_month='2026-05').first()
        assert row is not None and row.amount == 4500.0
        assert db.session.get(User, user.id).recurring_budget is None

        resp2 = auth_client.post(
            '/set_budget',
            data={'month': '06', 'year': '2026', 'budget': '6000', 'recurring': '1'},
            follow_redirects=True,
        )
        assert resp2.status_code == 200
        db.session.expire_all()
        row2 = Budget.query.filter_by(user_id=user.id, year_month='2026-06').first()
        assert row2 is not None and row2.amount == 6000.0
        assert db.session.get(User, user.id).recurring_budget == 6000.0


class TestPdfExportEscaping:
    """Tests ensuring ReportLab PDF export escapes user-supplied XML characters."""

    def test_pdf_export_escapes_xml_chars_in_description_and_email(
        self, auth_client, user
    ):
        from models import Expense

        u = db.session.get(User, user.id)
        u.email = 'alice&bob@test.com'
        db.session.add(Expense(
            user_id=user.id,
            date='2026-07-10',
            category='Shopping',
            amount=499.0,
            description='Coffee <b>bold & <online> snacks',
            payment_method='UPI',
        ))
        db.session.add(Expense(
            user_id=user.id,
            date='2026-07-11',
            category='Food',
            amount=120.0,
            description=('A' * 44) + '&Z',
            payment_method='Cash',
        ))
        db.session.commit()

        resp = auth_client.get('/export/pdf?month=2026-07')
        assert resp.status_code == 200
        assert resp.headers['Content-Type'] == 'application/pdf'
        assert resp.data.startswith(b'%PDF-')


class TestDashboardRecentTransactions:
    """Tests ensuring /dashboard shows the 5 newest transactions, not the 5 oldest."""

    def test_dashboard_displays_five_newest_expenses(self, auth_client, user):
        from models import Expense

        entries = [
            ('2026-01-01', 'Oldest-1'),
            ('2026-01-02', 'Oldest-2'),
            ('2026-01-03', 'Recent-3'),
            ('2026-01-04', 'Recent-4'),
            ('2026-01-05', 'Recent-5'),
            ('2026-01-07', 'Recent-6'),
            ('2026-01-07', 'Recent-7'),
        ]
        for dt, desc in entries:
            db.session.add(Expense(
                user_id=user.id,
                date=dt,
                category='Food',
                amount=50.0,
                description=desc,
                payment_method='UPI',
            ))
        db.session.commit()

        resp = auth_client.get('/dashboard')
        assert resp.status_code == 200
        html = resp.data.decode('utf-8')

        for label in ['Recent-7', 'Recent-6', 'Recent-5', 'Recent-4', 'Recent-3']:
            assert label in html
        assert 'Oldest-1' not in html
        assert 'Oldest-2' not in html
        assert html.index('Recent-7') < html.index('Recent-6') < html.index('Recent-5')


class TestDashboardAndServiceWorker:
    """Tests for dashboard asset 404 removal and sw.js fetch resilience."""

    def test_dashboard_does_not_reference_missing_chart_css(self, auth_client):
        resp = auth_client.get('/dashboard')
        assert resp.status_code == 200
        assert b'chart.umd.min.css' not in resp.data

    def test_sw_js_every_fetch_branch_resolves_to_valid_response_when_fetch_rejects(
        self, client
    ):
        import json
        import shutil
        import subprocess

        node = shutil.which('node')
        if not node:
            pytest.skip('Node.js not available to execute sw.js')

        sw_resp = client.get('/sw.js')
        assert sw_resp.status_code == 200
        sw_code = sw_resp.data.decode('utf-8')

        harness = r"""
const fs = require('fs');
const vm = require('vm');
const swCode = fs.readFileSync(0, 'utf8');

class FakeResponse {
  constructor(body = '', init = {}) {
    this.body = body;
    this.status = init.status ?? 200;
    this.statusText = init.statusText ?? 'OK';
    this.ok = this.status >= 200 && this.status < 300;
    this.headers = init.headers || {};
  }
  clone() { return new FakeResponse(this.body, { status: this.status, statusText: this.statusText, headers: this.headers }); }
}

const listeners = {};
const cacheStore = new Map([['/offline', new FakeResponse('OFFLINE_PAGE', { status: 200 })]]);

const context = {
  URL,
  Response: FakeResponse,
  console: { log() {}, warn() {}, error() {} },
  self: {
    addEventListener(type, fn) { listeners[type] = fn; },
    skipWaiting() {},
    clients: { claim() {} },
  },
  caches: {
    async open() { return { async addAll() {}, async put() {} }; },
    async keys() { return []; },
    async delete() { return true; },
    async match(req) {
      const key = typeof req === 'string' ? req : new URL(req.url).pathname;
      return cacheStore.get(key);
    },
  },
  fetch: async () => { throw new TypeError('Failed to fetch'); },
};

vm.runInNewContext(swCode, context);

async function dispatchFetch(url, { mode = 'cors', destination = '', accept = '*/*' } = {}) {
  let responded = null;
  const event = {
    request: {
      url,
      method: 'GET',
      mode,
      destination,
      headers: { get(name) { return name.toLowerCase() === 'accept' ? accept : null; } },
    },
    respondWith(promise) { responded = promise; },
  };
  listeners.fetch(event);
  const res = await responded;
  if (!(res instanceof FakeResponse)) {
    throw new Error(`Invalid non-Response returned for ${url}: ${String(res)}`);
  }
  return { status: res.status, body: res.body };
}

(async () => {
  const results = {
    staticMiss: await dispatchFetch('http://127.0.0.1/static/missing.css'),
    navOffline: await dispatchFetch('http://127.0.0.1/dashboard', { mode: 'navigate', destination: 'document', accept: 'text/html' }),
    nonNavDashboard: await dispatchFetch('http://127.0.0.1/dashboard', { mode: 'cors', accept: '*/*' }),
    manifestMiss: await dispatchFetch('http://127.0.0.1/manifest.json', { mode: 'cors', accept: '*/*' }),
  };
  cacheStore.clear();
  results.navOfflineNoCache = await dispatchFetch('http://127.0.0.1/dashboard', { mode: 'navigate', destination: 'document', accept: 'text/html' });
  console.log(JSON.stringify(results));
})().catch((err) => {
  console.error(err && err.stack ? err.stack : String(err));
  process.exit(1);
});
"""
        proc = subprocess.run(
            [node, '-e', harness],
            input=sw_code,
            text=True,
            capture_output=True,
            timeout=10,
        )
        assert proc.returncode == 0, f"sw.js rejected under offline fetch:\n{proc.stderr}"
        results = json.loads(proc.stdout)
        assert results['staticMiss']['status'] >= 400
        assert results['navOffline']['body'] == 'OFFLINE_PAGE'
        assert results['nonNavDashboard']['status'] >= 400
        assert results['manifestMiss']['status'] >= 400
        assert results['navOfflineNoCache']['status'] == 503


class TestLoggingPrivacy:
    """Tests ensuring auth logs never contain email addresses or raw submitted identifiers."""

    def test_register_logs_user_id_only(self, client, caplog):
        import logging

        with caplog.at_level(logging.INFO, logger='expenseflow'):
            resp = client.post(
                '/register',
                data={
                    'username': 'priv_user',
                    'email': 'priv_secret@example.com',
                    'password': 'password123',
                    'confirm_password': 'password123',
                },
                follow_redirects=False,
            )
        assert resp.status_code == 302
        created = User.query.filter_by(username='priv_user').first()
        assert created is not None
        assert f'New user registered: id={created.id}' in caplog.text
        assert 'priv_secret@example.com' not in caplog.text
        assert 'priv_user' not in caplog.text

    def test_login_and_logout_never_log_emails_or_submitted_identifiers(
        self, client, user, caplog
    ):
        import logging

        unknown_ident = 'mistyped_secret_identifier@example.com'
        with caplog.at_level(logging.INFO, logger='expenseflow'):
            client.post(
                '/login',
                data={'username': unknown_ident, 'password': 'wrong'},
                follow_redirects=False,
            )
            client.post(
                '/login',
                data={'username': user.email, 'password': 'wrong'},
                follow_redirects=False,
            )
            client.post(
                '/login',
                data={'username': user.email, 'password': 'password123'},
                follow_redirects=False,
            )
            client.get('/logout', follow_redirects=False)

        assert 'Login attempt for unknown user' in caplog.text
        assert f'Wrong password for user: id={user.id}' in caplog.text
        assert f'User logged in: id={user.id}' in caplog.text
        assert f'User logged out: id={user.id}' in caplog.text
        assert unknown_ident not in caplog.text
        assert user.email not in caplog.text
        assert user.username not in caplog.text


class TestDatabaseConfig:
    """Tests for PostgreSQL URI normalization, engine options, and test DB override."""

    @pytest.mark.parametrize(
        'raw_uri,expected_uri',
        [
            ('postgres://u:p@localhost:5432/expenseflow', 'postgresql+psycopg://u:p@localhost:5432/expenseflow'),
            ('postgresql://u:p@localhost:5432/expenseflow', 'postgresql+psycopg://u:p@localhost:5432/expenseflow'),
            ('  postgresql://u:p@db:5432/app  ', 'postgresql+psycopg://u:p@db:5432/app'),
            ('postgresql+psycopg://u:p@localhost:5432/app', 'postgresql+psycopg://u:p@localhost:5432/app'),
            ('sqlite:///expenses.db', 'sqlite:///expenses.db'),
        ],
    )
    def test_normalize_database_uri_rewrites_postgres_schemes(self, raw_uri, expected_uri):
        """Legacy postgres:// and bare postgresql:// URIs must normalize to postgresql+psycopg://."""
        from app import normalize_database_uri

        assert normalize_database_uri(raw_uri) == expected_uri

    def test_postgres_uri_sets_pool_pre_ping_and_normalizes_scheme(self, monkeypatch):
        """Production PostgreSQL config must normalize URI and enable pool_pre_ping even without psycopg installed."""
        monkeypatch.setenv('EXPENSEFLOW_SECRET_KEY', 'prod-secret-key-with-at-least-32-chars!!')
        monkeypatch.setenv('EXPENSEFLOW_DATABASE_URI', 'postgres://user:pass@db.internal:5432/expenseflow')
        monkeypatch.setattr(db, 'init_app', lambda _app: None)
        monkeypatch.setattr(db, 'create_all', lambda: None)

        pg_app = create_app('production')
        assert pg_app.config['SQLALCHEMY_DATABASE_URI'] == 'postgresql+psycopg://user:pass@db.internal:5432/expenseflow'
        assert pg_app.config.get('SQLALCHEMY_ENGINE_OPTIONS', {}).get('pool_pre_ping') is True

    def test_postgres_uri_loads_psycopg_dialect_when_installed(self, monkeypatch):
        """When optional psycopg is installed, create_app builds a psycopg-backed SQLAlchemy engine."""
        pytest.importorskip('psycopg')
        monkeypatch.setenv('EXPENSEFLOW_SECRET_KEY', 'prod-secret-key-with-at-least-32-chars!!')
        monkeypatch.setenv('EXPENSEFLOW_DATABASE_URI', 'postgres://user:pass@db.internal:5432/expenseflow')
        monkeypatch.setattr(db, 'create_all', lambda: None)

        pg_app = create_app('production')
        try:
            with pg_app.app_context():
                assert db.engine.dialect.driver == 'psycopg'
        finally:
            with pg_app.app_context():
                db.engine.dispose()

    def test_testing_mode_accepts_explicit_database_uri_and_ignores_prod_env(self, monkeypatch, tmp_path):
        """Testing mode keeps ignoring EXPENSEFLOW_DATABASE_URI while allowing explicit test DB override."""
        monkeypatch.setenv('EXPENSEFLOW_DATABASE_URI', 'postgres://should:not@be-used:5432/prod')

        default_test_app = create_app('testing')
        assert default_test_app.config['SQLALCHEMY_DATABASE_URI'] == 'sqlite:///:memory:'

        override_file = tmp_path / 'override_test.db'
        override_uri = f"sqlite:///{override_file.as_posix()}"
        override_app = create_app('testing', database_uri=override_uri)
        try:
            assert override_app.config['SQLALCHEMY_DATABASE_URI'] == override_uri
            with override_app.app_context():
                db.create_all()
                assert override_file.exists()
        finally:
            with override_app.app_context():
                db.engine.dispose()


class TestDependencyLockfiles:
    """Tests ensuring universal lockfiles exist, pin exact versions, and keep psycopg out of base installs."""

    @staticmethod
    def _package_lines(filename):
        from pathlib import Path
        root = Path(__file__).resolve().parent.parent
        lines = []
        for raw in (root / filename).read_text(encoding='utf-8').splitlines():
            stripped = raw.strip()
            if not stripped or stripped.startswith('#') or stripped.startswith('-'):
                continue
            lines.append(stripped)
        return lines

    def test_lockfiles_and_direct_inputs_exist(self):
        from pathlib import Path
        root = Path(__file__).resolve().parent.parent
        for name in (
            'requirements.in',
            'requirements.lock',
            'requirements-postgres.in',
            'requirements-postgres.lock',
        ):
            assert (root / name).is_file(), f'Missing dependency file: {name}'

    def test_base_lockfile_excludes_psycopg_and_pins_all_packages(self):
        base_lines = self._package_lines('requirements.lock')
        assert base_lines, 'requirements.lock must not be empty'
        for line in base_lines:
            pkg_spec = line.split(';', 1)[0].strip()
            assert '==' in pkg_spec, f'Unpinned requirement in requirements.lock: {line}'

        for fname in ('requirements.in', 'requirements.lock', 'requirements.txt'):
            joined = '\n'.join(self._package_lines(fname)).lower()
            assert 'psycopg' not in joined, f'psycopg must stay out of {fname}'

    def test_postgres_lockfile_pins_psycopg(self):
        pg_lines = self._package_lines('requirements-postgres.lock')
        assert pg_lines, 'requirements-postgres.lock must not be empty'
        names = set()
        for line in pg_lines:
            pkg_spec = line.split(';', 1)[0].strip()
            assert '==' in pkg_spec, f'Unpinned requirement in requirements-postgres.lock: {line}'
            names.add(pkg_spec.split('==', 1)[0].lower())
        assert 'psycopg' in names
        assert 'psycopg-binary' in names
