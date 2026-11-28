"""
statement_import.py — Multi-format bank statement parsing (CSV, XLSX, XLS, digital PDF),
in-memory password decryption, auto-detect & declarative mapping configs,
deterministic categorisation, idempotent deduplication, and running-balance reconciliation.

All parsing and decryption happen strictly in server memory without writing uploaded
files or passwords to disk or logs. Monetary values are parsed with Decimal
(ROUND_HALF_UP) into exact integer paise.
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
MAX_PDF_PAGES = 100
MAX_EXCEL_SCAN_ROWS = MAX_IMPORT_ROWS + MAX_PREAMBLE_SCAN + 500
MANUAL_DUP_DATE_TOLERANCE_DAYS = 1
ALLOWED_IMPORT_EXTENSIONS = ('.csv', '.xlsx', '.xls', '.pdf')

_PAISE_PER_RUPEE = Decimal('100')
_CURRENCY_PREFIX_RE = re.compile(r'^(?:₹|inr|rs\.?)\s*', re.IGNORECASE)
_TRAILING_DR_CR_RE = re.compile(r'\s*(?:\(\s*(dr|cr)\s*\)|\b(dr|cr)\.?)$', re.IGNORECASE)
_ASCII_GROUPED_DECIMAL_RE = re.compile(
    r'^[+-]?(?:(?:[0-9]+|[0-9]{1,3}(?:,[0-9]{2,3})+)(?:\.[0-9]+)?|\.[0-9]+)$'
)
_ASCII_DATE_CHARS_RE = re.compile(r'^[0-9A-Za-z/\-\s.,:]+$')
_CONTROL_CHARS_RE = re.compile(r'[\x00-\x1f\x7f-\x9f]')

_RULE_STOPWORDS = {
    'UPI', 'UPIAR', 'UPIAB', 'DR', 'CR', 'NEFT', 'RTGS', 'IMPS', 'POS', 'ATM', 'WDL',
    'DEP', 'TFR', 'INB', 'ACH', 'NACH', 'ECS', 'P2A', 'P2M', 'TRANSFER', 'PAYMENT',
    'PAY', 'TXN', 'REF', 'NO', 'INR', 'RS', 'TO', 'FROM', 'BY', 'AT', 'FOR', 'THE',
    'AND', 'MR', 'MS', 'MRS', 'BANK', 'INDIA', 'PVT', 'LTD', 'DEBIT', 'CREDIT', 'CARD',
    'ONLINE', 'ECOM', 'PURCHASE', 'WITHDRAWAL', 'DEPOSIT',
    'YESB', 'HDFC', 'SBIN', 'ICIC', 'UTIB', 'KKBK', 'BARB', 'PUNB', 'CNRB', 'IDFB',
    'UBIN', 'CBIN', 'BKID', 'AIRP', 'UCBA', 'IOBA', 'FDRL', 'PYTM', 'MAHB', 'SIBL',
    'INDB', 'CIUB', 'KVBL', 'TMBL', 'KARB', 'FINO', 'IPPB', 'JIOB',
}
_IFSC_LIKE_RE = re.compile(r'^[A-Z]{4}0[A-Z0-9]{6}$')
_MASKED_CARD_RE = re.compile(r'^[0-9]{4}[X*]{4,}[0-9]{2,4}$')
_PAGE_MARKER_RE = re.compile(r'^page\s+\d+(?:\s+of\s+\d+)?$', re.IGNORECASE)

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
    'this is a system generated document',
    'customers are requested to notify',
    'legends used in',
)

# Header synonym rankings for Auto-Detect across Indian and international bank exports
_DATE_HEADER_SYNONYMS = (
    'txn date', 'transaction date', 'trans date', 'value date',
    'posting date', 'posted on', 'entry date', 'date',
)
_NARRATION_HEADER_SYNONYMS = (
    'transaction remarks', 'transaction particulars', 'transaction details',
    'particulars', 'narration', 'details', 'remarks', 'description', 'memo', 'payee',
)
_REF_HEADER_SYNONYMS = (
    'ref no cheque no', 'chq ref no', 'transaction id', 'txn id',
    'ref no', 'reference no', 'reference', 'utr', 'cheque no',
)
_DEBIT_HEADER_SYNONYMS = (
    'debit amount', 'withdrawal', 'withdrawal amt', 'withdrawals', 'debit amt', 'debit', 'dr amount', 'dr',
)
_CREDIT_HEADER_SYNONYMS = (
    'credit amount', 'deposit', 'deposit amt', 'deposits', 'credit amt', 'credit', 'cr amount', 'cr',
)
_AMOUNT_HEADER_SYNONYMS = (
    'transaction amount', 'txn amount', 'amount', 'amt',
)
_TYPE_HEADER_SYNONYMS = (
    'dr cr', 'cr dr', 'transaction type', 'txn type', 'type', 'd c',
)
_BALANCE_HEADER_SYNONYMS = (
    'closing balance', 'running balance', 'available balance', 'balance', 'bal',
)


class StatementParseError(ValueError):
    """Raised when an uploaded statement fails file validation, decryption, or header detection."""


@dataclass(frozen=True)
class BankFormatConfig:
    """Declarative mapping config describing one bank statement table layout."""
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
    type_column: str | None = None
    signed_debit_negative: bool = True
    ref_column: str | None = None
    balance_column: str | None = None
    preamble_rows: int = 0
    strict_preamble: bool = False
    max_preamble_scan: int = MAX_PREAMBLE_SCAN
    default_payment_method: str = 'Net Banking'


BANK_FORMATS: dict[str, BankFormatConfig] = {
    'indian_standard': BankFormatConfig(
        key='indian_standard',
        label='Indian Bank Standard (DD/MM/YYYY · Withdrawal / Deposit)',
        description=(
            'Typical Indian bank export with metadata preamble rows, DD/MM/YYYY dates, '
            'Particulars narration, optional Chq./Ref.No., Withdrawal (INR) / Deposit (INR), and Balance.'
        ),
        date_column='Date',
        date_formats=('%d/%m/%Y', '%d-%m-%Y', '%d/%m/%y'),
        narration_column='Particulars',
        amount_mode='split_columns',
        debit_column='Withdrawal (INR)',
        credit_column='Deposit (INR)',
        ref_column='Chq./Ref.No.',
        balance_column='Balance (INR)',
        preamble_rows=5,
        strict_preamble=False,
        default_payment_method='Net Banking',
    ),
    'sbi_yono': BankFormatConfig(
        key='sbi_yono',
        label='SBI / YONO SBI (DD-MM-YYYY · Details / Debit / Credit)',
        description=(
            'State Bank of India (YONO Excel .xlsx/.csv) with metadata preamble, '
            'DD-MM-YYYY dates, Details narration, Ref No/Cheque No, Debit/Credit, and Balance.'
        ),
        date_column='Date',
        date_formats=('%d-%m-%Y', '%d/%m-%Y', '%d %b %Y', '%d-%b-%Y', '%d-%m-%y', '%d/%m/%y'),
        narration_column='Details',
        amount_mode='split_columns',
        debit_column='Debit',
        credit_column='Credit',
        ref_column='Ref No/Cheque No',
        balance_column='Balance',
        preamble_rows=12,
        strict_preamble=False,
        default_payment_method='Net Banking',
    ),
    'union_bank': BankFormatConfig(
        key='union_bank',
        label='Union Bank / Union Ease (DD-MM-YYYY · Remarks / Debit Amount / Credit Amount)',
        description=(
            'Union Bank of India statements (digital PDF or Excel/CSV) with Transaction Id, '
            'Remarks, (Dr)/(Cr) Debit Amount & Credit Amount columns, and Balance.'
        ),
        date_column='Date',
        date_formats=('%d-%m-%Y', '%d/%m-%Y', '%d-%m-%y', '%d %b %Y'),
        narration_column='Remarks',
        amount_mode='split_columns',
        debit_column='Debit Amount(₹)',
        credit_column='Credit Amount(₹)',
        ref_column='Transaction Id',
        balance_column='Balance(₹)',
        preamble_rows=0,
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
        date_formats=('%d-%m-%Y', '%d/%m-%Y', '%d %b %Y'),
        narration_column='Narration',
        amount_mode='split_columns',
        debit_column='Debit',
        credit_column='Credit',
        ref_column='Ref No',
        balance_column='Balance',
        preamble_rows=0,
        strict_preamble=False,
        default_payment_method='Net Banking',
    ),
    'signed_iso': BankFormatConfig(
        key='signed_iso',
        label='Signed Amount (YYYY-MM-DD · Negative = Debit)',
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
        balance_column='Balance',
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
    """Normalized in-memory representation of a single post-header statement row."""
    line_number: int
    row_status: str  # 'ready' | 'skipped_credit' | 'invalid'
    txn_date: date | None
    amount_paise: int | None
    running_balance_paise: int | None
    balance_mismatch_note: str | None
    raw_narration: str
    description: str
    ref_no: str
    payment_method: str
    fingerprint: str | None
    error_reason: str | None
    raw_preview: str


@dataclass(frozen=True)
class ParsedStatement:
    """Complete result of decoding and parsing a statement file in memory."""
    bank_profile: str
    detected_format_label: str
    detected_encoding: str
    file_sha256: str
    preamble_rows_skipped: int
    balance_check_status: str  # 'verified' | 'mismatch' | 'unavailable'
    balance_mismatch_count: int
    rows: list[ParsedStatementRow]


def clean_untrusted_text(raw: str | None, max_len: int) -> str:
    """Strip control chars, collapse whitespace, and bound length for untrusted text."""
    if raw is None or raw == '':
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
        label='Custom Column Mapping',
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
        balance_column='Balance',
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


def _strip_dr_cr_suffix(cleaned: str) -> tuple[str, str | None]:
    """Extract and strip trailing (Dr)/(Cr) or Dr/Cr markers from a statement number."""
    m = _TRAILING_DR_CR_RE.search(cleaned)
    if not m:
        return cleaned, None
    marker = (m.group(1) or m.group(2) or '').lower()
    stripped = cleaned[:m.start()].strip()
    return stripped, marker


def parse_statement_amount_to_paise(
    raw_value: str | None,
    *,
    allow_signed: bool = False,
    column_role: str | None = None,
) -> tuple[int | None, str | None]:
    """
    Parse a statement amount string into signed/unsigned integer paise using Decimal (ROUND_HALF_UP).

    Supports comma-grouped numbers (Indian `1,00,000.50` and Western `100,000.50`),
    optional currency markers (₹ / INR / Rs.), optional trailing `(Dr)`/`(Cr)` markers,
    and rejects NaN, Infinity, scientific notation, and non-ASCII digits.
    """
    cleaned = clean_untrusted_text(raw_value, 64)
    if not cleaned or cleaned in {'-', '--', '—'}:
        return None, None

    cleaned = _CURRENCY_PREFIX_RE.sub('', cleaned).strip()
    if not cleaned:
        return None, None

    cleaned, marker = _strip_dr_cr_suffix(cleaned)
    if marker and not cleaned:
        return None, f"Invalid amount '{clean_untrusted_text(raw_value, 40)}'"

    # Allow spaces around sign when currency sat between sign and number (e.g. "- ₹50.00")
    if cleaned and cleaned[0] in {'+', '-'}:
        sign_char = cleaned[0]
        rest = _CURRENCY_PREFIX_RE.sub('', cleaned[1:].strip()).strip()
        cleaned = sign_char + rest

    if not cleaned or not _ASCII_GROUPED_DECIMAL_RE.fullmatch(cleaned):
        return None, f"Invalid amount '{clean_untrusted_text(raw_value, 40)}'"

    if column_role == 'debit' and marker == 'cr':
        return None, f"Credit indicator '(Cr)' found in debit column ('{clean_untrusted_text(raw_value, 40)}')"
    if column_role == 'credit' and marker == 'dr':
        return None, f"Debit indicator '(Dr)' found in credit column ('{clean_untrusted_text(raw_value, 40)}')"

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

    if allow_signed and marker and cleaned[0] not in {'+', '-'}:
        paise = -abs(paise) if marker == 'dr' else abs(paise)
    elif allow_signed and marker:
        if (cleaned[0] == '-' and marker == 'cr') or (cleaned[0] == '+' and marker == 'dr'):
            return None, f"Conflicting sign and '{marker.upper()}' indicator in '{clean_untrusted_text(raw_value, 40)}'"

    return paise, None


def parse_statement_balance_to_paise(raw_value: str | None) -> int | None:
    """Parse an optional running-balance cell (including `(Cr)`/`(Dr)` suffixes) to signed integer paise."""
    cleaned = clean_untrusted_text(raw_value, 64)
    if not cleaned or cleaned in {'-', '--', '—'}:
        return None

    cleaned = _CURRENCY_PREFIX_RE.sub('', cleaned).strip()
    cleaned, marker = _strip_dr_cr_suffix(cleaned)
    if not cleaned or not _ASCII_GROUPED_DECIMAL_RE.fullmatch(cleaned):
        return None

    try:
        dec = Decimal(cleaned.replace(',', ''))
    except InvalidOperation:
        return None
    if not dec.is_finite() or abs(dec) > Decimal('1000000000'):
        return None

    paise = int((dec * _PAISE_PER_RUPEE).quantize(Decimal('1'), rounding=ROUND_HALF_UP))
    if marker == 'dr' and paise > 0:
        return -paise
    return paise


def _parse_statement_date(
    raw_value: str | None,
    date_formats: tuple[str, ...],
    today: date,
    *,
    allow_iso_excel_date: bool = False,
) -> tuple[date | None, str | None]:
    """Parse a date cell strictly against configured formats with ASCII digit validation."""
    cleaned = clean_untrusted_text(raw_value, 40)
    if not cleaned:
        return None, 'Missing transaction date'
    if not _ASCII_DATE_CHARS_RE.fullmatch(cleaned):
        return None, f"Invalid date '{cleaned}'"

    formats_to_try = list(date_formats)
    if allow_iso_excel_date and '%Y-%m-%d' not in formats_to_try:
        formats_to_try.append('%Y-%m-%d')

    for fmt in formats_to_try:
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


def _canonical_header_token(name: str) -> str:
    """Normalize bank header variations (e.g. 'Debit Amount(₹)' -> 'debit amount') for Auto-Detect."""
    lower = clean_untrusted_text(name, 100).lower()
    if not lower:
        return ''
    lower = re.sub(r'\(\s*(?:₹|inr|rs\.?)\s*\)', ' ', lower)
    lower = lower.replace('₹', ' ')
    lower = re.sub(r'[^a-z0-9]+', ' ', lower)
    return ' '.join(lower.split())


def _required_columns(config: BankFormatConfig) -> list[str]:
    cols = [config.date_column, config.narration_column]
    if config.amount_mode == 'split_columns':
        assert config.debit_column and config.credit_column
        cols.extend([config.debit_column, config.credit_column])
    else:
        assert config.amount_column
        cols.append(config.amount_column)
    return cols


def _build_header_col_map(row: list[str]) -> dict[str, int]:
    """Map normalized and canonical header tokens to the first matching column index."""
    col_map: dict[str, int] = {}
    for col_idx, cell in enumerate(row):
        norm = _normalize_header_name(cell)
        if norm and norm not in col_map:
            col_map[norm] = col_idx
    return col_map


def _find_column_index(col_map: dict[str, int], candidate: str | None) -> int | None:
    """Resolve exact normalized header name or canonical fallback for known columns."""
    if not candidate:
        return None
    norm = _normalize_header_name(candidate)
    if norm in col_map:
        return col_map[norm]
    canon = _canonical_header_token(candidate)
    if not canon:
        return None
    for existing_norm, idx in col_map.items():
        if _canonical_header_token(existing_norm) == canon:
            return idx
    return None


def _locate_header_row(
    raw_rows: list[list[str]],
    config: BankFormatConfig,
) -> tuple[int, dict[str, int]]:
    """Skip preamble rows and locate the mapped statement header row."""
    required = _required_columns(config)
    start_idx = config.preamble_rows if config.strict_preamble else 0
    scan_end = min(len(raw_rows), start_idx + config.max_preamble_scan + config.preamble_rows + 1)

    for idx in range(start_idx, scan_end):
        row = raw_rows[idx]
        col_map = _build_header_col_map(row)
        if all(_find_column_index(col_map, col) is not None for col in required):
            return idx, col_map

    expected = ', '.join(required)
    raise StatementParseError(
        f"Could not find expected columns ({expected}) for format '{config.label}'."
    )


def _match_synonym_column(canon_cols: list[tuple[int, str]], synonyms: tuple[str, ...]) -> int | None:
    """Find best column matching a synonym list (exact match before whole-token substring)."""
    for syn in synonyms:
        for idx, tok in canon_cols:
            if tok == syn:
                return idx
    for syn in synonyms:
        if len(syn) < 3:
            continue
        pattern = re.compile(rf'(?:^|\s){re.escape(syn)}(?:\s|$)')
        for idx, tok in canon_cols:
            if pattern.search(tok):
                return idx
    return None


def _infer_date_formats_from_rows(
    raw_rows: list[list[str]],
    header_idx: int,
    date_col_idx: int,
) -> tuple[str, ...]:
    """Infer unambiguous date format tuple from sample values below the header row."""
    samples: list[str] = []
    for row in raw_rows[header_idx + 1:]:
        if date_col_idx >= len(row):
            continue
        val = clean_untrusted_text(row[date_col_idx], 40)
        if val and any(ch.isdigit() for ch in val):
            samples.append(val)
            if len(samples) >= 25:
                break

    if any(re.fullmatch(r'\d{4}[-/]\d{1,2}[-/]\d{1,2}', s) for s in samples):
        return ('%Y-%m-%d', '%Y/%m/%d', '%d/%m/%Y', '%d-%m-%Y')

    if any(re.search(r'[A-Za-z]{3}', s) for s in samples):
        return ('%d-%b-%Y', '%d %b %Y', '%d-%B-%Y', '%d %B %Y', '%d-%b-%y', '%d-%m-%Y', '%d/%m/%Y')

    day_first_forced = False
    month_first_forced = False
    for s in samples:
        m = re.fullmatch(r'(\d{1,2})[/.\-](\d{1,2})[/.\-](\d{2,4})', s)
        if not m:
            continue
        first, second = int(m.group(1)), int(m.group(2))
        if first > 12:
            day_first_forced = True
        elif second > 12:
            month_first_forced = True

    if month_first_forced and not day_first_forced:
        return ('%m/%d/%Y', '%m-%d-%Y', '%m/%d/%y', '%m-%d-%y')

    # Indian statements default DD-MM-YYYY / DD/MM/YYYY cleanly when day_first_forced or <=12
    return ('%d-%m-%Y', '%d/%m-%Y', '%d.%m.%Y', '%d-%m-%y', '%d/%m/%y', '%d %b %Y', '%d-%b-%Y')


def _auto_detect_config(
    raw_rows: list[list[str]],
    source_kind: str,
) -> tuple[int, dict[str, int], BankFormatConfig]:
    """
    Automatically detect the header row, column roles, and date formats for almost
    any structured bank statement (CSV, XLSX, XLS, digital PDF).
    """
    scan_limit = min(len(raw_rows), MAX_PREAMBLE_SCAN + 20)
    best_candidate = None
    best_score = -1

    for idx in range(scan_limit):
        row = raw_rows[idx]
        canon_cols: list[tuple[int, str]] = []
        raw_col_map: dict[str, int] = {}
        for col_idx, cell in enumerate(row):
            norm = _normalize_header_name(cell)
            if norm and norm not in raw_col_map:
                raw_col_map[norm] = col_idx
            canon = _canonical_header_token(cell)
            if canon:
                canon_cols.append((col_idx, canon))

        if len(canon_cols) < 3:
            continue

        date_idx = _match_synonym_column(canon_cols, _DATE_HEADER_SYNONYMS)
        narr_idx = _match_synonym_column(canon_cols, _NARRATION_HEADER_SYNONYMS)
        if date_idx is None or narr_idx is None or date_idx == narr_idx:
            continue

        debit_idx = _match_synonym_column(canon_cols, _DEBIT_HEADER_SYNONYMS)
        credit_idx = _match_synonym_column(canon_cols, _CREDIT_HEADER_SYNONYMS)
        amount_idx = _match_synonym_column(canon_cols, _AMOUNT_HEADER_SYNONYMS)
        type_idx = _match_synonym_column(canon_cols, _TYPE_HEADER_SYNONYMS)
        ref_idx = _match_synonym_column(canon_cols, _REF_HEADER_SYNONYMS)
        balance_idx = _match_synonym_column(canon_cols, _BALANCE_HEADER_SYNONYMS)

        is_split = (
            debit_idx is not None
            and credit_idx is not None
            and len({date_idx, narr_idx, debit_idx, credit_idx}) == 4
        )
        is_single = amount_idx is not None and len({date_idx, narr_idx, amount_idx}) == 3
        if not is_split and not is_single:
            continue

        score = 10 + (4 if is_split else 2) + (2 if balance_idx is not None else 0) + (1 if ref_idx is not None else 0)
        if score > best_score:
            best_score = score
            best_candidate = (
                idx,
                row,
                raw_col_map,
                date_idx,
                narr_idx,
                debit_idx if is_split else None,
                credit_idx if is_split else None,
                None if is_split else amount_idx,
                None if is_split else type_idx,
                ref_idx,
                balance_idx,
            )

    if best_candidate is None:
        raise StatementParseError(
            'Could not automatically recognize Date, Narration, and Debit/Credit or Amount columns. '
            'Try selecting a specific bank format or Custom Column Mapping.'
        )

    (
        header_idx,
        header_row,
        raw_col_map,
        date_idx,
        narr_idx,
        debit_idx,
        credit_idx,
        amount_idx,
        type_idx,
        ref_idx,
        balance_idx,
    ) = best_candidate

    inferred_date_formats = _infer_date_formats_from_rows(raw_rows, header_idx, date_idx)

    def _col_header(c_idx: int | None) -> str | None:
        if c_idx is None or c_idx >= len(header_row):
            return None
        return clean_untrusted_text(header_row[c_idx], 100)

    if debit_idx is not None and credit_idx is not None:
        mode_desc = 'Split Debit/Credit'
        amount_mode = 'split_columns'
    elif type_idx is not None:
        mode_desc = 'Amount + Dr/Cr'
        amount_mode = 'signed_amount'
    else:
        mode_desc = 'Signed Amount'
        amount_mode = 'signed_amount'

    detected_label = f'Auto-Detect ({source_kind} · {mode_desc})'

    config = BankFormatConfig(
        key='auto_detect',
        label=detected_label,
        description='Automatically detected statement layout.',
        date_column=_col_header(date_idx) or 'Date',
        date_formats=inferred_date_formats,
        narration_column=_col_header(narr_idx) or 'Description',
        amount_mode=amount_mode,
        debit_column=_col_header(debit_idx),
        credit_column=_col_header(credit_idx),
        amount_column=_col_header(amount_idx),
        type_column=_col_header(type_idx),
        signed_debit_negative=True,
        ref_column=_col_header(ref_idx),
        balance_column=_col_header(balance_idx),
        preamble_rows=header_idx,
        strict_preamble=True,
        default_payment_method='Net Banking',
    )
    return header_idx, raw_col_map, config


def infer_payment_method(narration: str, default_method: str = 'Net Banking') -> str:
    """Deterministically infer a validated ExpenseFlow payment method from bank narration."""
    upper = narration.upper()
    if 'UPI' in upper:
        method = 'UPI'
    elif any(tok in upper for tok in ('ATM', 'CASH WDL', 'CASH WITHDRAWAL')):
        method = 'Cash'
    elif any(tok in upper for tok in ('CREDIT CARD', 'CCARD', 'CC PMT', 'CC/')):
        method = 'Credit Card'
    elif any(tok in upper for tok in ('POS ', 'POS/', 'POS-', 'POS:', 'DEBIT CARD', 'RUPAY', 'VISA', 'MASTERCARD', 'ECOM')):
        method = 'Debit Card'
    elif any(tok in upper for tok in ('WALLET', 'PAYTM WALLET', 'AMAZON PAY', 'PHONEPE WALLET')):
        method = 'Digital Wallet'
    elif any(tok in upper for tok in ('NEFT', 'IMPS', 'RTGS', 'ACH', 'NACH', 'ECS', 'INB', 'BY TRANSFER', 'TO TRANSFER')):
        method = 'Net Banking'
    else:
        method = default_method

    return method if method in VALID_METHODS else 'Net Banking'


def suggest_rule_pattern(narration: str) -> str:
    """
    Extract a reusable merchant keyword from a noisy Indian bank narration.

    Strips banking rail prefixes (`WDL TFR`, `UPIAR`, `UPIAB`), VPA handles, masked card numbers,
    IFSC/bank codes, and numeric reference IDs so "always apply" saves a clean merchant rule.
    """
    cleaned = clean_untrusted_text(narration, 200).upper()
    if not cleaned:
        return ''

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
    *,
    allow_iso_excel_date: bool = False,
) -> bool:
    """Detect trailing bank summary/disclaimer blocks after transaction rows without dropping invalid rows."""
    raw_date = row[date_col_idx] if date_col_idx < len(row) else ''
    _, date_err = _parse_statement_date(
        raw_date,
        date_formats,
        today,
        allow_iso_excel_date=allow_iso_excel_date,
    )
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


def _format_cell_value(val) -> str:
    """Convert raw Excel/PDF cell values cleanly into normalized strings without float scientific drift."""
    if val is None:
        return ''
    if isinstance(val, datetime):
        return val.date().isoformat()
    if isinstance(val, date):
        return val.isoformat()
    if isinstance(val, bool):
        return str(val)
    if isinstance(val, int):
        return str(val)
    if isinstance(val, float):
        try:
            d = Decimal(str(val))
            if not d.is_finite():
                return str(val)
            txt = format(d, 'f')
            if '.' in txt:
                txt = txt.rstrip('0').rstrip('.')
            return txt
        except InvalidOperation:
            return str(val)
    return str(val)


def _extract_csv_rows(raw_bytes: bytes) -> tuple[list[list[str]], str, str, bool]:
    text, encoding_used = decode_csv_bytes(raw_bytes)
    try:
        raw_rows = list(csv.reader(io.StringIO(text, newline='')))
    except csv.Error as exc:
        raise StatementParseError(f'Malformed CSV syntax: {exc}') from exc
    if not raw_rows:
        raise StatementParseError('Uploaded CSV file contains no rows.')
    return raw_rows, encoding_used, 'CSV', False


def _score_candidate_sheet_rows(rows: list[list[str]]) -> int:
    """Score extracted worksheet rows so multi-sheet Excel workbooks pick the statement sheet."""
    score = len(rows)
    for row in rows[:MAX_PREAMBLE_SCAN]:
        canon_cols = [(i, _canonical_header_token(c)) for i, c in enumerate(row) if _canonical_header_token(c)]
        if len(canon_cols) < 3:
            continue
        if (
            _match_synonym_column(canon_cols, _DATE_HEADER_SYNONYMS) is not None
            and _match_synonym_column(canon_cols, _NARRATION_HEADER_SYNONYMS) is not None
        ):
            score += 10_000
            break
    return score


def _extract_xlsx_rows(
    raw_bytes: bytes,
    password: str | None = None,
) -> tuple[list[list[str]], str, str, bool]:
    if not raw_bytes:
        raise StatementParseError('Uploaded Excel (.xlsx) file is empty.')
    if len(raw_bytes) > MAX_IMPORT_FILE_BYTES:
        raise StatementParseError('Statement file exceeds the 2 MB maximum size limit.')

    import msoffcrypto
    import openpyxl

    stream: io.BytesIO = io.BytesIO(raw_bytes)
    detected_mode = 'xlsx'

    # Encrypted Excel (.xlsx) wraps contents in an encrypted OLE2 container
    is_encrypted = False
    try:
        office_file = msoffcrypto.OfficeFile(stream)
        is_encrypted = office_file.is_encrypted()
    except Exception:
        is_encrypted = False

    if is_encrypted:
        if not password:
            raise StatementParseError(
                'This Excel (.xlsx) statement is password-protected. Please enter its password and try again.'
            )
        stream.seek(0)
        office_file = msoffcrypto.OfficeFile(stream)
        decrypted = io.BytesIO()
        try:
            office_file.load_key(password=password)
            office_file.decrypt(decrypted)
        except Exception as exc:
            raise StatementParseError(
                'Incorrect password for the uploaded Excel (.xlsx) statement.'
            ) from exc
        decrypted.seek(0)
        stream = decrypted
        detected_mode = 'xlsx-encrypted'
    else:
        stream.seek(0)

    wb = None
    try:
        wb = openpyxl.load_workbook(stream, read_only=True, data_only=True)
        best_rows: list[list[str]] = []
        best_score = -1
        for sheet_name in wb.sheetnames[:10]:
            ws = wb[sheet_name]
            sheet_rows: list[list[str]] = []
            for row_idx, row_vals in enumerate(ws.iter_rows(values_only=True)):
                if row_idx >= MAX_EXCEL_SCAN_ROWS:
                    break
                sheet_rows.append([_format_cell_value(v) for v in row_vals])
            score = _score_candidate_sheet_rows(sheet_rows)
            if score > best_score:
                best_score = score
                best_rows = sheet_rows
    except StatementParseError:
        raise
    except Exception as exc:
        raise StatementParseError(
            'Unable to read Excel (.xlsx) file. Ensure it is a valid workbook.'
        ) from exc
    finally:
        if wb is not None:
            wb.close()

    if not best_rows:
        raise StatementParseError('Uploaded Excel (.xlsx) file contains no rows.')
    return best_rows, detected_mode, 'XLSX', True


def _extract_xls_rows(
    raw_bytes: bytes,
    password: str | None = None,
) -> tuple[list[list[str]], str, str, bool]:
    if not raw_bytes:
        raise StatementParseError('Uploaded Excel (.xls) file is empty.')
    if len(raw_bytes) > MAX_IMPORT_FILE_BYTES:
        raise StatementParseError('Statement file exceeds the 2 MB maximum size limit.')

    import xlrd

    stream: io.BytesIO = io.BytesIO(raw_bytes)
    detected_mode = 'xls'
    xls_bytes = raw_bytes

    try:
        import msoffcrypto

        office_file = msoffcrypto.OfficeFile(stream)
        if office_file.is_encrypted():
            if not password:
                raise StatementParseError(
                    'This Excel (.xls) statement is password-protected. Please enter its password and try again.'
                )
            decrypted = io.BytesIO()
            try:
                office_file.load_key(password=password)
                office_file.decrypt(decrypted)
            except Exception as exc:
                raise StatementParseError(
                    'Incorrect password for the uploaded Excel (.xls) statement.'
                ) from exc
            xls_bytes = decrypted.getvalue()
            detected_mode = 'xls-encrypted'
    except StatementParseError:
        raise
    except Exception:
        pass

    try:
        wb = xlrd.open_workbook(file_contents=xls_bytes)
    except xlrd.XLRDError as exc:
        msg = str(exc).lower()
        if 'encrypt' in msg or 'password' in msg:
            raise StatementParseError(
                'This legacy Excel (.xls) file uses an unsupported encryption mode. Save As CSV UTF-8 or XLSX and upload again.'
            ) from exc
        raise StatementParseError('Unable to read legacy Excel (.xls) file.') from exc

    best_rows: list[list[str]] = []
    best_score = -1
    for sheet in wb.sheets()[:10]:
        sheet_rows: list[list[str]] = []
        row_cap = min(sheet.nrows, MAX_EXCEL_SCAN_ROWS)
        for r_idx in range(row_cap):
            row_out: list[str] = []
            for c_idx in range(sheet.ncols):
                cell = sheet.cell(r_idx, c_idx)
                if cell.ctype == xlrd.XL_CELL_DATE:
                    dt = xlrd.xldate_as_datetime(cell.value, wb.datemode)
                    row_out.append(dt.date().isoformat())
                elif cell.ctype == xlrd.XL_CELL_NUMBER:
                    row_out.append(_format_cell_value(cell.value))
                else:
                    row_out.append(str(cell.value or ''))
            sheet_rows.append(row_out)
        score = _score_candidate_sheet_rows(sheet_rows)
        if score > best_score:
            best_score = score
            best_rows = sheet_rows

    if not best_rows:
        raise StatementParseError('Uploaded Excel (.xls) file contains no rows.')
    return best_rows, detected_mode, 'XLS', True


def _pdf_has_candidate_header(rows: list[list[str]]) -> bool:
    for row in rows[:MAX_PREAMBLE_SCAN]:
        canon_cols = [(i, _canonical_header_token(c)) for i, c in enumerate(row) if _canonical_header_token(c)]
        if len(canon_cols) < 3:
            continue
        if (
            _match_synonym_column(canon_cols, _DATE_HEADER_SYNONYMS) is not None
            and _match_synonym_column(canon_cols, _NARRATION_HEADER_SYNONYMS) is not None
        ):
            return True
    return False


def _extract_borderless_pdf_rows(pages) -> list[list[str]]:
    """
    Reconstruct rows from borderless digital bank statement PDFs using word x-coordinates.

    Detects the header line using synonym matching, assigns every word below the header into
    column buckets cleanly, skips repeated headers/page footers, and joins wrapped narration lines.
    """
    word_lines: list[list[dict]] = []
    for page in pages:
        words = page.extract_words(keep_blank_chars=False, use_text_flow=False) or []
        page_lines: list[list[dict]] = []
        for w in sorted(words, key=lambda item: (round(float(item['top']), 1), float(item['x0']))):
            if not page_lines or abs(float(w['top']) - float(page_lines[-1][0]['top'])) > 4.0:
                page_lines.append([w])
            else:
                page_lines[-1].append(w)
        word_lines.extend(page_lines)

    if not word_lines:
        return []

    def _group_header_words(line_words: list[dict]) -> list[dict]:
        cols: list[dict] = []
        for w in sorted(line_words, key=lambda item: float(item['x0'])):
            x0, x1 = float(w['x0']), float(w['x1'])
            if cols and (x0 - cols[-1]['x1']) <= 14.0:
                cols[-1]['text'] = f"{cols[-1]['text']} {w['text']}"
                cols[-1]['x1'] = max(cols[-1]['x1'], x1)
            else:
                cols.append({'text': w['text'], 'x0': x0, 'x1': x1})
        return cols

    header_pos: int | None = None
    cols: list[dict] = []
    date_idx: int | None = None
    narr_idx: int | None = None
    debit_idx: int | None = None
    credit_idx: int | None = None
    amount_idx: int | None = None
    balance_idx: int | None = None

    for i, ln in enumerate(word_lines[:MAX_PREAMBLE_SCAN + 20]):
        candidate_cols = _group_header_words(ln)
        if len(candidate_cols) < 3:
            continue
        canon = [(idx, _canonical_header_token(c['text'])) for idx, c in enumerate(candidate_cols)]
        d_i = _match_synonym_column(canon, _DATE_HEADER_SYNONYMS)
        n_i = _match_synonym_column(canon, _NARRATION_HEADER_SYNONYMS)
        deb_i = _match_synonym_column(canon, _DEBIT_HEADER_SYNONYMS)
        cred_i = _match_synonym_column(canon, _CREDIT_HEADER_SYNONYMS)
        amt_i = _match_synonym_column(canon, _AMOUNT_HEADER_SYNONYMS)
        bal_i = _match_synonym_column(canon, _BALANCE_HEADER_SYNONYMS)
        has_amt = (deb_i is not None and cred_i is not None) or amt_i is not None
        if d_i is not None and n_i is not None and has_amt:
            header_pos = i
            cols = candidate_cols
            date_idx, narr_idx = d_i, n_i
            debit_idx, credit_idx, amount_idx, balance_idx = deb_i, cred_i, amt_i, bal_i
            break

    if header_pos is None or date_idx is None or narr_idx is None:
        return []

    bounds: list[tuple[float, float]] = []
    for i, col in enumerate(cols):
        left = 0.0 if i == 0 else max(cols[i - 1]['x1'] + 2.0, col['x0'] - 16.0)
        right = 1e9 if i == len(cols) - 1 else max(col['x1'] + 2.0, cols[i + 1]['x0'] - 16.0)
        bounds.append((left, right))

    header_row = [c['text'] for c in cols]
    out_rows: list[list[str]] = [header_row]
    col_expectations = [
        (idx, _normalize_header_name(header_row[idx]))
        for idx in (date_idx, narr_idx, debit_idx, credit_idx, amount_idx)
        if idx is not None
    ]
    amt_or_bal_indices = [idx for idx in (debit_idx, credit_idx, amount_idx, balance_idx) if idx is not None]

    for ln in word_lines[header_pos + 1:]:
        row_cells = ['' for _ in cols]
        for w in sorted(ln, key=lambda item: float(item['x0'])):
            cx = (float(w['x0']) + float(w['x1'])) / 2.0
            target = None
            for j, (b_left, b_right) in enumerate(bounds):
                if b_left <= cx <= b_right:
                    target = j
                    break
            if target is None:
                target = min(
                    range(len(cols)),
                    key=lambda idx: abs(cx - ((cols[idx]['x0'] + cols[idx]['x1']) / 2.0)),
                )
            row_cells[target] = (row_cells[target] + ' ' + w['text']).strip()

        if all(not c for c in row_cells):
            continue
        if _is_repeated_header_row(row_cells, col_expectations):
            continue

        first_txt = next((c for c in row_cells if c), '')
        if _PAGE_MARKER_RE.fullmatch(first_txt):
            continue

        norm_first = first_txt.lstrip('*- \t:').lower()
        if len(out_rows) > 1 and norm_first.startswith(_STATEMENT_FOOTER_PREFIXES):
            out_rows.append(row_cells)
            break

        # Merge wrapped continuation lines of multi-line narrations into the preceding transaction
        has_date = bool(row_cells[date_idx])
        has_money = any(bool(row_cells[idx]) for idx in amt_or_bal_indices)
        has_narr = bool(row_cells[narr_idx])
        if len(out_rows) > 1 and not has_date and not has_money and has_narr:
            out_rows[-1][narr_idx] = (out_rows[-1][narr_idx] + ' ' + row_cells[narr_idx]).strip()
            continue

        out_rows.append(row_cells)

    return out_rows


def _extract_pdf_rows(
    raw_bytes: bytes,
    password: str | None = None,
) -> tuple[list[list[str]], str, str, bool]:
    if not raw_bytes:
        raise StatementParseError('Uploaded PDF file is empty.')
    if len(raw_bytes) > MAX_IMPORT_FILE_BYTES:
        raise StatementParseError('Statement file exceeds the 2 MB maximum size limit.')
    if not raw_bytes.lstrip().startswith(b'%PDF-'):
        raise StatementParseError('Uploaded file does not appear to be a valid PDF document.')

    import pdfplumber
    from pdfminer.pdfdocument import PDFPasswordIncorrect
    from pdfplumber.utils.exceptions import PdfminerException

    detected_mode = 'pdf'
    try:
        with pdfplumber.open(io.BytesIO(raw_bytes), password=password or '') as pdf:
            page_count = len(pdf.pages)
            if page_count == 0:
                raise StatementParseError('Uploaded PDF document contains 0 pages.')
            if page_count > MAX_PDF_PAGES:
                raise StatementParseError(
                    f'PDF exceeds the maximum of {MAX_PDF_PAGES} pages per upload.'
                )

            if password and getattr(pdf.doc, 'encryption', None) is not None:
                detected_mode = 'pdf-encrypted'

            has_selectable_text = False
            lattice_rows: list[list[str]] = []

            for page in pdf.pages:
                page_txt = (page.extract_text() or '').strip()
                if page_txt:
                    has_selectable_text = True

                for tbl in page.extract_tables() or []:
                    if not tbl or max((len(r) for r in tbl), default=0) < 3:
                        continue
                    for r in tbl:
                        lattice_rows.append([cell or '' for cell in r])

            if not has_selectable_text:
                raise StatementParseError(
                    'This PDF appears to be a scanned/image-only document with no selectable text. '
                    'ExpenseFlow supports digital text PDFs, Excel (.xlsx/.xls), and CSV files.'
                )

            if lattice_rows and _pdf_has_candidate_header(lattice_rows):
                chosen_rows = lattice_rows
            else:
                borderless_rows = _extract_borderless_pdf_rows(pdf.pages)
                chosen_rows = borderless_rows or lattice_rows

            if not chosen_rows:
                raise StatementParseError(
                    'Could not extract a transaction table from this PDF. '
                    'Ensure it is a digital bank statement PDF (or export Excel/CSV from your bank portal).'
                )
            return chosen_rows, detected_mode, 'PDF', False
    except (PDFPasswordIncorrect, PdfminerException) as exc:
        inner = exc.args[0] if (isinstance(exc, PdfminerException) and exc.args) else exc
        if (
            isinstance(exc, PDFPasswordIncorrect)
            or isinstance(inner, PDFPasswordIncorrect)
            or isinstance(exc.__cause__, PDFPasswordIncorrect)
        ):
            if not password:
                raise StatementParseError(
                    'This PDF statement is password-protected. Please enter its password and upload again.'
                ) from exc
            raise StatementParseError(
                'Incorrect password for this PDF statement. Please double-check and try again.'
            ) from exc
        raise StatementParseError(
            'Unable to parse the uploaded PDF file. Ensure it is an uncorrupted digital bank PDF.'
        ) from exc
    except StatementParseError:
        raise
    except Exception as exc:
        raise StatementParseError(
            'Unable to parse the uploaded PDF file. Ensure it is an uncorrupted digital bank PDF.'
        ) from exc


def _parse_amount_columns(
    row: list[str],
    config: BankFormatConfig,
    *,
    debit_col_idx: int | None,
    credit_col_idx: int | None,
    amount_col_idx: int | None,
    type_col_idx: int | None,
) -> tuple[bool, int | None, str | None]:
    """Parse debit/credit state and positive integer paise from split or single-amount layouts."""
    if config.amount_mode == 'split_columns':
        assert debit_col_idx is not None and credit_col_idx is not None
        deb_paise, deb_err = parse_statement_amount_to_paise(
            row[debit_col_idx], allow_signed=False, column_role='debit'
        )
        cred_paise, cred_err = parse_statement_amount_to_paise(
            row[credit_col_idx], allow_signed=False, column_role='credit'
        )
        if deb_err:
            return False, None, f'Debit column error: {deb_err}'
        if cred_err:
            return False, None, f'Credit column error: {cred_err}'

        deb_pos = deb_paise is not None and deb_paise > 0
        cred_pos = cred_paise is not None and cred_paise > 0
        if deb_pos and cred_pos:
            return False, None, 'Row contains both non-zero debit and credit amounts'
        if deb_pos:
            return False, deb_paise, None
        if cred_pos:
            return True, cred_paise, None
        if deb_paise == 0 or cred_paise == 0:
            return False, None, 'Zero transaction amount'
        return False, None, 'Both debit and credit amounts are empty'

    assert amount_col_idx is not None
    signed_paise, signed_err = parse_statement_amount_to_paise(
        row[amount_col_idx], allow_signed=True, column_role='signed'
    )
    if signed_err:
        return False, None, signed_err
    if signed_paise is None:
        return False, None, 'Missing transaction amount'
    if signed_paise == 0:
        return False, None, 'Zero transaction amount'

    if type_col_idx is not None and type_col_idx < len(row):
        type_tok = clean_untrusted_text(row[type_col_idx], 24).upper().rstrip('.')
        if type_tok in {'DR', 'D', 'DEBIT', 'WITHDRAWAL'}:
            return False, abs(signed_paise), None
        if type_tok in {'CR', 'C', 'CREDIT', 'DEPOSIT'}:
            return True, abs(signed_paise), None

    is_debit = (signed_paise < 0) if config.signed_debit_negative else (signed_paise > 0)
    return (not is_debit), abs(signed_paise), None


def _format_paise_inline(paise: int) -> str:
    """Format signed integer paise cleanly as ₹ string for balance mismatch notes."""
    sign = '-' if paise < 0 else ''
    abs_val = abs(paise)
    rupees, rem = divmod(abs_val, 100)
    return f'{sign}₹{rupees:,}.{rem:02d}'


def _reconcile_running_balances(
    rows: list[ParsedStatementRow],
) -> tuple[list[ParsedStatementRow], str, int]:
    """
    Deterministically verify `Prev Balance + Credit - Debit == Row Balance` across consecutive rows.

    Automatically detects whether the statement is sorted ascending (oldest-first) or descending
    (newest-first) before reporting mismatches on exact offending rows.
    """
    pairs: list[tuple[int, int]] = []
    last_valid_idx: int | None = None

    for idx, row in enumerate(rows):
        if row.row_status == 'invalid':
            # Do not chain across unparsed rows so we don't falsely blame adjacent valid rows
            last_valid_idx = None
            continue
        if row.running_balance_paise is None or row.amount_paise is None:
            last_valid_idx = None
            continue
        if last_valid_idx is not None:
            pairs.append((last_valid_idx, idx))
        last_valid_idx = idx

    if not pairs:
        return rows, 'unavailable', 0

    def _signed_delta(r: ParsedStatementRow) -> int:
        assert r.amount_paise is not None
        return r.amount_paise if r.row_status == 'skipped_credit' else -r.amount_paise

    asc_hits = sum(
        1 for prev_i, curr_i in pairs
        if rows[curr_i].running_balance_paise
        == (rows[prev_i].running_balance_paise or 0) + _signed_delta(rows[curr_i])
    )
    desc_hits = sum(
        1 for prev_i, curr_i in pairs
        if rows[prev_i].running_balance_paise
        == (rows[curr_i].running_balance_paise or 0) + _signed_delta(rows[prev_i])
    )
    ascending = asc_hits >= desc_hits

    notes_by_idx: dict[int, str] = {}
    for prev_i, curr_i in pairs:
        prev_row = rows[prev_i]
        curr_row = rows[curr_i]
        assert prev_row.running_balance_paise is not None and curr_row.running_balance_paise is not None

        if ascending:
            expected_bal = prev_row.running_balance_paise + _signed_delta(curr_row)
            actual_bal = curr_row.running_balance_paise
            target_idx = curr_i
        else:
            expected_bal = curr_row.running_balance_paise + _signed_delta(prev_row)
            actual_bal = prev_row.running_balance_paise
            target_idx = prev_i

        if expected_bal != actual_bal:
            diff = abs(actual_bal - expected_bal)
            notes_by_idx[target_idx] = (
                f'Balance check mismatch: expected {_format_paise_inline(expected_bal)}, '
                f'statement shows {_format_paise_inline(actual_bal)} (diff {_format_paise_inline(diff)})'
            )[:255]

    if not notes_by_idx:
        return rows, 'verified', 0

    updated_rows = [
        ParsedStatementRow(
            line_number=r.line_number,
            row_status=r.row_status,
            txn_date=r.txn_date,
            amount_paise=r.amount_paise,
            running_balance_paise=r.running_balance_paise,
            balance_mismatch_note=notes_by_idx.get(idx),
            raw_narration=r.raw_narration,
            description=r.description,
            ref_no=r.ref_no,
            payment_method=r.payment_method,
            fingerprint=r.fingerprint,
            error_reason=r.error_reason,
            raw_preview=r.raw_preview,
        )
        for idx, r in enumerate(rows)
    ]
    return updated_rows, 'mismatch', len(notes_by_idx)


def _parse_tabular_rows(
    raw_rows: list[list[str]],
    *,
    file_sha256: str,
    encoding_used: str,
    source_kind: str,
    allow_iso_excel_date: bool,
    config: BankFormatConfig | None,
    today: date | None,
) -> ParsedStatement:
    """Core mapping, validation, deduplication, and running-balance verification over normalized rows."""
    ref_today = today or datetime.now().date()
    if config is None:
        header_idx, col_map, effective_config = _auto_detect_config(raw_rows, source_kind)
    else:
        header_idx, col_map = _locate_header_row(raw_rows, config)
        effective_config = config

    header_row = raw_rows[header_idx]
    effective_header_len = len(header_row)
    while effective_header_len > 0 and not clean_untrusted_text(header_row[effective_header_len - 1], 100):
        effective_header_len -= 1

    date_col_idx = _find_column_index(col_map, effective_config.date_column)
    narr_col_idx = _find_column_index(col_map, effective_config.narration_column)
    assert date_col_idx is not None and narr_col_idx is not None

    ref_col_idx = _find_column_index(col_map, effective_config.ref_column)
    balance_col_idx = _find_column_index(col_map, effective_config.balance_column)
    if balance_col_idx is None:
        # Opportunistically find a Balance column for verification even when preset omits its exact alias
        canon_cols = [(i, _canonical_header_token(c)) for i, c in enumerate(header_row) if _canonical_header_token(c)]
        balance_col_idx = _match_synonym_column(canon_cols, _BALANCE_HEADER_SYNONYMS)

    debit_col_idx = (
        _find_column_index(col_map, effective_config.debit_column)
        if effective_config.amount_mode == 'split_columns'
        else None
    )
    credit_col_idx = (
        _find_column_index(col_map, effective_config.credit_column)
        if effective_config.amount_mode == 'split_columns'
        else None
    )
    amount_col_idx = (
        _find_column_index(col_map, effective_config.amount_column)
        if effective_config.amount_mode == 'signed_amount'
        else None
    )
    type_col_idx = (
        _find_column_index(col_map, effective_config.type_column)
        if effective_config.amount_mode == 'signed_amount' and effective_config.type_column
        else None
    )

    max_needed_idx = max(
        idx
        for idx in (date_col_idx, narr_col_idx, debit_col_idx, credit_col_idx, amount_col_idx)
        if idx is not None
    )

    header_expectations: list[tuple[int, str]] = []
    for req_col in _required_columns(effective_config):
        r_idx = _find_column_index(col_map, req_col)
        if r_idx is not None and r_idx < len(header_row):
            header_expectations.append((r_idx, _normalize_header_name(header_row[r_idx])))

    parsed_rows: list[ParsedStatementRow] = []
    occ_counter: Counter[tuple[str, int, str, str]] = Counter()
    non_blank_data_rows = 0

    for row_idx in range(header_idx + 1, len(raw_rows)):
        line_number = row_idx + 1
        row = raw_rows[row_idx]
        if all(not clean_untrusted_text(cell, 200) for cell in row):
            continue

        if parsed_rows and _is_repeated_header_row(row, header_expectations):
            continue

        first_non_empty = next(
            (clean_untrusted_text(c, 80) for c in row if clean_untrusted_text(c, 80)),
            '',
        )
        if parsed_rows and _PAGE_MARKER_RE.fullmatch(first_non_empty):
            continue

        if parsed_rows and _is_statement_footer_row(
            row,
            date_col_idx,
            effective_config.date_formats,
            ref_today,
            allow_iso_excel_date=allow_iso_excel_date,
        ):
            break

        non_blank_data_rows += 1
        if non_blank_data_rows > MAX_IMPORT_ROWS:
            raise StatementParseError(
                f'Statement exceeds the maximum of {MAX_IMPORT_ROWS} rows per import.'
            )

        raw_preview = clean_untrusted_text(' | '.join(row), 500)

        trimmed_row_len = len(row)
        while trimmed_row_len > effective_header_len and not clean_untrusted_text(row[trimmed_row_len - 1], 100):
            trimmed_row_len -= 1
        if trimmed_row_len > effective_header_len:
            parsed_rows.append(ParsedStatementRow(
                line_number=line_number,
                row_status='invalid',
                txn_date=None,
                amount_paise=None,
                running_balance_paise=None,
                balance_mismatch_note=None,
                raw_narration='',
                description='',
                ref_no='',
                payment_method=effective_config.default_payment_method,
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
                running_balance_paise=None,
                balance_mismatch_note=None,
                raw_narration='',
                description='',
                ref_no='',
                payment_method=effective_config.default_payment_method,
                fingerprint=None,
                error_reason='Incomplete CSV row: missing required columns',
                raw_preview=raw_preview,
            ))
            continue

        raw_date = row[date_col_idx]
        raw_narr = row[narr_col_idx]
        raw_ref = row[ref_col_idx] if (ref_col_idx is not None and ref_col_idx < len(row)) else ''
        running_bal = (
            parse_statement_balance_to_paise(row[balance_col_idx])
            if (balance_col_idx is not None and balance_col_idx < len(row))
            else None
        )

        norm_narr = clean_untrusted_text(raw_narr, 500)
        norm_ref = clean_untrusted_text(raw_ref, 100)

        txn_date, date_err = _parse_statement_date(
            raw_date,
            effective_config.date_formats,
            ref_today,
            allow_iso_excel_date=allow_iso_excel_date,
        )
        if date_err:
            parsed_rows.append(ParsedStatementRow(
                line_number=line_number,
                row_status='invalid',
                txn_date=None,
                amount_paise=None,
                running_balance_paise=running_bal,
                balance_mismatch_note=None,
                raw_narration=norm_narr,
                description=norm_narr[:255],
                ref_no=norm_ref,
                payment_method=effective_config.default_payment_method,
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
                running_balance_paise=running_bal,
                balance_mismatch_note=None,
                raw_narration='',
                description='',
                ref_no=norm_ref,
                payment_method=effective_config.default_payment_method,
                fingerprint=None,
                error_reason='Missing transaction narration',
                raw_preview=raw_preview,
            ))
            continue

        is_credit, amount_paise, amt_error = _parse_amount_columns(
            row,
            effective_config,
            debit_col_idx=debit_col_idx,
            credit_col_idx=credit_col_idx,
            amount_col_idx=amount_col_idx,
            type_col_idx=type_col_idx,
        )

        if amt_error or amount_paise is None:
            parsed_rows.append(ParsedStatementRow(
                line_number=line_number,
                row_status='invalid',
                txn_date=txn_date,
                amount_paise=None,
                running_balance_paise=running_bal,
                balance_mismatch_note=None,
                raw_narration=norm_narr,
                description=norm_narr[:255],
                ref_no=norm_ref,
                payment_method=effective_config.default_payment_method,
                fingerprint=None,
                error_reason=amt_error or 'Invalid transaction amount',
                raw_preview=raw_preview,
            ))
            continue

        payment_method = infer_payment_method(norm_narr, effective_config.default_payment_method)

        if is_credit:
            parsed_rows.append(ParsedStatementRow(
                line_number=line_number,
                row_status='skipped_credit',
                txn_date=txn_date,
                amount_paise=amount_paise,
                running_balance_paise=running_bal,
                balance_mismatch_note=None,
                raw_narration=norm_narr,
                description=norm_narr[:255],
                ref_no=norm_ref,
                payment_method=payment_method,
                fingerprint=None,
                error_reason=None,
                raw_preview=raw_preview,
            ))
            continue

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
            running_balance_paise=running_bal,
            balance_mismatch_note=None,
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

    reconciled_rows, balance_status, mismatch_count = _reconcile_running_balances(parsed_rows)

    return ParsedStatement(
        bank_profile=effective_config.key,
        detected_format_label=effective_config.label[:80],
        detected_encoding=encoding_used[:32],
        file_sha256=file_sha256,
        preamble_rows_skipped=header_idx,
        balance_check_status=balance_status,
        balance_mismatch_count=mismatch_count,
        rows=reconciled_rows,
    )


def parse_statement_csv(
    raw_bytes: bytes,
    config: BankFormatConfig | None,
    *,
    today: date | None = None,
) -> ParsedStatement:
    """Parse an uploaded CSV statement completely in memory using `config` (or auto-detect if None)."""
    raw_rows, encoding_used, source_kind, allow_iso_excel = _extract_csv_rows(raw_bytes)
    file_sha256 = hashlib.sha256(raw_bytes).hexdigest()
    return _parse_tabular_rows(
        raw_rows,
        file_sha256=file_sha256,
        encoding_used=encoding_used,
        source_kind=source_kind,
        allow_iso_excel_date=allow_iso_excel,
        config=config,
        today=today,
    )


def parse_statement_file(
    raw_bytes: bytes,
    *,
    filename: str,
    config: BankFormatConfig | None = None,
    file_password: str | None = None,
    today: date | None = None,
) -> ParsedStatement:
    """
    Parse any supported bank statement (.csv, .xlsx, .xls, digital .pdf) strictly in memory.

    Supports optional transient password decryption for encrypted Excel and PDF files without
    writing uploaded bytes or passwords to disk or logs.
    """
    clean_name = sanitize_filename(filename)
    ext = ('.' + clean_name.rsplit('.', 1)[-1].lower()) if '.' in clean_name else ''
    if ext not in ALLOWED_IMPORT_EXTENSIONS:
        raise StatementParseError(
            'Unsupported file format. Please upload a .csv, .xlsx, .xls, or digital .pdf statement.'
        )

    if ext == '.csv':
        raw_rows, encoding_used, source_kind, allow_iso_excel = _extract_csv_rows(raw_bytes)
    elif ext == '.xlsx':
        raw_rows, encoding_used, source_kind, allow_iso_excel = _extract_xlsx_rows(raw_bytes, file_password)
    elif ext == '.xls':
        raw_rows, encoding_used, source_kind, allow_iso_excel = _extract_xls_rows(raw_bytes, file_password)
    else:
        raw_rows, encoding_used, source_kind, allow_iso_excel = _extract_pdf_rows(raw_bytes, file_password)

    file_sha256 = hashlib.sha256(raw_bytes).hexdigest()
    return _parse_tabular_rows(
        raw_rows,
        file_sha256=file_sha256,
        encoding_used=encoding_used,
        source_kind=source_kind,
        allow_iso_excel_date=allow_iso_excel,
        config=config,
        today=today,
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
    Find the best unclaimed manually entered Expense likely matching a statement debit row.

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
        score = (1 if day_diff == 0 else 0, overlap, -exp.id)
        if best_score is None or score > best_score:
            best_score = score
            best_id = exp.id

    return best_id
