"""
statement_import.py — Bank CSV parsing, mapping configs, deterministic categorisation,
idempotent deduplication, and staging helpers for ExpenseFlow.

All parsing happens strictly in memory without writing uploaded files to disk.
Monetary amounts are parsed with Decimal (ROUND_HALF_UP) into exact integer paise.
"""
from __future__ import annotations

from collections import Counter
import csv
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import hashlib
import io
import re
from typing import Iterable

from utils import MAX_EXPENSE_RUPEES, VALID_CATEGORIES, VALID_METHODS

MAX_IMPORT_FILE_BYTES = 2 * 1024 * 1024  # 2 MB
MAX_IMPORT_ROWS = 2_000
MAX_PREAMBLE_SCAN = 50
MANUAL_DUP_DATE_TOLERANCE_DAYS = 1

_PAISE_PER_RUPEE = Decimal('100')
_CURRENCY_PREFIX_RE = re.compile(r'^(?:₹|inr|rs\.?)\s*', re.IGNORECASE)
_ASCII_GROUPED_DECIMAL_RE = re.compile(
    r'^[+-]?(?:(?:[0-9]+|[0-9]{1,3}(?:,[0-9]{2,3})+)(?:\.[0-9]+)?|\.[0-9]+)$'
)
_ASCII_DATE_CHARS_RE = re.compile(r'^[0-9A-Za-z/\-\s.,:]+$')
_CONTROL_CHARS_RE = re.compile(r'[\x00-\x1f\x7f-\x9f]')

_RULE_STOPWORDS = {
    'UPI', 'DR', 'CR', 'NEFT', 'RTGS', 'IMPS', 'POS', 'ATM', 'WDL', 'DEP', 'TFR', 'INB',
    'ACH', 'NACH', 'ECS', 'P2A', 'P2M', 'TRANSFER', 'PAYMENT', 'PAY', 'TXN', 'REF', 'NO',
    'INR', 'RS', 'TO', 'FROM', 'BY', 'AT', 'FOR', 'THE', 'AND', 'MR', 'MS', 'MRS',
    'BANK', 'INDIA', 'PVT', 'LTD', 'DEBIT', 'CREDIT', 'CARD', 'ONLINE', 'ECOM',
    'PURCHASE', 'WITHDRAWAL', 'DEPOSIT',
    'YESB', 'HDFC', 'SBIN', 'ICIC', 'UTIB', 'KKBK', 'BARB', 'PUNB', 'CNRB', 'IDFB',
    'UBIN', 'CBIN', 'BKID', 'AIRP', 'UCBA', 'IOBA', 'FDRL', 'PYTM', 'MAHB', 'SIBL',
    'INDB', 'CIUB', 'KVBL', 'TMBL', 'KARB', 'FINO', 'IPPB', 'JIOB',
}
_IFSC_LIKE_RE = re.compile(r'^[A-Z]{4}0[A-Z0-9]{6}$')
_MASKED_CARD_RE = re.compile(r'^[0-9]{4}[X*]{4,}[0-9]{2,4}$')

_STATEMENT_FOOTER_PREFIXES = (
    'statement summary',
    'account summary',
    'summary :',
    'summary:',
    'end of statement',
    'end of the statement',
    'brought forward',
    'opening balance',
    'closing balance',
    'total debits',
    'grand total',
    'please do not share your atm',
    'if your account is operated by a power of attorney',
    'this is a computer generated statement',
    'legends used in',
)


class StatementParseError(ValueError):
    """Raised when an uploaded CSV fails file-level validation or header mapping."""


@dataclass(frozen=True)
class BankFormatConfig:
    """Declarative mapping config describing one bank CSV statement layout."""
    key: str
    label: str
    description: str
    date_column: str
    date_formats: tuple[str, ...]
    narration_column: str
    amount_mode: str  # 'split_columns' or 'signed_amount'
    debit_column: str | None = None
    credit_column: str | None = None
    amount_column: str | None = None
    signed_debit_negative: bool = True
    ref_column: str | None = None
    preamble_rows: int = 0
    strict_preamble: bool = False
    max_preamble_scan: int = MAX_PREAMBLE_SCAN
    default_payment_method: str = 'Net Banking'


BANK_FORMATS: dict[str, BankFormatConfig] = {
    'indian_standard': BankFormatConfig(
        key='indian_standard',
        label='Indian Bank Standard (DD/MM/YYYY · Withdrawal / Deposit)',
        description=(
            'Typical Indian bank CSV with metadata preamble rows, DD/MM/YYYY dates, '
            'Particulars narration, optional Chq./Ref.No., and separate Withdrawal (INR) / Deposit (INR) columns.'
        ),
        date_column='Date',
        date_formats=('%d/%m/%Y', '%d-%m-%Y', '%d/%m/%y'),
        narration_column='Particulars',
        amount_mode='split_columns',
        debit_column='Withdrawal (INR)',
        credit_column='Deposit (INR)',
        ref_column='Chq./Ref.No.',
        preamble_rows=5,
        strict_preamble=False,
        default_payment_method='Net Banking',
    ),
    'sbi_yono': BankFormatConfig(
        key='sbi_yono',
        label='SBI / YONO SBI (DD-MM-YYYY · Details / Debit / Credit)',
        description=(
            'State Bank of India (YONO / NetBanking Excel exported via Save As → CSV UTF-8) with '
            'metadata preamble, DD-MM-YYYY dates, Details narration, Ref No/Cheque No, '
            'Debit/Credit columns, and Statement Summary footer.'
        ),
        date_column='Date',
        date_formats=('%d-%m-%Y', '%d/%m-%Y', '%d %b %Y', '%d-%b-%Y', '%d-%m-%y', '%d/%m/%y'),
        narration_column='Details',
        amount_mode='split_columns',
        debit_column='Debit',
        credit_column='Credit',
        ref_column='Ref No/Cheque No',
        preamble_rows=12,
        strict_preamble=False,
        default_payment_method='Net Banking',
    ),
    'indian_alt_dash': BankFormatConfig(
        key='indian_alt_dash',
        label='Indian Bank Alt (DD-MM-YYYY · Debit / Credit)',
        description=(
            'Alternate Indian bank export using Txn Date, Narration, Ref No, Debit, and Credit columns.'
        ),
        date_column='Txn Date',
        date_formats=('%d-%m-%Y', '%d/%m/%Y', '%d %b %Y'),
        narration_column='Narration',
        amount_mode='split_columns',
        debit_column='Debit',
        credit_column='Credit',
        ref_column='Ref No',
        preamble_rows=0,
        strict_preamble=False,
        default_payment_method='Net Banking',
    ),
    'signed_iso': BankFormatConfig(
        key='signed_iso',
        label='Signed Amount CSV (YYYY-MM-DD · Negative = Debit)',
        description=(
            'Single signed Amount column where negative numbers are debits (expenses) '
            'and positive numbers are credits (skipped in v1).'
        ),
        date_column='Date',
        date_formats=('%Y-%m-%d', '%d/%m/%Y'),
        narration_column='Description',
        amount_mode='signed_amount',
        amount_column='Amount',
        signed_debit_negative=True,
        ref_column=None,
        preamble_rows=0,
        strict_preamble=False,
        default_payment_method='Net Banking',
    ),
}


SUPPORTED_CUSTOM_DATE_FORMATS: dict[str, tuple[str, ...]] = {
    'dd/mm/yyyy': ('%d/%m/%Y', '%d/%m/%y'),
    'dd-mm-yyyy': ('%d-%m-%Y', '%d-%m-%y'),
    'yyyy-mm-dd': ('%Y-%m-%d',),
    'mm/dd/yyyy': ('%m/%d/%Y',),
}


@dataclass(frozen=True)
class ParsedStatementRow:
    """Normalized in-memory representation of a single post-header CSV row."""
    line_number: int
    row_status: str  # 'ready' | 'skipped_credit' | 'invalid'
    txn_date: date | None
    amount_paise: int | None
    raw_narration: str
    description: str
    ref_no: str
    payment_method: str
    fingerprint: str | None
    error_reason: str | None
    raw_preview: str


@dataclass(frozen=True)
class ParsedStatement:
    """Complete result of decoding and parsing a statement CSV in memory."""
    bank_profile: str
    detected_encoding: str
    file_sha256: str
    preamble_rows_skipped: int
    rows: list[ParsedStatementRow]


def clean_untrusted_text(raw: str | None, max_len: int) -> str:
    """Strip control chars, collapse whitespace, and bound length for untrusted CSV text."""
    if not raw:
        return ''
    cleaned = _CONTROL_CHARS_RE.sub(' ', str(raw))
    collapsed = ' '.join(cleaned.split())
    return collapsed[:max_len]


def sanitize_filename(filename: str | None) -> str:
    """Extract a clean basename without path separators or control characters."""
    if not filename:
        return 'statement.csv'
    base = filename.replace('\\', '/').rsplit('/', 1)[-1]
    cleaned = clean_untrusted_text(base, 255)
    return cleaned or 'statement.csv'


def build_custom_bank_config(
    *,
    date_column: str,
    date_format_key: str,
    narration_column: str,
    amount_mode: str,
    debit_column: str = '',
    credit_column: str = '',
    amount_column: str = '',
    signed_debit_negative: bool = True,
    ref_column: str = '',
    preamble_rows: int = 0,
) -> BankFormatConfig:
    """Validate and construct a custom BankFormatConfig from user-supplied column names."""
    d_col = clean_untrusted_text(date_column, 80)
    n_col = clean_untrusted_text(narration_column, 80)
    if not d_col or not n_col:
        raise StatementParseError('Custom mapping requires both Date and Narration column names.')

    date_formats = SUPPORTED_CUSTOM_DATE_FORMATS.get(date_format_key)
    if not date_formats:
        raise StatementParseError('Please select a supported custom date format.')

    if amount_mode not in {'split_columns', 'signed_amount'}:
        raise StatementParseError('Custom amount mode must be split_columns or signed_amount.')

    if amount_mode == 'split_columns':
        deb = clean_untrusted_text(debit_column, 80)
        cred = clean_untrusted_text(credit_column, 80)
        if not deb or not cred:
            raise StatementParseError('Split-column mapping requires both Debit and Credit column names.')
        amt_col = None
    else:
        amt_col = clean_untrusted_text(amount_column, 80)
        if not amt_col:
            raise StatementParseError('Signed-amount mapping requires an Amount column name.')
        deb = None
        cred = None

    if preamble_rows < 0 or preamble_rows > MAX_PREAMBLE_SCAN:
        raise StatementParseError(f'Preamble rows to skip must be between 0 and {MAX_PREAMBLE_SCAN}.')

    return BankFormatConfig(
        key='custom',
        label='Custom CSV Mapping',
        description='User-supplied column mapping configuration.',
        date_column=d_col,
        date_formats=date_formats,
        narration_column=n_col,
        amount_mode=amount_mode,
        debit_column=deb,
        credit_column=cred,
        amount_column=amt_col,
        signed_debit_negative=signed_debit_negative,
        ref_column=clean_untrusted_text(ref_column, 80) or None,
        preamble_rows=preamble_rows,
        strict_preamble=(preamble_rows > 0),
        default_payment_method='Net Banking',
    )


def decode_csv_bytes(raw_bytes: bytes) -> tuple[str, str]:
    """
    Decode raw uploaded CSV bytes strictly in memory with BOM/encoding handling.

    Rejects empty payloads, oversized files, and binary content containing NUL or
    unexpected control bytes. Returns (decoded_text, encoding_name).
    """
    if not raw_bytes:
        raise StatementParseError('Uploaded CSV file is empty.')
    if len(raw_bytes) > MAX_IMPORT_FILE_BYTES:
        raise StatementParseError('CSV file exceeds the 2 MB maximum size limit.')

    encoding_used = None
    text = None

    if raw_bytes.startswith(b'\xef\xbb\xbf'):
        try:
            text = raw_bytes.decode('utf-8-sig')
            encoding_used = 'utf-8-sig'
        except UnicodeDecodeError as exc:
            raise StatementParseError('Unable to decode UTF-8 BOM CSV file.') from exc
    elif raw_bytes.startswith((b'\xff\xfe', b'\xfe\xff')):
        try:
            text = raw_bytes.decode('utf-16')
            encoding_used = 'utf-16'
        except UnicodeDecodeError as exc:
            raise StatementParseError('Unable to decode UTF-16 CSV file.') from exc
    else:
        if b'\x00' in raw_bytes:
            raise StatementParseError('Uploaded file appears to be binary and not a valid CSV.')
        for candidate in ('utf-8', 'cp1252', 'iso-8859-1'):
            try:
                text = raw_bytes.decode(candidate)
                encoding_used = candidate
                break
            except UnicodeDecodeError:
                continue

    if text is None or encoding_used is None:
        raise StatementParseError('Could not decode CSV text using supported encodings.')

    # Reject non-whitespace binary and C1 control characters
    for ch in text:
        code = ord(ch)
        if (code < 32 and ch not in ('\t', '\n', '\r')) or code == 127 or (128 <= code <= 159):
            raise StatementParseError('Uploaded file contains invalid control bytes and is not a plain text CSV.')

    if not text.strip():
        raise StatementParseError('Uploaded CSV file is empty.')

    normalized_text = text.replace('\r\n', '\n').replace('\r', '\n')
    return normalized_text, encoding_used


def parse_statement_amount_to_paise(
    raw_value: str | None,
    *,
    allow_signed: bool = False,
) -> tuple[int | None, str | None]:
    """
    Parse a statement amount string into signed/unsigned integer paise using Decimal (ROUND_HALF_UP).

    Supports comma-grouped numbers (Indian `1,00,000.50` and Western `100,000.50`),
    optional leading currency markers (₹ / INR / Rs.), and rejects NaN, Infinity,
    scientific notation, and non-ASCII digits.
    Returns (paise_or_None, error_reason_or_None).
    Note: an empty/blank/dash string returns (None, None).
    """
    cleaned = clean_untrusted_text(raw_value, 64)
    if not cleaned or cleaned in {'-', '--', '—'}:
        return None, None

    cleaned = _CURRENCY_PREFIX_RE.sub('', cleaned).strip()
    if not cleaned:
        return None, None

    # Allow spaces only around sign if currency sat between sign and number (e.g. "- ₹50.00")
    if cleaned[0] in {'+', '-'}:
        sign_char = cleaned[0]
        rest = _CURRENCY_PREFIX_RE.sub('', cleaned[1:].strip()).strip()
        cleaned = sign_char + rest

    if not _ASCII_GROUPED_DECIMAL_RE.fullmatch(cleaned):
        return None, f"Invalid amount '{clean_untrusted_text(raw_value, 40)}'"

    if not allow_signed and cleaned[0] in {'+', '-'}:
        if cleaned[0] == '-':
            return None, f"Negative amount '{clean_untrusted_text(raw_value, 40)}' in unsigned column"

    numeric_str = cleaned.replace(',', '')
    try:
        dec = Decimal(numeric_str)
    except InvalidOperation:
        return None, f"Invalid amount '{clean_untrusted_text(raw_value, 40)}'"

    if not dec.is_finite():
        return None, f"Invalid non-finite amount '{clean_untrusted_text(raw_value, 40)}'"

    if abs(dec) > Decimal(MAX_EXPENSE_RUPEES):
        return None, f'Amount exceeds maximum limit of ₹{MAX_EXPENSE_RUPEES:,}'

    paise = int((dec * _PAISE_PER_RUPEE).quantize(Decimal('1'), rounding=ROUND_HALF_UP))
    if abs(paise) > int(MAX_EXPENSE_RUPEES) * 100:
        return None, f'Amount exceeds maximum limit of ₹{MAX_EXPENSE_RUPEES:,}'

    return paise, None


def _parse_statement_date(
    raw_value: str | None,
    date_formats: tuple[str, ...],
    today: date,
) -> tuple[date | None, str | None]:
    """Parse a date cell strictly against configured formats with ASCII digit validation."""
    cleaned = clean_untrusted_text(raw_value, 40)
    if not cleaned:
        return None, 'Missing transaction date'
    if not _ASCII_DATE_CHARS_RE.fullmatch(cleaned):
        return None, f"Invalid date '{cleaned}'"

    for fmt in date_formats:
        try:
            parsed = datetime.strptime(cleaned, fmt).date()
            if parsed.year < 1990:
                return None, f"Date '{cleaned}' is outside supported year range"
            if parsed > today:
                return None, f"Future date '{parsed.isoformat()}' is not allowed"
            return parsed, None
        except ValueError:
            continue

    expected = ' or '.join(date_formats)
    return None, f"Invalid date '{cleaned}' (expected {expected})"


def _normalize_header_name(name: str) -> str:
    return clean_untrusted_text(name, 100).lower()


def _required_columns(config: BankFormatConfig) -> list[str]:
    cols = [config.date_column, config.narration_column]
    if config.amount_mode == 'split_columns':
        assert config.debit_column and config.credit_column
        cols.extend([config.debit_column, config.credit_column])
    else:
        assert config.amount_column
        cols.append(config.amount_column)
    return cols


def _locate_header_row(
    raw_rows: list[list[str]],
    config: BankFormatConfig,
) -> tuple[int, dict[str, int]]:
    """
    Skip preamble rows and locate the mapped CSV header row.

    Returns (header_row_index, {normalized_column_name: column_index}).
    """
    required_norm = {_normalize_header_name(c) for c in _required_columns(config)}
    start_idx = config.preamble_rows if config.strict_preamble else 0
    scan_end = min(len(raw_rows), start_idx + config.max_preamble_scan + config.preamble_rows + 1)

    for idx in range(start_idx, scan_end):
        row = raw_rows[idx]
        col_map: dict[str, int] = {}
        for col_idx, cell in enumerate(row):
            norm = _normalize_header_name(cell)
            if norm and norm not in col_map:
                col_map[norm] = col_idx
        if required_norm.issubset(col_map.keys()):
            return idx, col_map

    expected = ', '.join(_required_columns(config))
    raise StatementParseError(
        f"Could not find expected columns ({expected}) for format '{config.label}'."
    )


def infer_payment_method(narration: str, default_method: str = 'Net Banking') -> str:
    """Deterministically infer a validated ExpenseFlow payment method from bank narration."""
    upper = narration.upper()
    if 'UPI' in upper:
        method = 'UPI'
    elif any(tok in upper for tok in ('ATM', 'CASH WDL', 'CASH WITHDRAWAL')):
        method = 'Cash'
    elif any(tok in upper for tok in ('CREDIT CARD', 'CCARD', 'CC PMT', 'CC/')):
        method = 'Credit Card'
    elif any(tok in upper for tok in ('POS ', 'POS/', 'POS-', 'DEBIT CARD', 'RUPAY', 'VISA', 'MASTERCARD', 'ECOM')):
        method = 'Debit Card'
    elif any(tok in upper for tok in ('WALLET', 'PAYTM WALLET', 'AMAZON PAY', 'PHONEPE WALLET')):
        method = 'Digital Wallet'
    elif any(tok in upper for tok in ('NEFT', 'IMPS', 'RTGS', 'ACH', 'NACH', 'ECS', 'INB', 'BY TRANSFER', 'TO TRANSFER')):
        method = 'Net Banking'
    else:
        method = default_method

    return method if method in VALID_METHODS else 'Net Banking'


def _is_repeated_header_row(row: list[str], col_expectations: list[tuple[int, str]]) -> bool:
    """Return True when a post-header row repeats all configured statement column headers."""
    if not col_expectations:
        return False
    max_idx = max(idx for idx, _ in col_expectations)
    if len(row) <= max_idx:
        return False
    return all(_normalize_header_name(row[idx]) == expected for idx, expected in col_expectations)


def _is_statement_footer_row(
    row: list[str],
    date_col_idx: int,
    date_formats: tuple[str, ...],
    today: date,
) -> bool:
    """Detect trailing bank summary/disclaimer blocks after transaction rows without dropping invalid rows."""
    raw_date = row[date_col_idx] if date_col_idx < len(row) else ''
    _, date_err = _parse_statement_date(raw_date, date_formats, today)
    if date_err is None:
        return False

    first_cell = next(
        (clean_untrusted_text(cell, 200) for cell in row if clean_untrusted_text(cell, 200)),
        '',
    )
    if not first_cell:
        return False

    normalized = first_cell.lstrip('*- \t:').lower()
    return normalized.startswith(_STATEMENT_FOOTER_PREFIXES)


def suggest_rule_pattern(narration: str) -> str:
    """
    Extract a reusable merchant keyword from a noisy Indian bank narration.

    Strips banking rail prefixes, VPA handles, masked card numbers, IFSC codes,
    and numeric reference IDs so "always apply" saves a reusable merchant rule.
    """
    cleaned = clean_untrusted_text(narration, 200).upper()
    if not cleaned:
        return ''

    # Drop UPI VPA handle portion like blinkit@yesb before splitting
    cleaned = re.sub(r'[A-Z0-9._%+-]+@[A-Z0-9.-]+', ' ', cleaned)
    tokens = re.split(r'[/\-*|,;:\s]+', cleaned)

    meaningful: list[str] = []
    for tok in tokens:
        if not tok:
            continue
        if tok in _RULE_STOPWORDS:
            continue
        if _IFSC_LIKE_RE.match(tok) or _MASKED_CARD_RE.match(tok):
            continue
        # Drop pure digits or alphanumeric tokens that are mostly numbers (reference IDs)
        alpha_count = sum(1 for ch in tok if ch.isalpha())
        if alpha_count < 2:
            continue
        if len(tok) >= 8 and sum(1 for ch in tok if ch.isdigit()) >= 4:
            continue
        meaningful.append(tok)
        if len(meaningful) >= 1 and len(tok) >= 4:
            break
        if len(meaningful) >= 2:
            break

    if meaningful:
        return ' '.join(meaningful)[:120]

    # Fallback: first non-empty segment up to 40 chars
    fallback = re.split(r'[/\-|]', cleaned)[0].strip()
    return fallback[:40]


def categorise_narration(narration: str, rules: Iterable) -> str:
    """
    Deterministically assign a category using the user's saved rules first, else 'Uncategorised'.

    Longest normalized rule pattern matches first; ties break by newest rule id.
    """
    upper_narration = clean_untrusted_text(narration, 500).upper()
    if not upper_narration:
        return 'Uncategorised'

    ordered_rules = sorted(
        rules,
        key=lambda r: (len(r.pattern_normalized), r.id or 0),
        reverse=True,
    )
    for rule in ordered_rules:
        if rule.pattern_normalized and rule.pattern_normalized in upper_narration:
            if rule.category in VALID_CATEGORIES:
                return rule.category
    return 'Uncategorised'


def parse_statement_csv(
    raw_bytes: bytes,
    config: BankFormatConfig,
    *,
    today: date | None = None,
) -> ParsedStatement:
    """
    Parse an uploaded bank statement CSV completely in memory using `config`.

    Never silently drops malformed post-header rows: invalid rows are returned with
    clear reasons, credits are marked `skipped_credit`, and valid debits receive
    occurrence-aware SHA-256 fingerprints so two identical same-day rows stay distinct
    while re-uploading the same statement remains idempotent.
    """
    ref_today = today or datetime.now().date()
    text, encoding_used = decode_csv_bytes(raw_bytes)
    file_sha256 = hashlib.sha256(raw_bytes).hexdigest()

    try:
        raw_rows = list(csv.reader(io.StringIO(text, newline='')))
    except csv.Error as exc:
        raise StatementParseError(f'Malformed CSV syntax: {exc}') from exc

    if not raw_rows:
        raise StatementParseError('Uploaded CSV file contains no rows.')

    header_idx, col_map = _locate_header_row(raw_rows, config)
    header_row = raw_rows[header_idx]
    effective_header_len = len(header_row)
    while effective_header_len > 0 and not clean_untrusted_text(header_row[effective_header_len - 1], 100):
        effective_header_len -= 1

    date_col_idx = col_map[_normalize_header_name(config.date_column)]
    narr_col_idx = col_map[_normalize_header_name(config.narration_column)]
    ref_col_idx = col_map.get(_normalize_header_name(config.ref_column)) if config.ref_column else None

    debit_col_idx = (
        col_map[_normalize_header_name(config.debit_column)]
        if config.amount_mode == 'split_columns' and config.debit_column
        else None
    )
    credit_col_idx = (
        col_map[_normalize_header_name(config.credit_column)]
        if config.amount_mode == 'split_columns' and config.credit_column
        else None
    )
    amount_col_idx = (
        col_map[_normalize_header_name(config.amount_column)]
        if config.amount_mode == 'signed_amount' and config.amount_column
        else None
    )

    max_needed_idx = max(
        idx
        for idx in (date_col_idx, narr_col_idx, debit_col_idx, credit_col_idx, amount_col_idx)
        if idx is not None
    )

    parsed_rows: list[ParsedStatementRow] = []
    occ_counter: Counter[tuple[str, int, str, str]] = Counter()
    non_blank_data_rows = 0
    header_expectations = [
        (col_map[_normalize_header_name(col)], _normalize_header_name(col))
        for col in _required_columns(config)
    ]

    for row_idx in range(header_idx + 1, len(raw_rows)):
        line_number = row_idx + 1
        row = raw_rows[row_idx]
        if all(not clean_untrusted_text(cell, 200) for cell in row):
            continue

        if parsed_rows and _is_repeated_header_row(row, header_expectations):
            continue

        if parsed_rows and _is_statement_footer_row(row, date_col_idx, config.date_formats, ref_today):
            break

        non_blank_data_rows += 1
        if non_blank_data_rows > MAX_IMPORT_ROWS:
            raise StatementParseError(
                f'Statement exceeds the maximum of {MAX_IMPORT_ROWS} rows per import.'
            )

        raw_preview = clean_untrusted_text(' | '.join(row), 500)

        # Check structural column alignment (ignore trailing empty cells)
        trimmed_row_len = len(row)
        while trimmed_row_len > effective_header_len and not clean_untrusted_text(row[trimmed_row_len - 1], 100):
            trimmed_row_len -= 1
        if trimmed_row_len > effective_header_len:
            parsed_rows.append(ParsedStatementRow(
                line_number=line_number,
                row_status='invalid',
                txn_date=None,
                amount_paise=None,
                raw_narration='',
                description='',
                ref_no='',
                payment_method=config.default_payment_method,
                fingerprint=None,
                error_reason='Malformed CSV row: extra columns beyond header',
                raw_preview=raw_preview,
            ))
            continue

        if len(row) <= max_needed_idx:
            parsed_rows.append(ParsedStatementRow(
                line_number=line_number,
                row_status='invalid',
                txn_date=None,
                amount_paise=None,
                raw_narration='',
                description='',
                ref_no='',
                payment_method=config.default_payment_method,
                fingerprint=None,
                error_reason='Incomplete CSV row: missing required columns',
                raw_preview=raw_preview,
            ))
            continue

        raw_date = row[date_col_idx]
        raw_narr = row[narr_col_idx]
        raw_ref = row[ref_col_idx] if (ref_col_idx is not None and ref_col_idx < len(row)) else ''

        norm_narr = clean_untrusted_text(raw_narr, 500)
        norm_ref = clean_untrusted_text(raw_ref, 100)

        txn_date, date_err = _parse_statement_date(raw_date, config.date_formats, ref_today)
        if date_err:
            parsed_rows.append(ParsedStatementRow(
                line_number=line_number,
                row_status='invalid',
                txn_date=None,
                amount_paise=None,
                raw_narration=norm_narr,
                description=norm_narr[:255],
                ref_no=norm_ref,
                payment_method=config.default_payment_method,
                fingerprint=None,
                error_reason=date_err,
                raw_preview=raw_preview,
            ))
            continue

        if not norm_narr:
            parsed_rows.append(ParsedStatementRow(
                line_number=line_number,
                row_status='invalid',
                txn_date=txn_date,
                amount_paise=None,
                raw_narration='',
                description='',
                ref_no=norm_ref,
                payment_method=config.default_payment_method,
                fingerprint=None,
                error_reason='Missing transaction narration',
                raw_preview=raw_preview,
            ))
            continue

        # Parse debit/credit amounts
        is_credit = False
        amount_paise: int | None = None
        amt_error: str | None = None

        if config.amount_mode == 'split_columns':
            assert debit_col_idx is not None and credit_col_idx is not None
            deb_paise, deb_err = parse_statement_amount_to_paise(row[debit_col_idx], allow_signed=False)
            cred_paise, cred_err = parse_statement_amount_to_paise(row[credit_col_idx], allow_signed=False)

            if deb_err:
                amt_error = f'Debit column error: {deb_err}'
            elif cred_err:
                amt_error = f'Credit column error: {cred_err}'
            else:
                deb_pos = deb_paise is not None and deb_paise > 0
                cred_pos = cred_paise is not None and cred_paise > 0
                if deb_pos and cred_pos:
                    amt_error = 'Row contains both non-zero debit and credit amounts'
                elif deb_pos:
                    amount_paise = deb_paise
                elif cred_pos:
                    is_credit = True
                    amount_paise = cred_paise
                elif deb_paise == 0 or cred_paise == 0:
                    amt_error = 'Zero transaction amount'
                else:
                    amt_error = 'Both debit and credit amounts are empty'
        else:
            assert amount_col_idx is not None
            signed_paise, signed_err = parse_statement_amount_to_paise(
                row[amount_col_idx], allow_signed=True
            )
            if signed_err:
                amt_error = signed_err
            elif signed_paise is None:
                amt_error = 'Missing transaction amount'
            elif signed_paise == 0:
                amt_error = 'Zero transaction amount'
            else:
                is_debit = (signed_paise < 0) if config.signed_debit_negative else (signed_paise > 0)
                if is_debit:
                    amount_paise = abs(signed_paise)
                else:
                    is_credit = True
                    amount_paise = abs(signed_paise)

        if amt_error or amount_paise is None:
            parsed_rows.append(ParsedStatementRow(
                line_number=line_number,
                row_status='invalid',
                txn_date=txn_date,
                amount_paise=None,
                raw_narration=norm_narr,
                description=norm_narr[:255],
                ref_no=norm_ref,
                payment_method=config.default_payment_method,
                fingerprint=None,
                error_reason=amt_error or 'Invalid transaction amount',
                raw_preview=raw_preview,
            ))
            continue

        payment_method = infer_payment_method(norm_narr, config.default_payment_method)

        if is_credit:
            parsed_rows.append(ParsedStatementRow(
                line_number=line_number,
                row_status='skipped_credit',
                txn_date=txn_date,
                amount_paise=amount_paise,
                raw_narration=norm_narr,
                description=norm_narr[:255],
                ref_no=norm_ref,
                payment_method=payment_method,
                fingerprint=None,
                error_reason=None,
                raw_preview=raw_preview,
            ))
            continue

        # Valid debit row: compute occurrence-aware fingerprint so two identical same-day
        # transactions receive distinct fingerprints, while re-uploading the same statement is idempotent.
        occ_key = (txn_date.isoformat(), amount_paise, norm_narr.upper(), norm_ref.upper())
        occ_counter[occ_key] += 1
        occurrence = occ_counter[occ_key]
        fp_material = f'{occ_key[0]}|{occ_key[1]}|{occ_key[2]}|{occ_key[3]}|{occurrence}'
        fingerprint = hashlib.sha256(fp_material.encode('utf-8')).hexdigest()

        parsed_rows.append(ParsedStatementRow(
            line_number=line_number,
            row_status='ready',
            txn_date=txn_date,
            amount_paise=amount_paise,
            raw_narration=norm_narr,
            description=norm_narr[:255],
            ref_no=norm_ref,
            payment_method=payment_method,
            fingerprint=fingerprint,
            error_reason=None,
            raw_preview=raw_preview,
        ))

    if not parsed_rows:
        raise StatementParseError('No transaction rows were found after the statement header.')

    return ParsedStatement(
        bank_profile=config.key,
        detected_encoding=encoding_used,
        file_sha256=file_sha256,
        preamble_rows_skipped=header_idx,
        rows=parsed_rows,
    )


def match_manual_duplicate(
    *,
    txn_date: date,
    amount_paise: int,
    narration: str,
    manual_candidates: list,
    claimed_ids: set[int],
) -> int | None:
    """
    Find the best unclaimed manually entered Expense likely matching a CSV debit row.

    Matches exact integer paise within ±MANUAL_DUP_DATE_TOLERANCE_DAYS (1 day),
    preferring same-day matches and higher token overlap in description.
    """
    narr_tokens = {tok for tok in re.split(r'[^A-Z0-9]+', narration.upper()) if len(tok) >= 3}
    best_id: int | None = None
    best_score: tuple[int, int, int] | None = None

    for exp in manual_candidates:
        if exp.id in claimed_ids:
            continue
        if exp.amount_paise != amount_paise:
            continue
        day_diff = abs((exp.date - txn_date).days)
        if day_diff > MANUAL_DUP_DATE_TOLERANCE_DAYS:
            continue

        desc_tokens = {
            tok for tok in re.split(r'[^A-Z0-9]+', (exp.description or '').upper()) if len(tok) >= 3
        }
        overlap = len(narr_tokens & desc_tokens)
        # Higher score is better: exact date (day_diff=0 -> 1), token overlap, earliest id tie-break
        score = (1 if day_diff == 0 else 0, overlap, -exp.id)
        if best_score is None or score > best_score:
            best_score = score
            best_id = exp.id

    return best_id
