"""
test_regressions.py — Stage 1A security and correctness regression tests.

Each test class targets a specific bug or hardening fix and was written
to fail before the corresponding fix was applied.
"""
import pytest

import app as app_module
from app import create_app
from models import db


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
            'dev-only-fallback-change-in-prod-!@#',
        ],
    )
    def test_production_rejects_blank_short_or_fallback_secret_key(
        self, monkeypatch, tmp_path, bad_key
    ):
        """create_app('production') must reject blank, <32-char, or fallback keys."""
        monkeypatch.setenv('EXPENSEFLOW_SECRET_KEY', bad_key)
        monkeypatch.setenv(
            'EXPENSEFLOW_DATABASE_URI',
            f"sqlite:///{(tmp_path / 'prod_bad_key.db').as_posix()}",
        )

        with pytest.raises(RuntimeError, match='EXPENSEFLOW_SECRET_KEY'):
            create_app('production')

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

