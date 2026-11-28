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

    def test_sbi_yono_bom_multiline_details_repeated_header_and_footer_termination(
        self, auth_client, user
    ):
        # 100% synthetic fixture modeled on SBI / YONO Excel Save-As CSV export structure
        sbi_csv = (
            '\ufeffUncleared Amount  :  0.00,,,,,\n'
            'MOD Bal   :  0.00,,,,,\n'
            'Lien  :  0.00,,,,,\n'
            'Limit  :  0.00,,,,,\n'
            ',,,,,\n'
            ',Currency  :  INR,,,,\n'
            '"\t\t\t",,,,,\n'
            '"\t\t\t",,,,,\n'
            ',,,,,\n'
            'Date,Details,Ref No/Cheque No,Debit,Credit,Balance\n'
            '01-05-2026," DEP TFR   UPI/CR/900000000001/SYNTHETIC DEPOSIT/UTIB/demo\n'
            ' user@okaxis   000000000001 AT 00000 DEMO BRANCH",,,2500,5000\n'
            '03-05-2026," WDL TFR   UPI/DR/900000000002/FAKE MART/YESB/fake\n'
            ' mart@ybl   000000000002 AT 00000 DEMO BRANCH",,186.8,,4813.2\n'
            '05-05-2026," WDL TFR   UPI/DR/900000000003/SAMPLE BISTRO/UTIB/sam\n'
            ' ple@okaxis   000000000003 AT 00000 DEMO BRANCH",,"1,250.50",,3562.7\n'
            'Date,Details,Ref No/Cheque No,Debit,Credit,Balance\n'
            '10-05-2026," INTERES\n T CREDIT",,,18,3580.7\n'
            '31-02-2026," WDL TFR   CORRUPT DATE ROW BEFORE FOOTER",,50,,3530.7\n'
            ',,,,,\n'
            'Statement Summary : 01-05-2026  To  31-05-2026,,,,,\n'
            'Brought Forward (₹),Dr Count,Cr Count,Total Debits (₹),Total Credits (₹),Closing Balance (₹)\n'
            '"2,500.00CR",2,2,"1,437.30","2,518.00",3580.70CR\n'
            ',,,,,\n'
            '"Please do not share your ATM, Debit/Credit Card number, PIN, OTP, Username or Password with\n'
            'anyone via email, SMS, phone call, or any other medium.",,,,,\n'
            '"If your account is operated by a Power of Attorney holder, please review the transactions.",,,,,\n'
            'This is a computer generated statement and does not require a signature.,,,,,\n'
        ).encode('utf-8')

        parsed = parse_statement_csv(sbi_csv, BANK_FORMATS['sbi_yono'], today=py_date(2026, 11, 28))
        assert parsed.detected_encoding == 'utf-8-sig'
        assert parsed.preamble_rows_skipped == 9

        # Repeated header skipped; footer lines terminated cleanly; genuine bad row before footer preserved
        debits = [r for r in parsed.rows if r.row_status == 'ready']
        credits = [r for r in parsed.rows if r.row_status == 'skipped_credit']
        invalids = [r for r in parsed.rows if r.row_status == 'invalid']
        assert len(debits) == 2
        assert len(credits) == 2
        assert len(invalids) == 1
        assert '31-02-2026' in invalids[0].error_reason

        assert debits[0].txn_date == py_date(2026, 5, 3)
        assert debits[0].amount_paise == 18_680
        assert debits[0].payment_method == 'UPI'
        assert debits[1].amount_paise == 125_050

        # Upload through Flask route, verify UI review & rule suggestion omits TFR prefix
        assert suggest_rule_pattern(debits[0].raw_narration) == 'FAKE'
        assert suggest_rule_pattern(debits[1].raw_narration) == 'SAMPLE'

        resp = _upload_csv(auth_client, sbi_csv, filename='synthetic_sbi.csv', bank_profile='sbi_yono')
        assert resp.status_code == 302
        batch_id = _extract_batch_id(resp)

        review_html = auth_client.get(f'/imports/{batch_id}').data.decode('utf-8')
        assert 'toggleAllImportRows' in review_html
        assert 'Invalid Rows (1)' in review_html
        assert 'Statement Summary' not in review_html
        assert 'Brought Forward' not in review_html
        assert 'Please do not share' not in review_html

        confirm_resp = auth_client.post(f'/imports/{batch_id}/confirm', follow_redirects=True)
        assert confirm_resp.status_code == 200
        assert Expense.query.filter_by(user_id=user.id).count() == 2

    def test_sbi_stopwords_exclude_tfr_and_bank_codes_from_rule_suggestions(self):
        assert suggest_rule_pattern(
            'WDL TFR UPI/DR/900000000009/ACME GROCERY/YESB/acme@ybl 0000123 AT 00000 BRANCH'
        ) == 'ACME'
        assert suggest_rule_pattern(
            'WDL TFR UPI/DR/900000000010/METRO TRANSIT/UBIN/metro@ubin 0000124 AT 00000 BRANCH'
        ) == 'METRO'

    def test_dr_cr_suffix_and_pos_colon_inference_and_union_bank_stopwords(self):
        assert parse_statement_amount_to_paise('55.00(Dr)', allow_signed=False, column_role='debit') == (5_500, None)
        assert parse_statement_amount_to_paise('30.00(Cr)', allow_signed=False, column_role='credit') == (3_000, None)
        assert parse_statement_amount_to_paise('1,250.50 (Dr)', allow_signed=True, column_role='signed') == (-125_050, None)

        bad_val, bad_err = parse_statement_amount_to_paise('30.00(Cr)', allow_signed=False, column_role='debit')
        assert bad_val is None and 'Credit indicator' in bad_err

        assert suggest_rule_pattern('UPIAR/900000000001/DR/FAKE MART/YESB/fake@ybl') == 'FAKE'
        assert suggest_rule_pattern('UPIAB/900000000002/CR/SAMPLE REFUND/UTIB/ref@axis') == 'SAMPLE'

        from statement_import import infer_payment_method
        assert infer_payment_method('POS:SYNTHETIC CLOUD SERVICE/0000123') == 'Debit Card'

    def test_union_bank_synthetic_encrypted_pdf_autodetects_and_verifies_balance(
        self, auth_client, user, caplog
    ):
        import logging
        from reportlab.lib import colors
        from reportlab.lib.pagesizes import letter
        from reportlab.lib.pdfencrypt import StandardEncryption
        from reportlab.platypus import PageBreak, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle
        from reportlab.lib.styles import getSampleStyleSheet

        styles = getSampleStyleSheet()
        pdf_buf = io.BytesIO()
        pw = 'UnionSecret!99'
        doc = SimpleDocTemplate(
            pdf_buf,
            pagesize=letter,
            encrypt=StandardEncryption(pw, 'OwnerSecret!99'),
        )

        # Page 1: Metadata table + Page 1 bordered statement table
        meta_tbl = Table([['Your Details', 'Account Details'], ['Synthetic User', 'INR']])
        meta_tbl.setStyle(TableStyle([('GRID', (0, 0), (-1, -1), 0.25, colors.grey)]))

        p1_rows = [
            ['Date', 'Transaction\nId', 'Remarks', 'Debit\nAmount(INR)', 'Credit\nAmount(INR)', 'Balance(INR)'],
            ['01-11-2026', 'T1001', 'UPIAR/900000000001/DR/FAKE MART\n/YESB/fake@ybl', '55.00(Dr)', '', '495.04(Cr)'],
            ['01-11-2026', 'T1002', 'UPIAB/900000000002/CR/SYNTHETIC DEP\n/CBIN/demo@okaxis', '', '30.00(Cr)', '525.04(Cr)'],
            ['02-11-2026', 'T1003', 'POS:SYNTHETIC CLOUD/0000123456', '19.82(Dr)', '', '505.22(Cr)'],
        ]
        t1 = Table(p1_rows)
        t1.setStyle(TableStyle([('GRID', (0, 0), (-1, -1), 0.5, colors.black)]))

        # Page 2: Repeated header + continuing rows maintaining exact running balance
        p2_rows = [
            ['Date', 'Transaction\nId', 'Remarks', 'Debit\nAmount(INR)', 'Credit\nAmount(INR)', 'Balance(INR)'],
            ['04-11-2026', 'T1004', 'UPIAR/900000000003/DR/DEMO CAFE\n/YESB/cafe@ybl', '230.00(Dr)', '', '275.22(Cr)'],
        ]
        t2 = Table(p2_rows)
        t2.setStyle(TableStyle([('GRID', (0, 0), (-1, -1), 0.5, colors.black)]))

        # Page 3: Promo banner page with zero transaction tables
        promo = Paragraph('Thank you for banking with us.', styles['Normal'])

        doc.build([meta_tbl, Spacer(1, 12), t1, PageBreak(), t2, PageBreak(), promo])
        pdf_bytes = pdf_buf.getvalue()

        # 1. Missing password is rejected cleanly
        resp_missing_pw = _upload_csv(
            auth_client,
            pdf_bytes,
            filename='synthetic_union.pdf',
            bank_profile='auto_detect',
        )
        assert resp_missing_pw.status_code == 302
        assert b'password-protected' in auth_client.get('/imports').data
        assert ImportBatch.query.count() == 0

        # 2. Wrong password is rejected cleanly
        resp_bad_pw = _upload_csv(
            auth_client,
            pdf_bytes,
            filename='synthetic_union.pdf',
            bank_profile='auto_detect',
            extra={'statement_password': 'wrong-password'},
        )
        assert resp_bad_pw.status_code == 302
        assert b'Incorrect password' in auth_client.get('/imports').data
        assert ImportBatch.query.count() == 0

        # 3. Valid password unlocks strictly in memory, verifies running balances, and never logs password
        with caplog.at_level(logging.INFO, logger='expenseflow'):
            resp_ok = _upload_csv(
                auth_client,
                pdf_bytes,
                filename='synthetic_union.pdf',
                bank_profile='auto_detect',
                extra={'statement_password': pw},
            )
        assert resp_ok.status_code == 302
        batch_id = _extract_batch_id(resp_ok)

        batch = db.session.get(ImportBatch, batch_id)
        assert batch.bank_profile == 'auto_detect'
        assert batch.detected_encoding == 'pdf-encrypted'
        assert 'PDF' in (batch.detected_format_label or '')
        assert batch.balance_check_status == 'verified'
        assert batch.balance_mismatch_count == 0
        assert batch.ready_count == 3
        assert batch.skipped_credit_count == 1
        assert batch.invalid_count == 0

        review_html = auth_client.get(f'/imports/{batch_id}').data.decode('utf-8')
        assert 'balanceVerifiedBadge' in review_html
        assert pw not in review_html
        assert pw not in caplog.text

        # Payment methods accurately inferred from PDF remarks
        methods = [
            r.payment_method
            for r in ImportRow.query.filter_by(batch_id=batch_id, row_status='ready').order_by(ImportRow.line_number.asc())
        ]
        assert methods == ['UPI', 'Debit Card', 'UPI']

        # Confirm writes the 3 debits atomically in paise
        auth_client.post(f'/imports/{batch_id}/confirm', follow_redirects=True)
        expenses = Expense.query.filter_by(user_id=user.id).order_by(Expense.id.asc()).all()
        assert [e.amount_paise for e in expenses] == [5_500, 1_982, 23_000]

    def test_encrypted_xlsx_empty_sheet_skip_native_dates_and_mock_xls(self, auth_client, user, caplog, monkeypatch):
        import logging
        import msoffcrypto
        import openpyxl

        wb = openpyxl.Workbook()
        empty_ws = wb.active
        empty_ws.title = 'SummaryCover'
        empty_ws.append(['Empty Cover Sheet'])

        stmt_ws = wb.create_sheet('Statement')
        stmt_ws.append(['Account Metadata', 'INR'])
        stmt_ws.append(['Date', 'Details', 'Ref No/Cheque No', 'Debit', 'Credit', 'Balance'])
        stmt_ws.append([py_date(2026, 5, 1), 'DEP TFR SALARY SYNTHETIC', 'R1', None, 5000.0, 5000.0])
        stmt_ws.append([py_date(2026, 5, 3), 'WDL TFR UPI/DR/900001/FAKE MART/YESB/fake@ybl', 'R2', 186.8, None, 4813.2])
        stmt_ws.append([py_date(2026, 5, 5), 'WDL TFR UPI/DR/900002/SAMPLE CAFE/UTIB/demo@okaxis', 'R3', 1250.5, None, 3562.7])

        plain_buf = io.BytesIO()
        wb.save(plain_buf)
        plain_buf.seek(0)

        enc_buf = io.BytesIO()
        xlsx_pw = 'ExcelLock#42'
        msoffcrypto.OfficeFile(plain_buf).encrypt(xlsx_pw, enc_buf)
        enc_bytes = enc_buf.getvalue()

        with caplog.at_level(logging.INFO, logger='expenseflow'):
            resp = _upload_csv(
                auth_client,
                enc_bytes,
                filename='yono_synthetic.xlsx',
                bank_profile='auto_detect',
                extra={'statement_password': xlsx_pw},
            )
        assert resp.status_code == 302
        batch_id = _extract_batch_id(resp)
        batch = db.session.get(ImportBatch, batch_id)
        assert batch.detected_encoding == 'xlsx-encrypted'
        assert batch.balance_check_status == 'verified'
        assert batch.ready_count == 2
        assert batch.skipped_credit_count == 1
        assert xlsx_pw not in caplog.text

        # Verify legacy .xls extractor handling via synthetic in-memory xlrd workbook
        import xlrd

        class _FakeCell:
            def __init__(self, ctype, value):
                self.ctype = ctype
                self.value = value

        class _FakeSheet:
            def __init__(self, grid):
                self._grid = grid
                self.nrows = len(grid)
                self.ncols = max(len(r) for r in grid)

            def cell(self, r, c):
                return self._grid[r][c]

        class _FakeBook:
            datemode = 0

            def sheets(self):
                return [_FakeSheet([
                    [_FakeCell(xlrd.XL_CELL_TEXT, h) for h in ('Txn Date', 'Narration', 'Amount', 'Dr/Cr', 'Balance')],
                    [_FakeCell(xlrd.XL_CELL_TEXT, '01-10-2026'), _FakeCell(xlrd.XL_CELL_TEXT, 'UPI/FAKE SHOP'),
                     _FakeCell(xlrd.XL_CELL_NUMBER, 250.5), _FakeCell(xlrd.XL_CELL_TEXT, 'DR'), _FakeCell(xlrd.XL_CELL_NUMBER, 749.5)],
                ])]

        monkeypatch.setattr(xlrd, 'open_workbook', lambda file_contents: _FakeBook())
        from statement_import import parse_statement_file
        parsed_xls = parse_statement_file(b'FAKE_XLS_STREAM', filename='legacy.xls', config=None, today=py_date(2026, 11, 28))
        assert parsed_xls.detected_encoding == 'xls'
        assert parsed_xls.rows[0].amount_paise == 25_050
        assert parsed_xls.rows[0].row_status == 'ready'

    def test_borderless_pdf_continuation_scanned_pdf_rejection_and_balance_mismatch(
        self, auth_client
    ):
        from reportlab.pdfgen import canvas as rl_canvas
        from reportlab.platypus import SimpleDocTemplate, Table

        # 1. Borderless digital PDF with Amount + Dr/Cr column and continuation narration line
        buf_borderless = io.BytesIO()
        SimpleDocTemplate(buf_borderless).build([Table([
            ['Date', 'Narration', 'Amount', 'Dr/Cr', 'Balance'],
            ['01-11-2026', 'WDL TFR UPI/DR/101/FAKE MART', '120.00', 'DR', '880.00'],
            ['', '/YESB/fakemart@ybl', '', '', ''],
            ['02-11-2026', 'DEP TFR SALARY WIRE', '500.00', 'CR', '1380.00'],
        ], colWidths=[75, 190, 65, 50, 75])])

        from statement_import import parse_statement_file
        parsed_bl = parse_statement_file(
            buf_borderless.getvalue(),
            filename='borderless.pdf',
            config=None,
            today=py_date(2026, 11, 28),
        )
        assert parsed_bl.balance_check_status == 'verified'
        assert len(parsed_bl.rows) == 2
        assert parsed_bl.rows[0].amount_paise == 12_000
        assert 'fakemart@ybl' in parsed_bl.rows[0].raw_narration
        assert parsed_bl.rows[1].row_status == 'skipped_credit'

        # 2. Scanned/image-only PDF (no selectable text) rejected cleanly
        buf_scan = io.BytesIO()
        c = rl_canvas.Canvas(buf_scan)
        c.rect(50, 50, 200, 100)
        c.showPage()
        c.save()
        with pytest.raises(StatementParseError, match='scanned/image-only'):
            parse_statement_file(buf_scan.getvalue(), filename='scan.pdf')

        # 3. Descending statement verifies without false mismatch; broken ascending flags exact row
        desc_csv = (
            'Date,Description,Amount,Balance\n'
            '2026-10-03,Later Spend,-100.00,900.00\n'
            '2026-10-01,Earlier Spend,-50.00,1000.00\n'
        ).encode('utf-8')
        parsed_desc = parse_statement_csv(desc_csv, BANK_FORMATS['signed_iso'], today=py_date(2026, 11, 28))
        assert parsed_desc.balance_check_status == 'verified'
        assert parsed_desc.balance_mismatch_count == 0

        broken_csv = (
            'Date,Description,Amount,Balance\n'
            '2026-10-01,Txn 1,-100.00,900.00\n'
            '2026-10-02,Txn 2 Corrupted Balance,-50.00,800.00\n'
        ).encode('utf-8')
        resp_brk = _upload_csv(auth_client, broken_csv, filename='broken_bal.csv', bank_profile='auto_detect')
        brk_batch_id = _extract_batch_id(resp_brk)
        brk_batch = db.session.get(ImportBatch, brk_batch_id)
        assert brk_batch.balance_check_status == 'mismatch'
        assert brk_batch.balance_mismatch_count == 1
        review_brk = auth_client.get(f'/imports/{brk_batch_id}').data.decode('utf-8')
        assert 'balanceMismatchAlert' in review_brk
        assert '₹850.00' in review_brk
        assert '₹800.00' in review_brk


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


class TestHardeningDosRetentionAndPrivacy:
    """Tests for zip-bomb/XML/PDF/timeout DoS guards, 24h TTL cleanup, per-user limits, privacy UI, and purge-all."""

    @staticmethod
    def _make_encrypted_sample_pdf(pw: str = 'UnionSecret!99') -> bytes:
        from reportlab.lib import colors
        from reportlab.lib.pagesizes import letter
        from reportlab.lib.pdfencrypt import StandardEncryption
        from reportlab.platypus import SimpleDocTemplate, Table, TableStyle

        buf = io.BytesIO()
        doc = SimpleDocTemplate(buf, pagesize=letter, encrypt=StandardEncryption(pw, 'OwnerSecret!99'))
        rows = [
            ['Date', 'Transaction Id', 'Remarks', 'Debit Amount(INR)', 'Credit Amount(INR)', 'Balance(INR)'],
            ['01-11-2026', 'T1001', 'UPIAR/900001/DR/CAFE/YESB/cafe@ybl', '50.00(Dr)', '', '950.00(Cr)'],
        ]
        tbl = Table(rows)
        tbl.setStyle(TableStyle([('GRID', (0, 0), (-1, -1), 0.5, colors.black)]))
        doc.build([tbl])
        return buf.getvalue()

    def test_defusedxml_enabled_and_xml_bomb_rejected(self, auth_client, user):
        import zipfile
        import openpyxl
        from statement_import import parse_statement_file

        assert openpyxl.DEFUSEDXML is True

        buf = io.BytesIO()
        with zipfile.ZipFile(buf, 'w', compression=zipfile.ZIP_DEFLATED) as zf:
            zf.writestr('[Content_Types].xml', '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"></Types>')
            zf.writestr(
                'xl/workbook.xml',
                '<?xml version="1.0" encoding="UTF-8"?>'
                '<!DOCTYPE lolz [<!ENTITY lol "lol"><!ENTITY lol2 "&lol;&lol;&lol;">]>'
                '<workbook>&lol2;</workbook>',
            )
        xml_bomb = buf.getvalue()

        with pytest.raises(StatementParseError, match='unsafe XML'):
            parse_statement_file(xml_bomb, filename='xml_bomb.xlsx')

        resp = _upload_csv(auth_client, xml_bomb, filename='xml_bomb.xlsx', bank_profile='auto_detect')
        assert resp.status_code == 302
        assert b'unsafe XML' in auth_client.get('/imports').data
        assert ImportBatch.query.count() == 0

    def test_xlsx_zip_bomb_entry_cap_and_bounded_ole_stream(self, auth_client, user):
        import zipfile
        from statement_import import MAX_XLSX_ZIP_ENTRIES, _BoundedBytesIO, parse_statement_file

        # 1. Tiny compressed ZIP (<50 KB) expanding beyond MAX_XLSX_DECOMPRESSED_BYTES (10 MB)
        bomb_buf = io.BytesIO()
        with zipfile.ZipFile(bomb_buf, 'w', compression=zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
            zf.writestr('[Content_Types].xml', '<Types/>')
            zf.writestr('xl/sharedStrings.xml', '<sst>' + ('A' * (9 * 1024 * 1024)) + '</sst>')
        bomb_bytes = bomb_buf.getvalue()
        assert len(bomb_bytes) < 50_000

        with pytest.raises(StatementParseError, match='safe decompression limit'):
            parse_statement_file(bomb_bytes, filename='zip_bomb.xlsx')

        resp = _upload_csv(auth_client, bomb_bytes, filename='zip_bomb.xlsx', bank_profile='auto_detect')
        assert resp.status_code == 302
        assert b'safe decompression limit' in auth_client.get('/imports').data
        assert ImportBatch.query.count() == 0

        # 2. Entry-count bomb (> MAX_XLSX_ZIP_ENTRIES)
        many_buf = io.BytesIO()
        with zipfile.ZipFile(many_buf, 'w', compression=zipfile.ZIP_DEFLATED) as zf:
            for i in range(MAX_XLSX_ZIP_ENTRIES + 1):
                zf.writestr(f'xl/entry_{i}.txt', 'x')
        with pytest.raises(StatementParseError, match='archive limit'):
            parse_statement_file(many_buf.getvalue(), filename='many_entries.xlsx')

        # 3. Bounded OLE decrypted stream aborts before buffering unbounded bytes
        bounded = _BoundedBytesIO(1024, 'Decrypted payload too large')
        assert bounded.write(b'x' * 512) == 512
        with pytest.raises(StatementParseError, match='Decrypted payload too large'):
            bounded.write(b'y' * 600)

    def test_pdf_page_cap_25_and_pathological_table_rejected_and_timeout_kills_worker(self, auth_client, user):
        from reportlab.lib import colors
        from reportlab.lib.pagesizes import letter
        from reportlab.pdfgen import canvas
        from reportlab.platypus import PageBreak, SimpleDocTemplate, Table, TableStyle
        from statement_import import MAX_PDF_PAGES, parse_statement_file, parse_statement_file_isolated

        assert MAX_PDF_PAGES == 25

        # 1. 26-page PDF exceeds MAX_PDF_PAGES (25)
        buf_26 = io.BytesIO()
        c = canvas.Canvas(buf_26, pagesize=letter)
        for p in range(26):
            c.drawString(50, 750, f'Synthetic Page {p + 1}')
            c.showPage()
        c.save()
        pdf_26 = buf_26.getvalue()

        with pytest.raises(StatementParseError, match='maximum of 25 pages'):
            parse_statement_file(pdf_26, filename='too_many_pages.pdf')

        resp = _upload_csv(auth_client, pdf_26, filename='too_many_pages.pdf', bank_profile='auto_detect')
        assert resp.status_code == 302
        assert b'maximum of 25 pages' in auth_client.get('/imports').data
        assert ImportBatch.query.count() == 0

        # 2. Pathological PDF vector-grid bomb rejected quickly without hanging
        buf_vec = io.BytesIO()
        cv = canvas.Canvas(buf_vec, pagesize=letter)
        cv.drawString(50, 760, 'Date   Remarks   Debit   Balance')
        for i in range(2_700):
            y = 100 + (i % 500)
            cv.line(40, y, 540, y + 1)
        cv.save()
        with pytest.raises(StatementParseError, match='safe complexity limits'):
            parse_statement_file_isolated(buf_vec.getvalue(), filename='pathological.pdf')

        # 3. Real subprocess timeout kills child cleanly and keeps parent healthy
        buf_dense = io.BytesIO()
        doc = SimpleDocTemplate(buf_dense, pagesize=letter)
        story = []
        sample_rows = [
            ['Date', 'Transaction Id', 'Remarks', 'Debit Amount(INR)', 'Credit Amount(INR)', 'Balance(INR)'],
            *[
                [f'0{(i % 9) + 1}-11-2026', f'T{i}', f'UPI/DR/MERCHANT{i}', '10.00(Dr)', '', '900.00(Cr)']
                for i in range(15)
            ],
        ]
        for p in range(15):
            t = Table(sample_rows)
            t.setStyle(TableStyle([('GRID', (0, 0), (-1, -1), 0.5, colors.black)]))
            story.append(t)
            if p < 14:
                story.append(PageBreak())
        doc.build(story)

        with pytest.raises(StatementParseError, match='processing time limit'):
            parse_statement_file_isolated(
                buf_dense.getvalue(),
                filename='dense_slow.pdf',
                timeout_seconds=0.05,
            )

        # Parent process remains completely responsive for subsequent parses
        ok_parsed = parse_statement_file_isolated(
            b'Date,Description,Amount\n2026-10-01,Tea,-15.00\n',
            filename='after_timeout.csv',
            config=BANK_FORMATS['signed_iso'],
        )
        assert len(ok_parsed.rows) == 1

    def test_per_user_import_rate_limiting_isolates_users_and_ignores_ip_rotation(self, tmp_path):
        db_file = tmp_path / 'per_user_rl.db'
        rl_app = create_app('development', database_uri=f"sqlite:///{db_file.as_posix()}")
        rl_app.config['WTF_CSRF_ENABLED'] = False
        try:
            with rl_app.app_context():
                db.create_all()
                u1 = User(username='rl_alice', email='rl_alice@example.com')
                u1.set_password('password123')
                u2 = User(username='rl_bob', email='rl_bob@example.com')
                u2.set_password('password123')
                db.session.add_all([u1, u2])
                db.session.commit()

            limiter.reset()
            csv_payload = b'Date,Description,Amount\n2026-10-01,Coffee,-25.00\n'

            alice_client = rl_app.test_client()
            _login(alice_client, 'rl_alice', 'password123')
            for _ in range(10):
                r = _upload_csv(alice_client, csv_payload, bank_profile='signed_iso')
                assert r.status_code == 302

            # 11th request from same IP hits per-user 429
            r_blocked = _upload_csv(alice_client, csv_payload, bank_profile='signed_iso')
            assert r_blocked.status_code == 429

            # Rotating client IP does NOT bypass per-user limit
            r_rotated_ip = alice_client.post(
                '/imports',
                data={
                    'bank_profile': 'signed_iso',
                    'statement_file': (io.BytesIO(csv_payload), 'statement.csv'),
                },
                content_type='multipart/form-data',
                environ_base={'REMOTE_ADDR': '203.0.113.77'},
            )
            assert r_rotated_ip.status_code == 429

            # Another user sharing the exact same IP is NOT throttled by Alice
            bob_client = rl_app.test_client()
            _login(bob_client, 'rl_bob', 'password123')
            r_bob = _upload_csv(bob_client, csv_payload, bank_profile='signed_iso')
            assert r_bob.status_code == 302
        finally:
            limiter.reset()
            limiter.enabled = False
            with rl_app.app_context():
                db.engine.dispose()

    def test_staged_batch_24h_ttl_lazy_purge_and_cli_script(self, auth_client, user, tmp_path):
        from datetime import datetime, timedelta, timezone
        import os
        import sys

        now_naive = datetime.now(timezone.utc).replace(tzinfo=None)
        old_ts = now_naive - timedelta(hours=25)
        fresh_ts = now_naive - timedelta(hours=5)

        expired_staged = ImportBatch(
            user_id=user.id,
            bank_profile='signed_iso',
            detected_format_label='Signed Amount',
            original_filename='expired_staged.csv',
            file_sha256='e' * 64,
            detected_encoding='utf-8',
            status='staged',
            total_rows=1,
            created_at=old_ts,
        )
        fresh_staged = ImportBatch(
            user_id=user.id,
            bank_profile='signed_iso',
            detected_format_label='Signed Amount',
            original_filename='fresh_staged.csv',
            file_sha256='f' * 64,
            detected_encoding='utf-8',
            status='staged',
            total_rows=1,
            created_at=fresh_ts,
        )
        old_confirmed = ImportBatch(
            user_id=user.id,
            bank_profile='signed_iso',
            detected_format_label='Signed Amount',
            original_filename='old_confirmed.csv',
            file_sha256='c' * 64,
            detected_encoding='utf-8',
            status='confirmed',
            total_rows=1,
            imported_count=1,
            created_at=old_ts,
            confirmed_at=old_ts,
        )
        db.session.add_all([expired_staged, fresh_staged, old_confirmed])
        db.session.flush()
        db.session.add_all([
            ImportRow(
                batch_id=expired_staged.id, user_id=user.id, line_number=2,
                row_status='ready', txn_date=py_date(2026, 10, 1), amount_paise=1000,
                raw_narration='OLD STAGED', description='OLD STAGED', payment_method='UPI',
            ),
            ImportRow(
                batch_id=fresh_staged.id, user_id=user.id, line_number=2,
                row_status='ready', txn_date=py_date(2026, 10, 2), amount_paise=2000,
                raw_narration='FRESH STAGED', description='FRESH STAGED', payment_method='UPI',
            ),
            ImportRow(
                batch_id=old_confirmed.id, user_id=user.id, line_number=2,
                row_status='imported', txn_date=py_date(2026, 10, 3), amount_paise=3000,
                raw_narration='OLD CONFIRMED', description='OLD CONFIRMED', payment_method='UPI',
            ),
            Expense(
                user_id=user.id, date=py_date(2026, 10, 3), category='Food',
                amount_paise=3000, description='Confirmed Keeper', payment_method='UPI',
                import_batch_id=old_confirmed.id, import_fingerprint='fp-keep-1',
            ),
        ])
        db.session.commit()
        expired_id = expired_staged.id
        fresh_id = fresh_staged.id
        confirmed_id = old_confirmed.id

        # Upload triggers lazy purge of expired staged batch only
        resp = _upload_csv(
            auth_client,
            b'Date,Description,Amount\n2026-10-05,New Coffee,-50.00\n',
            bank_profile='signed_iso',
        )
        assert resp.status_code == 302
        new_id = _extract_batch_id(resp)

        assert db.session.get(ImportBatch, expired_id) is None
        assert ImportRow.query.filter_by(batch_id=expired_id).count() == 0
        assert db.session.get(ImportBatch, fresh_id) is not None
        assert db.session.get(ImportBatch, confirmed_id) is not None
        assert db.session.get(ImportBatch, new_id) is not None
        assert Expense.query.filter_by(import_batch_id=confirmed_id).count() == 1

        # Scheduled CLI script works cleanly on file SQLite without EXPENSEFLOW_SECRET_KEY
        cli_db = tmp_path / 'cli_cleanup.db'
        cli_app = create_app('development', database_uri=f"sqlite:///{cli_db.as_posix()}")
        try:
            with cli_app.app_context():
                db.create_all()
                cu = User(username='cliuser', email='cli@example.com')
                cu.set_password('password123')
                db.session.add(cu)
                db.session.flush()
                b_exp = ImportBatch(
                    user_id=cu.id, bank_profile='signed_iso', detected_format_label='Signed Amount',
                    original_filename='SECRET_CLI_EXPIRED.csv', file_sha256='1' * 64,
                    detected_encoding='utf-8', status='staged', total_rows=1, created_at=old_ts,
                )
                b_keep = ImportBatch(
                    user_id=cu.id, bank_profile='signed_iso', detected_format_label='Signed Amount',
                    original_filename='recent.csv', file_sha256='2' * 64,
                    detected_encoding='utf-8', status='staged', total_rows=1, created_at=fresh_ts,
                )
                b_conf = ImportBatch(
                    user_id=cu.id, bank_profile='signed_iso', detected_format_label='Signed Amount',
                    original_filename='confirmed.csv', file_sha256='3' * 64,
                    detected_encoding='utf-8', status='confirmed', total_rows=1, created_at=old_ts, confirmed_at=old_ts,
                )
                db.session.add_all([b_exp, b_keep, b_conf])
                db.session.flush()
                db.session.add(ImportRow(
                    batch_id=b_exp.id, user_id=cu.id, line_number=2,
                    row_status='ready', txn_date=py_date(2026, 10, 1), amount_paise=100,
                    raw_narration='SECRET_CLI_NARRATION', description='SECRET_CLI_NARRATION',
                    payment_method='UPI',
                ))
                db.session.commit()
                cli_exp_id = b_exp.id
        finally:
            with cli_app.app_context():
                db.engine.dispose()

        clean_env = {k: v for k, v in os.environ.items() if k != 'EXPENSEFLOW_SECRET_KEY'}
        script = str(REPO_ROOT / 'scripts' / 'cleanup_staged_imports.py')
        db_uri = f"sqlite:///{cli_db.as_posix()}"

        dry = subprocess.run(
            [sys.executable, script, '--database-uri', db_uri, '--ttl-hours', '24', '--dry-run'],
            capture_output=True, text=True, env=clean_env, timeout=15,
        )
        assert dry.returncode == 0, dry.stderr
        assert json.loads(dry.stdout) == {'dry_run': True, 'purged_batches': 1, 'purged_rows': 1, 'ttl_hours': 24}
        assert 'SECRET_CLI' not in dry.stdout and 'SECRET_CLI' not in dry.stderr

        live = subprocess.run(
            [sys.executable, script, '--database-uri', db_uri, '--ttl-hours', '24'],
            capture_output=True, text=True, env=clean_env, timeout=15,
        )
        assert live.returncode == 0, live.stderr
        assert json.loads(live.stdout) == {'dry_run': False, 'purged_batches': 1, 'purged_rows': 1, 'ttl_hours': 24}
        assert 'SECRET_CLI' not in live.stdout and 'SECRET_CLI' not in live.stderr

        verify_app = create_app('development', database_uri=db_uri)
        try:
            with verify_app.app_context():
                assert db.session.get(ImportBatch, cli_exp_id) is None
                assert ImportRow.query.filter_by(batch_id=cli_exp_id).count() == 0
                assert ImportBatch.query.count() == 2
        finally:
            with verify_app.app_context():
                db.engine.dispose()

    def test_password_never_leaks_in_logs_session_flash_or_error_pages(
        self, auth_client, user, caplog, monkeypatch
    ):
        import logging
        import blueprints.imports as imports_mod

        pdf_pw = 'RightSecretPw!77'
        wrong_pw = 'WrongSecretPw!99'
        crash_pw = 'CrashSecretPw!55'
        enc_pdf = self._make_encrypted_sample_pdf(pw=pdf_pw)

        with caplog.at_level(logging.DEBUG):
            # 1. Wrong password on encrypted PDF
            r_wrong = _upload_csv(
                auth_client, enc_pdf, filename='enc.pdf', bank_profile='auto_detect',
                extra={'statement_password': wrong_pw},
            )
            assert r_wrong.status_code == 302
            wrong_html = auth_client.get('/imports').data.decode('utf-8')
            assert 'Incorrect password' in wrong_html
            assert wrong_pw not in wrong_html

            # 2. Valid password on encrypted PDF
            r_ok = _upload_csv(
                auth_client, enc_pdf, filename='enc.pdf', bank_profile='auto_detect',
                extra={'statement_password': pdf_pw},
            )
            assert r_ok.status_code == 302
            batch_id = _extract_batch_id(r_ok)
            ok_html = auth_client.get(f'/imports/{batch_id}').data.decode('utf-8')
            assert pdf_pw not in ok_html

            # 3. Unexpected exception / traceback never echoes supplied password
            def _boom(*_args, **_kwargs):
                raise RuntimeError(f'simulated internal failure with {crash_pw}')

            monkeypatch.setattr(imports_mod, 'parse_statement_file_isolated', _boom)
            r_crash = _upload_csv(
                auth_client, b'Date,Description,Amount\n2026-10-01,Test,-10.00\n',
                filename='crash.csv', bank_profile='signed_iso',
                extra={'statement_password': crash_pw},
            )
            assert r_crash.status_code == 302
            crash_html = auth_client.get('/imports').data.decode('utf-8')
            assert 'Unable to parse the uploaded statement file safely.' in crash_html
            assert crash_pw not in crash_html

        with auth_client.session_transaction() as sess:
            sess_dump = repr(dict(sess))
            for secret in (pdf_pw, wrong_pw, crash_pw):
                assert secret not in sess_dump
                assert secret not in caplog.text

    def test_privacy_notice_tested_format_wording_and_purge_all_imported_data(
        self, app, client, auth_client, user
    ):
        # 1. Privacy notice and non-"universal" tested-format wording on /imports
        page_html = auth_client.get('/imports').data.decode('utf-8')
        assert 'Privacy &amp; Data Retention' in page_html
        assert '24 hours' in page_html
        assert 'Delete All My Imported Data' in page_html
        assert 'YONO SBI' in page_html
        assert 'Union Ease' in page_html
        assert 'Custom Column Mapping' in page_html
        assert 'universal' not in page_html.lower()
        assert 'any indian bank' not in page_html.lower()

        # 2. Seed manual expense + confirmed batch + staged batch + rule for Alice, plus data for Bob
        manual_exp = Expense(
            user_id=user.id, date=py_date(2026, 10, 1), category='Food',
            amount_paise=12500, description='Manual Lunch', payment_method='Cash',
        )
        bob = User(username='bob_purge', email='bob_purge@example.com')
        bob.set_password('password123')
        db.session.add_all([manual_exp, bob])
        db.session.commit()

        # Upload & confirm batch 1 for Alice with a rule
        resp_confirm = _upload_csv(
            auth_client,
            b'Date,Description,Amount\n2026-10-02,Imported Taxi,-350.00\n',
            bank_profile='signed_iso',
        )
        confirm_id = _extract_batch_id(resp_confirm)
        auth_client.post(f'/imports/{confirm_id}/confirm', data={'include_row_1': '1'})
        auth_client.post('/imports/rules', data={'pattern': 'UBER', 'category': 'Transport'})

        # Upload staged batch 2 for Alice
        _upload_csv(
            auth_client,
            b'Date,Description,Amount\n2026-10-03,Staged Snack,-45.00\n',
            bank_profile='signed_iso',
        )

        # Seed Bob's imported data
        bob_batch = ImportBatch(
            user_id=bob.id, bank_profile='signed_iso', detected_format_label='Signed Amount',
            original_filename='bob.csv', file_sha256='b' * 64, detected_encoding='utf-8',
            status='confirmed', total_rows=1, imported_count=1,
        )
        db.session.add(bob_batch)
        db.session.flush()
        db.session.add_all([
            ImportRow(
                batch_id=bob_batch.id, user_id=bob.id, line_number=2, row_status='imported',
                txn_date=py_date(2026, 10, 4), amount_paise=9900, raw_narration='BOB',
                description='BOB', payment_method='UPI',
            ),
            Expense(
                user_id=bob.id, date=py_date(2026, 10, 4), category='Food',
                amount_paise=9900, description='Bob Imported', payment_method='UPI',
                import_batch_id=bob_batch.id, import_fingerprint='bob-fp-1',
            ),
            CategoryRule(user_id=bob.id, pattern='SWIGGY', pattern_normalized='SWIGGY', category='Food'),
        ])
        db.session.commit()

        # CSRF enforced on /imports/purge-all when enabled
        app.config['WTF_CSRF_ENABLED'] = True
        app.config['WTF_CSRF_CHECK_DEFAULT'] = True
        try:
            assert auth_client.post('/imports/purge-all').status_code == 400
        finally:
            app.config['WTF_CSRF_ENABLED'] = False
            app.config['WTF_CSRF_CHECK_DEFAULT'] = False

        # Alice purges all her imported data cleanly
        purge_resp = auth_client.post('/imports/purge-all', follow_redirects=True)
        assert purge_resp.status_code == 200
        assert b'Deleted all imported data' in purge_resp.data

        assert Expense.query.filter_by(user_id=user.id).count() == 1
        assert Expense.query.filter_by(user_id=user.id).one().description == 'Manual Lunch'
        assert ImportBatch.query.filter_by(user_id=user.id).count() == 0
        assert ImportRow.query.filter_by(user_id=user.id).count() == 0
        assert CategoryRule.query.filter_by(user_id=user.id).count() == 0

        # Bob remains 100% isolated and untouched
        assert Expense.query.filter_by(user_id=bob.id).count() == 1
        assert ImportBatch.query.filter_by(user_id=bob.id).count() == 1
        assert ImportRow.query.filter_by(batch_id=bob_batch.id).count() == 1
        assert CategoryRule.query.filter_by(user_id=bob.id).count() == 1

