#!/usr/bin/env python3
"""
End-to-end smoke test for ExpenseFlow using the Python standard library only.

Usage:
    SMOKE_USER=<username> SMOKE_PASS=<password> python scripts/smoke_test.py http://127.0.0.1:5000
"""
import argparse
import email.utils
import html.parser
import http.cookiejar
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone


class _FormParser(html.parser.HTMLParser):
    """Minimal stdlib HTML parser to extract CSRF tokens and profile form defaults."""

    def __init__(self):
        super().__init__()
        self.csrf_tokens = []
        self.inputs = {}

    def handle_starttag(self, tag, attrs):
        if tag != 'input':
            return
        attr_map = dict(attrs)
        name = attr_map.get('name')
        if not name:
            return
        value = attr_map.get('value', '')
        if name == 'csrf_token' and value:
            self.csrf_tokens.append(value)
        if name not in self.inputs:
            self.inputs[name] = value


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Prevent automatic redirect following so 302 Location and Set-Cookie can be inspected."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class _SmokeClient:
    """Thin urllib + CookieJar client supporting both raw-302 and redirect-following calls."""

    def __init__(self, base_url):
        self.base_url = base_url.rstrip('/')
        self.scheme = urllib.parse.urlparse(self.base_url).scheme.lower()
        self.jar = http.cookiejar.CookieJar()
        self.cookie_proc = urllib.request.HTTPCookieProcessor(self.jar)
        self.follow_opener = urllib.request.build_opener(self.cookie_proc)
        self.raw_opener = urllib.request.build_opener(self.cookie_proc, _NoRedirect())

    def url(self, path):
        if path.startswith('http://') or path.startswith('https://'):
            return path
        return f'{self.base_url}{path}'

    def get(self, path, follow_redirects=True):
        req = urllib.request.Request(self.url(path), method='GET')
        opener = self.follow_opener if follow_redirects else self.raw_opener
        try:
            with opener.open(req, timeout=20) as resp:
                return resp.status, dict(resp.headers.items()), resp.headers.get_all('Set-Cookie') or [], resp.read()
        except urllib.error.HTTPError as err:
            return err.code, dict(err.headers.items()), err.headers.get_all('Set-Cookie') or [], err.read()

    def post(self, path, data, referer_path=None, follow_redirects=True):
        encoded = urllib.parse.urlencode(data).encode('utf-8')
        headers = {'Content-Type': 'application/x-www-form-urlencoded'}
        if referer_path is not None:
            headers['Referer'] = self.url(referer_path)
        req = urllib.request.Request(self.url(path), data=encoded, headers=headers, method='POST')
        opener = self.follow_opener if follow_redirects else self.raw_opener
        try:
            with opener.open(req, timeout=20) as resp:
                return resp.status, dict(resp.headers.items()), resp.headers.get_all('Set-Cookie') or [], resp.read()
        except urllib.error.HTTPError as err:
            return err.code, dict(err.headers.items()), err.headers.get_all('Set-Cookie') or [], err.read()


def _parse_form(html_bytes):
    parser = _FormParser()
    parser.feed(html_bytes.decode('utf-8', errors='replace'))
    return parser


def _first_csrf(html_bytes):
    tokens = _parse_form(html_bytes).csrf_tokens
    if not tokens:
        raise RuntimeError('Missing csrf_token hidden input in HTML form')
    return tokens[0]


def _cookie_attrs(set_cookie_headers):
    session_headers = [h for h in set_cookie_headers if h.startswith('session=')]
    if not session_headers:
        return None
    parts = [p.strip().lower() for p in session_headers[-1].split(';')[1:]]
    return set(parts)


def _server_today(headers):
    raw_date = headers.get('Date') or headers.get('date')
    if raw_date:
        try:
            return email.utils.parsedate_to_datetime(raw_date).strftime('%Y-%m-%d')
        except (TypeError, ValueError):
            pass
    return datetime.now(timezone.utc).strftime('%Y-%m-%d')


def _extract_expense_ids(view_html_bytes):
    text = view_html_bytes.decode('utf-8', errors='replace')
    return [int(m) for m in re.findall(r'/delete/(\d+)', text)]


def run_smoke_test(base_url, smoke_user, smoke_pass):
    client = _SmokeClient(base_url)
    results = []
    created_expense_id = None

    def record(name, ok, detail=''):
        status = 'PASS' if ok else 'FAIL'
        suffix = f' ({detail})' if detail else ''
        print(f'[{status}] {name}{suffix}')
        results.append(ok)
        return ok

    try:
        # 1. Login page returns 200
        status, headers, _, body = client.get('/login')
        login_csrf = None
        try:
            login_csrf = _first_csrf(body)
        except RuntimeError:
            pass
        record('GET /login returns 200 with CSRF token', status == 200 and bool(login_csrf), f'status={status}')
        if not login_csrf:
            return 1

        server_date = _server_today(headers)

        # 2. Wrong password vs unknown username return identical generic response (not /register)
        st_wrong, hdr_wrong, _, _ = client.post(
            '/login',
            {'username': smoke_user, 'password': f'{smoke_pass}-incorrect', 'csrf_token': login_csrf},
            referer_path='/login',
            follow_redirects=False,
        )
        loc_wrong = hdr_wrong.get('Location', '')
        _, _, _, page_wrong = client.get(loc_wrong or '/login')
        csrf_after_wrong = _first_csrf(page_wrong)

        st_unk, hdr_unk, _, _ = client.post(
            '/login',
            {'username': 'definitely_nonexistent_smoke_user_999', 'password': 'wrong-password', 'csrf_token': csrf_after_wrong},
            referer_path='/login',
            follow_redirects=False,
        )
        loc_unk = hdr_unk.get('Location', '')
        _, _, _, page_unk = client.get(loc_unk or '/login')

        wrong_txt = page_wrong.decode('utf-8', errors='replace')
        unk_txt = page_unk.decode('utf-8', errors='replace')
        generic_msg = 'Invalid username/email or password.'
        enum_ok = (
            st_wrong == 302
            and st_unk == 302
            and urllib.parse.urlparse(loc_wrong).path == '/login'
            and urllib.parse.urlparse(loc_unk).path == '/login'
            and '/register' not in loc_unk
            and generic_msg in wrong_txt
            and generic_msg in unk_txt
        )
        record('Wrong password and unknown user give identical generic response', enum_ok)

        # 3. Login succeeds + Set-Cookie attributes check
        login_csrf_valid = _first_csrf(page_unk)
        st_login, hdr_login, login_cookies, _ = client.post(
            '/login',
            {'username': smoke_user, 'password': smoke_pass, 'csrf_token': login_csrf_valid},
            referer_path='/login',
            follow_redirects=False,
        )
        loc_login = hdr_login.get('Location', '')
        attrs = _cookie_attrs(login_cookies)
        login_ok = st_login == 302 and urllib.parse.urlparse(loc_login).path == '/dashboard'
        record('POST /login succeeds and redirects to /dashboard', login_ok, f'status={st_login}')

        cookie_ok = attrs is not None and 'httponly' in attrs and 'samesite=lax' in attrs
        if client.scheme == 'https':
            cookie_ok = cookie_ok and ('secure' in attrs)
        record(
            f'Login Set-Cookie flags valid for {client.scheme}://',
            cookie_ok,
            f'attrs={sorted(attrs) if attrs else None}',
        )
        if not login_ok:
            return 1

        # 4. Dashboard loads
        st_dash, _, _, dash_body = client.get('/dashboard')
        dash_txt = dash_body.decode('utf-8', errors='replace')
        record('GET /dashboard loads (200)', st_dash == 200 and 'Monthly Budget' in dash_txt, f'status={st_dash}')

        # 5. Add expense with XML-special description, verify presence, track ID for cleanup
        _, _, _, view_before = client.get('/view')
        before_ids = set(_extract_expense_ids(view_before))

        _, _, _, add_form = client.get('/add')
        add_csrf = _first_csrf(add_form)
        special_desc = 'H&M <b>x</b> & snacks'
        st_add, _, _, _ = client.post(
            '/add',
            {
                'date': server_date,
                'amount': '123.45',
                'category': 'Shopping',
                'payment_method': 'UPI',
                'description': special_desc,
                'csrf_token': add_csrf,
            },
            referer_path='/add',
            follow_redirects=True,
        )
        _, _, _, view_after = client.get('/view')
        after_ids = set(_extract_expense_ids(view_after))
        new_ids = sorted(after_ids - before_ids)
        if new_ids:
            created_expense_id = new_ids[-1]
        view_after_txt = view_after.decode('utf-8', errors='replace')
        add_ok = (
            st_add == 200
            and created_expense_id is not None
            and 'H&amp;M &lt;b&gt;x&lt;/b&gt; &amp; snacks' in view_after_txt
        )
        record('POST /add persists XML-escaped expense', add_ok, f'id={created_expense_id}')

        # 6. /export/pdf returns valid PDF header
        ym = server_date[:7]
        st_pdf, hdr_pdf, _, pdf_body = client.get(f'/export/pdf?month={ym}')
        ct_pdf = hdr_pdf.get('Content-Type', '')
        pdf_ok = st_pdf == 200 and 'application/pdf' in ct_pdf and pdf_body.startswith(b'%PDF-')
        record('GET /export/pdf returns %PDF- document', pdf_ok, f'status={st_pdf}, bytes={len(pdf_body)}')

        # 7. /set_budget valid succeeds and "abc" gives validation message (not 500)
        _, _, _, bform = client.get('/set_budget')
        b_csrf = _first_csrf(bform)
        st_b_ok, _, _, b_ok_body = client.post(
            '/set_budget',
            {
                'month': server_date[5:7],
                'year': server_date[:4],
                'budget': '5000',
                'csrf_token': b_csrf,
            },
            referer_path='/set_budget',
            follow_redirects=True,
        )
        b_ok_txt = b_ok_body.decode('utf-8', errors='replace')
        record(
            'POST /set_budget with valid amount succeeds',
            st_b_ok == 200 and 'updated successfully' in b_ok_txt,
            f'status={st_b_ok}',
        )

        _, _, _, bform2 = client.get('/set_budget')
        b_csrf2 = _first_csrf(bform2)
        st_b_bad, _, _, b_bad_body = client.post(
            '/set_budget',
            {
                'month': server_date[5:7],
                'year': server_date[:4],
                'budget': 'abc',
                'csrf_token': b_csrf2,
            },
            referer_path='/set_budget',
            follow_redirects=True,
        )
        b_bad_txt = b_bad_body.decode('utf-8', errors='replace')
        record(
            'POST /set_budget with "abc" returns validation message (not 500)',
            st_b_bad == 200 and 'valid budget amount' in b_bad_txt,
            f'status={st_b_bad}',
        )

        # 8. /edit_profile rejects email change without current password
        _, _, _, pform = client.get('/edit_profile')
        parsed_prof = _parse_form(pform)
        orig_username = parsed_prof.inputs.get('username', smoke_user)
        orig_email = parsed_prof.inputs.get('email', '')
        orig_avatar = parsed_prof.inputs.get('avatar', 'avatar_1')
        p_csrf = parsed_prof.csrf_tokens[0]

        st_ep, _, _, ep_body = client.post(
            '/edit_profile',
            {
                'username': orig_username,
                'email': 'smoke_blocked_change@example.com',
                'avatar': orig_avatar,
                'current_password': '',
                'new_password': '',
                'confirm_password': '',
                'csrf_token': p_csrf,
            },
            referer_path='/edit_profile',
            follow_redirects=True,
        )
        ep_txt = ep_body.decode('utf-8', errors='replace')
        _, _, _, pform_after = client.get('/edit_profile')
        after_email = _parse_form(pform_after).inputs.get('email', '')
        ep_ok = (
            st_ep == 200
            and 'Current password is required' in ep_txt
            and after_email == orig_email
        )
        record('POST /edit_profile rejects email change without current password', ep_ok)

    finally:
        # 9. Always delete the created test expense
        if created_expense_id is not None:
            try:
                _, _, _, view_cleanup = client.get('/view')
                del_csrf = _first_csrf(view_cleanup)
                st_del, _, _, view_after_del = client.post(
                    f'/delete/{created_expense_id}',
                    {'csrf_token': del_csrf},
                    referer_path='/view',
                    follow_redirects=True,
                )
                remaining = set(_extract_expense_ids(view_after_del))
                cleanup_ok = st_del == 200 and created_expense_id not in remaining
                record(f'Cleanup deletes test expense id={created_expense_id}', cleanup_ok)
            except Exception as exc:
                record(f'Cleanup deletes test expense id={created_expense_id}', False, str(exc))

    return 0 if results and all(results) else 1


def main():
    parser = argparse.ArgumentParser(description='ExpenseFlow stdlib smoke test')
    parser.add_argument('base_url', help='Base URL, e.g. http://127.0.0.1:5000')
    args = parser.parse_args()

    smoke_user = os.environ.get('SMOKE_USER', '').strip()
    smoke_pass = os.environ.get('SMOKE_PASS', '')
    if not smoke_user or not smoke_pass:
        print('[FAIL] SMOKE_USER and SMOKE_PASS environment variables must be set.', file=sys.stderr)
        sys.exit(2)

    sys.exit(run_smoke_test(args.base_url, smoke_user, smoke_pass))


if __name__ == '__main__':
    main()
