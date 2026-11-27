"""
test_statement_import.py — comprehensive tests for bank CSV statement import,
mapping configs, encoding/caps, dedupe, rules, review/confirm/undo atomicity,
cross-user isolation, CSRF/rate-limit/XSS/formula-injection security, and smoke test.
"""
from datetime import date as py_date
import io
import json
from pathlib import Path
import re
import shutil
import subprocess
import threading

import pytest
from sqlalchemy.exc import SQLAlchemyError
from werkzeug.serving import make_server

from app import create_app
from extensions import limiter
from models import CategoryRule, Expense, ImportBatch, ImportRow, User, db
from statement_import import (
    BANK_FORMATS,
    BankFormatConfig,
    MAX_IMPORT_FILE_BYTES,
    MAX_IMPORT_ROWS,
    StatementParseError,
    categorise_narration,
    parse_statement_amount_to_paise,
    parse_statement_csv,
    suggest_rule_pattern,
)
from utils import sanitize_csv_cell


REPO_ROOT = Path(__file__).resolve().parent.parent
SAMPLE_CSV_PATH = REPO_ROOT / 'static' / 'samples' / 'synthetic_indian_bank_statement.csv'


def _login(client, username='alice', password='password123'):
    return client.post(
        '/login',
        data={'username': username, 'password': password},
        follow_redirects=True,
    )


def _upload_csv(client, csv_bytes: bytes, *, filename='statement.csv', bank_profile='indian_standard', extra=None):
    data = {
        'bank_profile': bank_profile,
        'statement_file': (io.BytesIO(csv_bytes), filename),
    }
    if extra:
        data.update(extra)
    return client.post('/imports', data=data, content_type='multipart/form-data', follow_redirects=False)


def _extract_batch_id(resp) -> int:
    loc = resp.headers.get('Location', '')
    match = re.search(r'/imports/(\d+)', loc)
    assert match, f'Expected redirect to /imports/<id>, got {loc!r}'
    return int(match.group(1))


class TestMappingParserAndEncodings:
    """Tests for mapping configs, Indian bank sample fixture, encodings, caps, and Decimal paise."""

    def test_shipped_synthetic_indian_bank_sample_parses_expected_buckets(self):
        raw_bytes = SAMPLE_CSV_PATH.read_bytes()
        parsed = parse_statement_csv(
            raw_bytes,
            BANK_FORMATS['indian_standard'],
            today=py_date(2026, 11, 27),
        )
        assert parsed.preamble_rows_skipped == 5
        assert parsed.detected_encoding == 'utf-8'
        assert len(parsed.rows) == 12

        debits = [r for r in parsed.rows if r.row_status == 'ready']
        credits = [r for r in parsed.rows if r.row_status == 'skipped_credit']
        invalids = [r for r in parsed.rows if r.row_status == 'invalid']

        assert len(debits) == 9
        assert len(credits) == 2
        assert len(invalids) == 1

        # Exact paise parsing including Indian/grouped commas and quoted commas
        assert debits[0].txn_date == py_date(2026, 10, 1)
        assert debits[0].amount_paise == 45_850
        assert debits[0].payment_method == 'UPI'
        assert debits[2].description == 'UPI/DR/428300000003/METRO RAIL, BENGALURU/ICIC/metro@icici'
        assert debits[5].amount_paise == 125_075  # "1,250.75"

        # Two identical same-day debits receive distinct occurrence-aware fingerprints
        same_day = [r for r in debits if r.txn_date == py_date(2026, 10, 5)]
        assert len(same_day) == 2
        assert same_day[0].amount_paise == 4_000
        assert same_day[1].amount_paise == 4_000
        assert same_day[0].fingerprint != same_day[1].fingerprint

        # Credits preserved with exact paise for display, never imported as expenses
        assert [c.amount_paise for c in credits] == [4_500_000, 50_000]

        # Invalid row listed with line number & explicit reason
        assert invalids[0].line_number == 18
        assert '31/02/2026' in invalids[0].error_reason

    def test_signed_amount_and_alt_dash_configs_plus_custom_mapping(self, auth_client, user):
        signed_csv = (
            'Date,Description,Amount\n'
            '2026-10-01,Coffee Shop,-145.50\n'
            '2026-10-02,Refund,200.00\n'
            '2026-10-03,Lunch,-0.10\n'
        ).encode('utf-8')
        parsed_signed = parse_statement_csv(signed_csv, BANK_FORMATS['signed_iso'], today=py_date(2026, 11, 27))
        assert [r.row_status for r in parsed_signed.rows] == ['ready', 'skipped_credit', 'ready']
        assert [r.amount_paise for r in parsed_signed.rows if r.row_status == 'ready'] == [14_550, 10]

        alt_csv = (
            'Txn Date,Narration,Ref No,Debit,Credit\n'
            '01-10-2026,UPI/BLINKIT,R1,10.125,\n'
            '02-10-2026,SALARY,R2,,50000.00\n'
        ).encode('utf-8')
        parsed_alt = parse_statement_csv(alt_csv, BANK_FORMATS['indian_alt_dash'], today=py_date(2026, 11, 27))
        assert parsed_alt.rows[0].amount_paise == 1013  # ROUND_HALF_UP
        assert parsed_alt.rows[1].row_status == 'skipped_credit'

        # Custom mapping uploaded via HTTP route
        custom_csv = (
            'Bank Export Banner\n'
            'PostedOn,Memo,Spent,Received\n'
            '2026-10-11,METRO CARD TOPUP,"1,00,000.50",\n'
        ).encode('utf-8')
        resp = _upload_csv(
            auth_client,
            custom_csv,
            filename='custom.csv',
            bank_profile='custom',
            extra={
                'custom_date_column': 'PostedOn',
                'custom_date_format': 'yyyy-mm-dd',
                'custom_narration_column': 'Memo',
                'custom_amount_mode': 'split_columns',
                'custom_debit_column': 'Spent',
                'custom_credit_column': 'Received',
                'custom_preamble_rows': '1',
            },
        )
        assert resp.status_code == 302
        batch_id = _extract_batch_id(resp)
        row = ImportRow.query.filter_by(batch_id=batch_id).one()
        assert row.amount_paise == 10_000_050
        assert row.txn_date == py_date(2026, 10, 11)

    @pytest.mark.parametrize('encoding', ['utf-8-sig', 'utf-16', 'cp1252'])
    def test_encoding_handling_utf8_bom_utf16_and_cp1252(self, encoding):
        csv_text = (
            'Date,Description,Amount\r\n'
            '2026-10-05,Café — Snack – ₹99.50,-99.50\r\n'
        )
        if encoding == 'cp1252':
            csv_text = 'Date,Description,Amount\r\n2026-10-05,Caf\xe9 \x96 Snack,-99.50\r\n'
            raw = csv_text.encode('latin-1')
        else:
            raw = csv_text.encode(encoding)

        parsed = parse_statement_csv(raw, BANK_FORMATS['signed_iso'], today=py_date(2026, 11, 27))
        assert parsed.detected_encoding == encoding
        assert parsed.rows[0].amount_paise == 9950

    def test_statement_amount_decimal_parsing_precision_and_rejections(self):
        assert parse_statement_amount_to_paise('₹ 1,00,000.50') == (10_000_050, None)
        assert parse_statement_amount_to_paise('INR 1,234.56') == (123_456, None)
        assert parse_statement_amount_to_paise('10.125') == (1_013, None)
        assert parse_statement_amount_to_paise('-250.75', allow_signed=True) == (-25_075, None)
        assert parse_statement_amount_to_paise('-') == (None, None)

        for bad in ('nan', 'inf', '1e5', '١٠٠', 'abc', '1,2,3', '10000001'):
            val, err = parse_statement_amount_to_paise(bad)
            assert val is None and err is not None

        val_neg, err_neg = parse_statement_amount_to_paise('-50.00', allow_signed=False)
        assert val_neg is None and 'Negative amount' in err_neg

    def test_size_cap_row_cap_binary_rejection_and_no_disk_storage(self, auth_client, tmp_path):
        # 1. File size cap (> 2 MB)
        oversized = b'Date,Description,Amount\n' + (b'2026-10-01,Test,-10.00\n' * 100_000)
        assert len(oversized) > MAX_IMPORT_FILE_BYTES
        resp_big = _upload_csv(auth_client, oversized, bank_profile='signed_iso')
        assert resp_big.status_code == 302
        follow_big = auth_client.get('/imports')
        assert b'2 MB' in follow_big.data
        assert ImportBatch.query.count() == 0

        # 2. Row cap (> MAX_IMPORT_ROWS) while staying < 2 MB
        rows_over = ['Date,Description,Amount'] + [
            f'2026-10-01,Item {i},-1.00' for i in range(MAX_IMPORT_ROWS + 2)
        ]
        over_bytes = '\n'.join(rows_over).encode('utf-8')
        assert len(over_bytes) < MAX_IMPORT_FILE_BYTES
        with pytest.raises(StatementParseError, match=str(MAX_IMPORT_ROWS)):
            parse_statement_csv(over_bytes, BANK_FORMATS['signed_iso'], today=py_date(2026, 11, 27))

        # 3. Binary & non-CSV rejection
        resp_bin = _upload_csv(auth_client, b'%PDF-1.4\x00\x01\x02', filename='bad.csv', bank_profile='signed_iso')
        assert resp_bin.status_code == 302
        assert ImportBatch.query.count() == 0

        resp_ext = _upload_csv(auth_client, b'a,b\n1,2\n', filename='not_csv.txt', bank_profile='signed_iso')
        assert resp_ext.status_code == 302
        assert ImportBatch.query.count() == 0

        # 4. Valid CSV never writes uploaded file to disk in repo/instance
        before_files = {p.name for p in REPO_ROOT.iterdir()}
        unique_marker = 'never_stored_unique_marker_991122.csv'
        resp_ok = _upload_csv(
            auth_client,
            b'Date,Description,Amount\n2026-10-01,In memory only,-25.00\n',
            filename=unique_marker,
            bank_profile='signed_iso',
        )
        assert resp_ok.status_code == 302
        after_files = {p.name for p in REPO_ROOT.iterdir()}
        assert after_files == before_files
        assert not list(REPO_ROOT.rglob(unique_marker))

    def test_malformed_rows_are_listed_with_reasons_never_silently_dropped(self, auth_client, user):
        messy_csv = (
            'Metadata Line 1\n'
            'Date,Particulars,Chq./Ref.No.,Withdrawal (INR),Deposit (INR),Balance (INR)\n'
            '01/10/2026,VALID DEBIT,,100.00,,900.00\n'
            '31/02/2026,BAD CALENDAR DATE,,50.00,,850.00\n'
            '01/01/2099,FUTURE DATE,,50.00,,800.00\n'
            '02/10/2026,,,50.00,,750.00\n'
            '03/10/2026,NON NUMERIC DEBIT,,ABC,,750.00\n'
            '04/10/2026,BOTH DEBIT AND CREDIT,,10.00,20.00,760.00\n'
            '05/10/2026,ZERO AMOUNT,,0.00,0.00,760.00\n'
            '06/10/2026,MISSING COLS\n'
            '07/10/2026,EXTRA COLS,,10.00,,750.00,EXTRA_DATA\n'
            '08/10/2026,VALID CREDIT,,,500.00,1250.00\n'
        ).encode('utf-8')

        resp = _upload_csv(auth_client, messy_csv, bank_profile='indian_standard')
        assert resp.status_code == 302
        batch_id = _extract_batch_id(resp)

        # Staging never writes Expense rows
        assert Expense.query.filter_by(user_id=user.id).count() == 0

        rows = ImportRow.query.filter_by(batch_id=batch_id).order_by(ImportRow.line_number.asc()).all()
        assert len(rows) == 10  # zero data rows silently dropped
        invalids = [r for r in rows if r.row_status == 'invalid']
        assert len(invalids) == 8
        for inv in invalids:
            assert inv.error_reason

        review_html = auth_client.get(f'/imports/{batch_id}').data.decode('utf-8')
        assert 'Invalid Rows (8)' in review_html
        assert 'Skipped Credits / Deposits (1)' in review_html
        for inv in invalids:
            assert inv.error_reason.split("'")[0] in review_html

    def test_large_valid_csv_over_chunk_threshold_confirms_and_undoes(self, auth_client, user):
        # 600 rows exercises SQLite >500 bind-parameter chunking in _existing_import_fingerprints
        lines = ['Date,Description,Amount']
        for i in range(600):
            day = (i % 28) + 1
            lines.append(f'2026-10-{day:02d},Bulk Txn {i},-{10 + (i % 50)}.25')
        big_csv = '\n'.join(lines).encode('utf-8')

        resp = _upload_csv(auth_client, big_csv, filename='bulk_600.csv', bank_profile='signed_iso')
        assert resp.status_code == 302
        batch_id = _extract_batch_id(resp)
        assert Expense.query.filter_by(user_id=user.id).count() == 0

        confirm_resp = auth_client.post(f'/imports/{batch_id}/confirm', follow_redirects=True)
        assert confirm_resp.status_code == 200
        assert Expense.query.filter_by(user_id=user.id).count() == 600

        # Re-uploading same 600-row CSV dedupes all 600 across chunks
        resp2 = _upload_csv(auth_client, big_csv, filename='bulk_600.csv', bank_profile='signed_iso')
        batch_id2 = _extract_batch_id(resp2)
        batch2 = db.session.get(ImportBatch, batch_id2)
        assert batch2.already_imported_count == 600
        assert batch2.ready_count == 0

        # Undo removes all 600 cleanly
        undo_resp = auth_client.post(f'/imports/{batch_id}/undo', follow_redirects=True)
        assert undo_resp.status_code == 200
        assert Expense.query.filter_by(user_id=user.id).count() == 0


class TestDedupeAndRules:
    """Tests for idempotent dedupe, identical repeats, manual duplicate flags, and deterministic rules."""

    def test_idempotent_reupload_adds_nothing_while_identical_same_day_rows_stay_two(
        self, auth_client, user
    ):
        sample_bytes = SAMPLE_CSV_PATH.read_bytes()
        resp1 = _upload_csv(auth_client, sample_bytes, filename='october.csv', bank_profile='indian_standard')
        batch_id1 = _extract_batch_id(resp1)

        auth_client.post(f'/imports/{batch_id1}/confirm', follow_redirects=True)
        db.session.expire_all()
        expenses = Expense.query.filter_by(user_id=user.id).all()
        assert len(expenses) == 9  # 9 debits imported; 2 credits & 1 invalid skipped
        chai_expenses = [e for e in expenses if 'CHAI CORNER' in (e.description or '')]
        assert len(chai_expenses) == 2
        assert all(e.amount_paise == 4_000 for e in chai_expenses)

        # Re-upload exact same statement -> all 9 debits flagged already_imported; confirm adds 0
        resp2 = _upload_csv(auth_client, sample_bytes, filename='october.csv', bank_profile='indian_standard')
        batch_id2 = _extract_batch_id(resp2)
        batch2 = db.session.get(ImportBatch, batch_id2)
        assert batch2.already_imported_count == 9
        assert batch2.ready_count == 0

        auth_client.post(f'/imports/{batch_id2}/confirm', follow_redirects=True)
        assert Expense.query.filter_by(user_id=user.id).count() == 9

        # Overlapping statement containing the same two + a 3rd identical same-day CHAI CORNER imports ONLY the 3rd
        overlap_csv = (
            'Date,Particulars,Chq./Ref.No.,Withdrawal (INR),Deposit (INR),Balance (INR)\n'
            '05/10/2026,POS 4532XXXXXXXX0000 CHAI CORNER,,40.00,,49116.50\n'
            '05/10/2026,POS 4532XXXXXXXX0000 CHAI CORNER,,40.00,,49076.50\n'
            '05/10/2026,POS 4532XXXXXXXX0000 CHAI CORNER,,40.00,,49036.50\n'
        ).encode('utf-8')
        resp3 = _upload_csv(auth_client, overlap_csv, filename='overlap.csv', bank_profile='indian_standard')
        batch_id3 = _extract_batch_id(resp3)
        batch3 = db.session.get(ImportBatch, batch_id3)
        assert batch3.already_imported_count == 2
        assert batch3.ready_count == 1

        auth_client.post(f'/imports/{batch_id3}/confirm', follow_redirects=True)
        assert Expense.query.filter_by(user_id=user.id).count() == 10

    def test_manual_expense_duplicate_flag_lets_user_skip_or_import_anyway(self, auth_client, user):
        # User manually logged Swiggy ₹320.00 and ONE of the two ₹40.00 Chai Corner expenses
        m1 = Expense(
            user_id=user.id,
            date=py_date(2026, 10, 2),
            category='Food',
            amount_paise=32_000,
            description='Swiggy dinner',
            payment_method='UPI',
        )
        m2 = Expense(
            user_id=user.id,
            date=py_date(2026, 10, 6),  # +1 day settlement offset from 2026-10-05 CSV row
            category='Food',
            amount_paise=4_000,
            description='Chai',
            payment_method='Cash',
        )
        db.session.add_all([m1, m2])
        db.session.commit()

        sample_bytes = SAMPLE_CSV_PATH.read_bytes()
        resp = _upload_csv(auth_client, sample_bytes, filename='sample.csv', bank_profile='indian_standard')
        batch_id = _extract_batch_id(resp)

        batch = db.session.get(ImportBatch, batch_id)
        assert batch.duplicate_manual_count == 2  # Swiggy + 1 of the 2 Chai rows
        assert batch.ready_count == 7

        dup_rows = ImportRow.query.filter_by(batch_id=batch_id, row_status='possible_duplicate').all()
        assert len(dup_rows) == 2
        assert all(not r.include_on_confirm for r in dup_rows)

        review_html = auth_client.get(f'/imports/{batch_id}').data.decode('utf-8')
        assert 'Possible duplicate of manual expense' in review_html
        assert 'Swiggy dinner' in review_html

        # Confirm with defaults skips both flagged duplicates -> 2 manual + 7 imported = 9
        auth_client.post(f'/imports/{batch_id}/confirm', follow_redirects=True)
        assert Expense.query.filter_by(user_id=user.id).count() == 9

        # Now test "Import anyway": upload another CSV matching m1, explicitly include it
        csv_force = (
            'Date,Description,Amount\n'
            '2026-10-02,UPI-SWIGGY SECOND ORDER,-320.00\n'
        ).encode('utf-8')
        resp2 = _upload_csv(auth_client, csv_force, filename='force.csv', bank_profile='signed_iso')
        batch_id2 = _extract_batch_id(resp2)
        row2 = ImportRow.query.filter_by(batch_id=batch_id2).one()
        assert row2.row_status == 'possible_duplicate'

        auth_client.post(
            f'/imports/{batch_id2}/confirm',
            data={
                'form_submitted': '1',
                f'include_{row2.id}': '1',
                f'category_{row2.id}': 'Food',
                f'payment_method_{row2.id}': 'UPI',
                f'description_{row2.id}': 'Second Swiggy order',
            },
            follow_redirects=True,
        )
        assert Expense.query.filter_by(user_id=user.id).count() == 10

    def test_deterministic_rules_first_else_uncategorised_and_always_apply_saves_rule(
        self, auth_client, user
    ):
        assert suggest_rule_pattern('UPI/DR/428100000001/BLINKIT GROCERY/YESB/blinkit@yesb') == 'BLINKIT'

        # Without saved rules, all staged debit rows default to Uncategorised
        sample_bytes = SAMPLE_CSV_PATH.read_bytes()
        resp = _upload_csv(auth_client, sample_bytes, filename='rules_test.csv', bank_profile='indian_standard')
        batch_id = _extract_batch_id(resp)

        rows = ImportRow.query.filter_by(batch_id=batch_id, row_status='ready').order_by(ImportRow.line_number).all()
        assert all(r.category == 'Uncategorised' for r in rows)

        # User corrects row 0 (BLINKIT GROCERY) to Food with "always_apply" and clicks Save & Re-apply Rules
        blinkit_row = rows[0]
        form_data = {'form_submitted': '1'}
        for r in rows:
            form_data[f'include_{r.id}'] = '1'
            form_data[f'category_{r.id}'] = 'Food' if r.id == blinkit_row.id else r.category
            form_data[f'payment_method_{r.id}'] = r.payment_method
            form_data[f'description_{r.id}'] = r.description
        form_data[f'always_apply_{blinkit_row.id}'] = '1'
        form_data[f'rule_pattern_{blinkit_row.id}'] = 'BLINKIT'

        apply_resp = auth_client.post(f'/imports/{batch_id}/apply-rules', data=form_data, follow_redirects=True)
        assert apply_resp.status_code == 200

        db.session.expire_all()
        saved_rule = CategoryRule.query.filter_by(user_id=user.id, pattern_normalized='BLINKIT').first()
        assert saved_rule is not None and saved_rule.category == 'Food'

        # Both BLINKIT GROCERY and BLINKIT EXPRESS are now Food; others remain Uncategorised
        updated_rows = ImportRow.query.filter_by(batch_id=batch_id, row_status='ready').order_by(ImportRow.line_number).all()
        blinkit_cats = [r.category for r in updated_rows if 'BLINKIT' in r.raw_narration]
        other_cats = [r.category for r in updated_rows if 'BLINKIT' not in r.raw_narration]
        assert blinkit_cats == ['Food', 'Food']
        assert all(c == 'Uncategorised' for c in other_cats)

        # Longest-pattern-wins determinism: adding generic "UPI" -> Other still lets "BLINKIT" -> Food win
        auth_client.post('/imports/rules', data={'pattern': 'UPI', 'category': 'Other'}, follow_redirects=True)
        all_rules = CategoryRule.query.filter_by(user_id=user.id).all()
        assert categorise_narration('UPI/DR/999/BLINKIT MART', all_rules) == 'Food'
        assert categorise_narration('UPI/DR/999/UNKNOWN VENDOR', all_rules) == 'Other'

        # Confirming batch keeps Uncategorised expenses editable on /edit/<id>
        auth_client.post(f'/imports/{batch_id}/confirm', follow_redirects=True)
        uncat_exp = Expense.query.filter_by(user_id=user.id, category='Uncategorised').first()
        assert uncat_exp is not None
        edit_resp = auth_client.post(
            f'/edit/{uncat_exp.id}',
            data={
                'date': uncat_exp.date.isoformat(),
                'category': 'Uncategorised',
                'amount': '320.00',
                'description': uncat_exp.description,
                'payment_method': uncat_exp.payment_method,
            },
            follow_redirects=True,
        )
        assert edit_resp.status_code == 200
        assert b'Expense updated successfully!' in edit_resp.data

    def test_always_apply_directly_on_confirm_saves_rule_and_applies_to_batch_and_future(
        self, auth_client, user
    ):
        csv1 = (
            'Date,Description,Amount\n'
            '2026-10-01,UPI/ZOMATO ORDER 1,-200.00\n'
            '2026-10-02,UPI/ZOMATO ORDER 2,-150.00\n'
        ).encode('utf-8')
        resp1 = _upload_csv(auth_client, csv1, filename='zomato1.csv', bank_profile='signed_iso')
        batch_id1 = _extract_batch_id(resp1)
        rows = ImportRow.query.filter_by(batch_id=batch_id1).order_by(ImportRow.line_number).all()

        # Check always_apply directly on Confirm (without clicking Apply Rules first)
        auth_client.post(
            f'/imports/{batch_id1}/confirm',
            data={
                'form_submitted': '1',
                f'include_{rows[0].id}': '1',
                f'include_{rows[1].id}': '1',
                f'category_{rows[0].id}': 'Food',
                f'category_{rows[1].id}': 'Uncategorised',
                f'payment_method_{rows[0].id}': 'UPI',
                f'payment_method_{rows[1].id}': 'UPI',
                f'description_{rows[0].id}': rows[0].description,
                f'description_{rows[1].id}': rows[1].description,
                f'always_apply_{rows[0].id}': '1',
                f'rule_pattern_{rows[0].id}': 'ZOMATO',
            },
            follow_redirects=True,
        )
        assert CategoryRule.query.filter_by(user_id=user.id, pattern_normalized='ZOMATO').one().category == 'Food'
        exps = Expense.query.filter_by(user_id=user.id).all()
        assert len(exps) == 2
        assert all(e.category == 'Food' for e in exps)

        # Future upload automatically categorises ZOMATO -> Food
        csv2 = b'Date,Description,Amount\n2026-10-05,UPI/ZOMATO ORDER 3,-99.00\n'
        resp2 = _upload_csv(auth_client, csv2, filename='zomato2.csv', bank_profile='signed_iso')
        batch_id2 = _extract_batch_id(resp2)
        assert ImportRow.query.filter_by(batch_id=batch_id2).one().category == 'Food'


class TestAtomicityAndConcurrencySafety:
    """Tests for single-transaction confirm/undo atomicity, rollback, double-confirm, and re-upload after undo."""

    def test_confirm_and_undo_atomicity_rollbacks_on_commit_error_and_double_confirm_is_safe(
        self, auth_client, user, monkeypatch
    ):
        # Keep one pre-existing manual expense untouched across confirm failure & undo
        manual = Expense(
            user_id=user.id,
            date=py_date(2026, 9, 1),
            category='Home',
            amount_paise=100_000,
            description='Rent manual',
            payment_method='Net Banking',
        )
        db.session.add(manual)
        db.session.commit()

        sample_bytes = SAMPLE_CSV_PATH.read_bytes()
        resp = _upload_csv(auth_client, sample_bytes, filename='atomic.csv', bank_profile='indian_standard')
        batch_id = _extract_batch_id(resp)

        # Simulate DB error during confirm commit -> full rollback (batch stays staged, 0 imported expenses)
        def failing_commit():
            raise SQLAlchemyError('simulated confirm failure')

        with monkeypatch.context() as mp:
            mp.setattr(db.session, 'commit', failing_commit)
            r_fail_confirm = auth_client.post(f'/imports/{batch_id}/confirm', follow_redirects=True)
            assert r_fail_confirm.status_code == 200

        db.session.expire_all()
        assert db.session.get(ImportBatch, batch_id).status == 'staged'
        assert Expense.query.filter_by(user_id=user.id).count() == 1

        # Real confirm succeeds and sets import_batch_id on all 9 rows
        r_confirm = auth_client.post(f'/imports/{batch_id}/confirm', follow_redirects=True)
        assert r_confirm.status_code == 200
        db.session.expire_all()
        assert db.session.get(ImportBatch, batch_id).status == 'confirmed'
        assert Expense.query.filter_by(user_id=user.id).count() == 10

        # Double-confirm is safe (no duplicate rows, no 500)
        r_double = auth_client.post(f'/imports/{batch_id}/confirm', follow_redirects=True)
        assert r_double.status_code == 200
        assert b'already been confirmed' in r_double.data
        assert Expense.query.filter_by(user_id=user.id).count() == 10

        # Simulate DB failure on undo commit AFTER Expense.delete -> rollback restores ALL 9 expenses + confirmed status
        with monkeypatch.context() as mp:
            mp.setattr(db.session, 'commit', failing_commit)
            r_fail_undo = auth_client.post(f'/imports/{batch_id}/undo', follow_redirects=True)
            assert r_fail_undo.status_code == 200

        db.session.expire_all()
        assert db.session.get(ImportBatch, batch_id).status == 'confirmed'
        assert Expense.query.filter_by(user_id=user.id).count() == 10

        # Real undo removes all 9 imported rows in one transaction, keeping the manual expense
        r_undo = auth_client.post(f'/imports/{batch_id}/undo', follow_redirects=True)
        assert r_undo.status_code == 200
        db.session.expire_all()
        assert db.session.get(ImportBatch, batch_id).status == 'undone'
        remaining = Expense.query.filter_by(user_id=user.id).all()
        assert [e.id for e in remaining] == [manual.id]

        # Double-undo & confirm-after-undo are safe no-ops
        r_undo2 = auth_client.post(f'/imports/{batch_id}/undo', follow_redirects=True)
        assert r_undo2.status_code == 200
        r_reconfirm = auth_client.post(f'/imports/{batch_id}/confirm', follow_redirects=True)
        assert r_reconfirm.status_code == 200
        assert Expense.query.filter_by(user_id=user.id).count() == 1

        # Re-uploading after undo allows importing the statement again cleanly
        resp_again = _upload_csv(auth_client, sample_bytes, filename='atomic_again.csv', bank_profile='indian_standard')
        batch_again = _extract_batch_id(resp_again)
        assert db.session.get(ImportBatch, batch_again).ready_count == 9

    def test_two_staged_batches_of_same_csv_second_confirm_adds_zero_duplicates(
        self, auth_client, user
    ):
        payload = b'Date,Description,Amount\n2026-10-01,Two Tab Txn,-75.00\n'
        b1 = _extract_batch_id(_upload_csv(auth_client, payload, filename='tab1.csv', bank_profile='signed_iso'))
        b2 = _extract_batch_id(_upload_csv(auth_client, payload, filename='tab2.csv', bank_profile='signed_iso'))

        auth_client.post(f'/imports/{b1}/confirm', follow_redirects=True)
        assert Expense.query.filter_by(user_id=user.id).count() == 1

        r2 = auth_client.post(f'/imports/{b2}/confirm', follow_redirects=True)
        assert r2.status_code == 200
        assert Expense.query.filter_by(user_id=user.id).count() == 1
        assert db.session.get(ImportBatch, b2).imported_count == 0


class TestSecurityIsolationAndExports:
    """Tests for cross-user isolation, CSRF, upload rate-limiting, XSS escaping, CSV formula injection, and log privacy."""

    def test_cross_user_isolation_and_unauthenticated_redirects(self, client, user, user2):
        # Unauthenticated redirects
        assert client.get('/imports', follow_redirects=False).status_code == 302
        assert client.get('/export/csv', follow_redirects=False).status_code == 302

        # Alice uploads a batch and saves a rule
        _login(client, 'alice', 'password123')
        resp = _upload_csv(client, SAMPLE_CSV_PATH.read_bytes(), filename='alice.csv', bank_profile='indian_standard')
        alice_batch_id = _extract_batch_id(resp)
        client.post('/imports/rules', data={'pattern': 'BLINKIT', 'category': 'Food'})
        alice_rule = CategoryRule.query.filter_by(user_id=user.id).one()

        # Log in as Bob -> all routes targeting Alice's batch/rule return 404
        client.get('/logout')
        _login(client, 'bob', 'password123')
        assert client.get(f'/imports/{alice_batch_id}').status_code == 404
        assert client.post(f'/imports/{alice_batch_id}/apply-rules').status_code == 404
        assert client.post(f'/imports/{alice_batch_id}/confirm').status_code == 404
        assert client.post(f'/imports/{alice_batch_id}/undo').status_code == 404
        assert client.post(f'/imports/{alice_batch_id}/discard').status_code == 404
        assert client.post(f'/imports/rules/{alice_rule.id}/delete').status_code == 404

        # Alice confirms; Bob uploading exact same CSV is NOT blocked by Alice's fingerprints
        client.get('/logout')
        _login(client, 'alice', 'password123')
        client.post(f'/imports/{alice_batch_id}/confirm')
        assert Expense.query.filter_by(user_id=user.id).count() == 9

        client.get('/logout')
        _login(client, 'bob', 'password123')
        resp_bob = _upload_csv(client, SAMPLE_CSV_PATH.read_bytes(), filename='bob.csv', bank_profile='indian_standard')
        bob_batch_id = _extract_batch_id(resp_bob)
        assert db.session.get(ImportBatch, bob_batch_id).ready_count == 9
        client.post(f'/imports/{bob_batch_id}/confirm')
        assert Expense.query.filter_by(user_id=user2.id).count() == 9

    def test_csrf_enforced_on_all_import_post_routes(self, app, user):
        app.config['WTF_CSRF_ENABLED'] = False
        c = app.test_client()
        _login(c, 'alice', 'password123')
        resp = _upload_csv(
            c,
            b'Date,Description,Amount\n2026-10-01,Test,-10.00\n',
            bank_profile='signed_iso',
        )
        batch_id = _extract_batch_id(resp)

        app.config['WTF_CSRF_ENABLED'] = True
        app.config['WTF_CSRF_CHECK_DEFAULT'] = True
        try:
            assert _upload_csv(c, b'Date,Description,Amount\n2026-10-01,X,-10.00\n', bank_profile='signed_iso').status_code == 400
            assert c.post(f'/imports/{batch_id}/apply-rules').status_code == 400
            assert c.post(f'/imports/{batch_id}/confirm').status_code == 400
            assert c.post(f'/imports/{batch_id}/undo').status_code == 400
            assert c.post(f'/imports/{batch_id}/discard').status_code == 400
            assert c.post('/imports/rules', data={'pattern': 'X', 'category': 'Food'}).status_code == 400
        finally:
            app.config['WTF_CSRF_ENABLED'] = False
            app.config['WTF_CSRF_CHECK_DEFAULT'] = False

    def test_upload_rate_limiting_returns_429_after_limit(self, tmp_path):
        db_file = tmp_path / 'ratelimit.db'
        rl_app = create_app('development', database_uri=f"sqlite:///{db_file.as_posix()}")
        rl_app.config['WTF_CSRF_ENABLED'] = False
        try:
            with rl_app.app_context():
                db.create_all()
                u = User(username='rluser', email='rl@example.com')
                u.set_password('password123')
                db.session.add(u)
                db.session.commit()

            limiter.reset()
            c = rl_app.test_client()
            _login(c, 'rluser', 'password123')
            csv_payload = b'Date,Description,Amount\n2026-10-01,Coffee,-25.00\n'
            for _ in range(10):
                r = _upload_csv(c, csv_payload, bank_profile='signed_iso')
                assert r.status_code == 302
            r_blocked = _upload_csv(c, csv_payload, bank_profile='signed_iso')
            assert r_blocked.status_code == 429
            assert b'Too many statement uploads' in r_blocked.data
        finally:
            limiter.reset()
            limiter.enabled = False
            with rl_app.app_context():
                db.engine.dispose()

    def test_untrusted_csv_xss_escaped_and_formula_injection_neutralized_on_exports(
        self, auth_client, user
    ):
        xss_filename = '<script>alert(9)</script>.csv'
        evil_csv = (
            'Date,Description,Amount\n'
            '2026-10-01,"<script>alert(1)</script> & =CMD|\'/C calc\'!A0",-111.00\n'
            '2026-10-02,"=SUM(1+1)",-22.00\n'
            '2026-10-03,"+1+1",-33.00\n'
            '2026-10-04,"-2+3",-44.00\n'
            '2026-10-05,"@SUM(A1:A2)",-55.00\n'
            '2026-10-06,"  =1+1",-66.00\n'
            '2026-10-07,"\t=1+1",-77.00\n'
        ).encode('utf-8')

        resp = _upload_csv(auth_client, evil_csv, filename=xss_filename, bank_profile='signed_iso')
        batch_id = _extract_batch_id(resp)

        review_html = auth_client.get(f'/imports/{batch_id}').data.decode('utf-8')
        assert '<script>alert(1)</script>' not in review_html
        assert '<script>alert(9)</script>' not in review_html
        assert '&lt;script&gt;alert(1)&lt;/script&gt;' in review_html

        auth_client.post(f'/imports/{batch_id}/confirm', follow_redirects=True)

        view_html = auth_client.get('/view').data.decode('utf-8')
        assert '<script>alert(1)</script>' not in view_html
        assert '&lt;script&gt;alert(1)&lt;/script&gt;' in view_html

        pdf_resp = auth_client.get('/export/pdf?month=2026-10')
        assert pdf_resp.status_code == 200
        assert pdf_resp.data.startswith(b'%PDF-')

        # Also add a manual expense retaining leading whitespace before '=' to verify export prefixing
        db.session.add(Expense(
            user_id=user.id,
            date=py_date(2026, 10, 8),
            category='Other',
            amount_paise=8800,
            description='  =IMPORTXML("http://bad")',
            payment_method='Cash',
        ))
        db.session.commit()

        # Server-side CSV export neutralizes every formula prefix
        csv_resp = auth_client.get('/export/csv')
        assert csv_resp.status_code == 200
        csv_body = csv_resp.data.decode('utf-8')
        assert "'=SUM(1+1)" in csv_body
        assert "'+1+1" in csv_body
        assert "'-2+3" in csv_body
        assert "'@SUM(A1:A2)" in csv_body
        assert "'  =IMPORTXML" in csv_body
        assert "'=1+1" in csv_body

        # Python helper unit check
        assert sanitize_csv_cell("=1+1") == "'=1+1"
        assert sanitize_csv_cell("  +1+1") == "'  +1+1"
        assert sanitize_csv_cell("Normal coffee") == "Normal coffee"

        # Verify client-side csvSafeCell in templates/view_expense.html via Node.js if available
        node = shutil.which('node')
        if node:
            tpl = (REPO_ROOT / 'templates' / 'view_expense.html').read_text(encoding='utf-8')
            match = re.search(r'function csvSafeCell\(val\)\s*\{.*?^\s{4}\}', tpl, re.MULTILINE | re.DOTALL)
            assert match, 'csvSafeCell helper missing from view_expense.html'
            js_code = match.group(0) + """
const samples = ['=1+1', '+cmd', '-1+2', '@SUM(1)', '  =1+1', '\\t=1+1', 'Coffee "Special"'];
console.log(JSON.stringify(samples.map(csvSafeCell)));
"""
            proc = subprocess.run([node, '-e', js_code], capture_output=True, text=True, timeout=10)
            assert proc.returncode == 0, proc.stderr
            out = json.loads(proc.stdout)
            assert out == [
                '"\'=1+1"',
                '"\'+cmd"',
                '"\'-1+2"',
                '"\'@SUM(1)"',
                '"\'  =1+1"',
                '"\'\t=1+1"',
                '"Coffee ""Special"""',
            ]

    def test_import_logs_never_leak_csv_narrations_or_filenames(self, auth_client, user, caplog):
        import logging

        secret_filename = 'SECRET_BANK_ACCT_9876543210.csv'
        secret_narr = 'UPI/SECRET_MERCHANT_99887766/CONFIDENTIAL_NOTE'
        payload = f'Date,Description,Amount\n2026-10-01,{secret_narr},-42.00\n'.encode('utf-8')

        with caplog.at_level(logging.INFO, logger='expenseflow'):
            resp = _upload_csv(auth_client, payload, filename=secret_filename, bank_profile='signed_iso')
            batch_id = _extract_batch_id(resp)
            auth_client.post(f'/imports/{batch_id}/confirm')
            auth_client.post(f'/imports/{batch_id}/undo')

        assert secret_filename not in caplog.text
        assert secret_narr not in caplog.text
        assert 'CONFIDENTIAL_NOTE' not in caplog.text
        assert f'user={user.id}' in caplog.text


class TestSmokeTestEndToEnd:
    """Execute scripts/smoke_test.py (including import -> confirm -> undo) against a live local HTTP server."""

    def test_smoke_test_script_runs_full_flow_including_import_confirm_undo(self, tmp_path):
        from scripts.smoke_test import run_smoke_test

        db_file = tmp_path / 'smoke.db'
        smoke_app = create_app('development', database_uri=f"sqlite:///{db_file.as_posix()}")
        with smoke_app.app_context():
            db.create_all()
            u = User(username='smokeuser', email='smoke@example.com')
            u.set_password('SmokePass123!')
            db.session.add(u)
            db.session.commit()

        limiter.reset()
        server = make_server('127.0.0.1', 0, smoke_app)
        port = server.server_port
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            exit_code = run_smoke_test(f'http://127.0.0.1:{port}', 'smokeuser', 'SmokePass123!')
            assert exit_code == 0
            with smoke_app.app_context():
                # Manual expense cleaned up + imported batch undone -> 0 expenses remaining
                assert Expense.query.count() == 0
                batch = ImportBatch.query.one()
                assert batch.status == 'undone'
        finally:
            limiter.reset()
            limiter.enabled = False
            server.shutdown()
            server.server_close()
            with smoke_app.app_context():
                db.engine.dispose()
