"""
blueprints/imports.py — bank CSV statement upload, staging review, rules, confirm, and undo.

Routes:
  GET/POST /imports
  GET      /imports/<batch_id>
  POST     /imports/<batch_id>/apply-rules
  POST     /imports/<batch_id>/confirm
  POST     /imports/<batch_id>/undo
  POST     /imports/<batch_id>/discard
  POST     /imports/rules
  POST     /imports/rules/<rule_id>/delete
"""
from datetime import datetime, timedelta, timezone
import logging

from flask import (
    Blueprint, flash, make_response, redirect, render_template,
    request, session, url_for,
)
from sqlalchemy.exc import SQLAlchemyError

from extensions import limiter
from models import CategoryRule, Expense, ImportBatch, ImportRow, db
from statement_import import (
    BANK_FORMATS,
    MANUAL_DUP_DATE_TOLERANCE_DAYS,
    MAX_IMPORT_FILE_BYTES,
    MAX_IMPORT_ROWS,
    SUPPORTED_CUSTOM_DATE_FORMATS,
    StatementParseError,
    build_custom_bank_config,
    categorise_narration,
    clean_untrusted_text,
    match_manual_duplicate,
    parse_statement_csv,
    sanitize_filename,
    suggest_rule_pattern,
)
from utils import (
    EXPENSE_CATEGORIES,
    PAYMENT_METHODS,
    VALID_CATEGORIES,
    VALID_METHODS,
    commit_session,
    is_logged_in,
)

logger = logging.getLogger('expenseflow')

imports_bp = Blueprint('imports', __name__)


def _chunked(items: list[str], size: int = 500):
    for i in range(0, len(items), size):
        yield items[i:i + size]


def _existing_import_fingerprints(user_id: int, fingerprints: list[str]) -> set[str]:
    """Fetch existing Expense.import_fingerprint values for `user_id` safely in SQLite-safe chunks."""
    if not fingerprints:
        return set()
    found: set[str] = set()
    unique_fps = list(dict.fromkeys(fingerprints))
    for chunk in _chunked(unique_fps, 500):
        rows = (
            db.session.query(Expense.import_fingerprint)
            .filter(
                Expense.user_id == user_id,
                Expense.import_fingerprint.in_(chunk),
            )
            .all()
        )
        found.update(fp for (fp,) in rows if fp)
    return found


def _upsert_category_rule(user_id: int, raw_pattern: str, category: str) -> CategoryRule | None:
    """Create or update a user-scoped keyword categorisation rule; returns None if invalid."""
    pattern_clean = clean_untrusted_text(raw_pattern, 120)
    pattern_norm = pattern_clean.upper()
    if not pattern_norm or category not in VALID_CATEGORIES or category == 'Uncategorised':
        return None

    now = datetime.now(timezone.utc)
    existing = CategoryRule.query.filter_by(
        user_id=user_id,
        pattern_normalized=pattern_norm,
    ).first()
    if existing:
        existing.pattern = pattern_clean
        existing.category = category
        existing.updated_at = now
        return existing

    rule = CategoryRule(
        user_id=user_id,
        pattern=pattern_clean,
        pattern_normalized=pattern_norm,
        category=category,
        created_at=now,
        updated_at=now,
    )
    db.session.add(rule)
    return rule


def _render_upload_page(user_id: int, status_code: int = 200):
    batches = (
        ImportBatch.query
        .filter_by(user_id=user_id)
        .order_by(ImportBatch.created_at.desc(), ImportBatch.id.desc())
        .limit(20)
        .all()
    )
    rules = (
        CategoryRule.query
        .filter_by(user_id=user_id)
        .order_by(CategoryRule.updated_at.desc(), CategoryRule.id.desc())
        .all()
    )
    return make_response(
        render_template(
            'import_upload.html',
            bank_formats=list(BANK_FORMATS.values()),
            custom_date_formats=list(SUPPORTED_CUSTOM_DATE_FORMATS.keys()),
            categories=[c for c in EXPENSE_CATEGORIES if c != 'Uncategorised'],
            batches=batches,
            rules=rules,
            max_file_mb=MAX_IMPORT_FILE_BYTES // (1024 * 1024),
            max_rows=MAX_IMPORT_ROWS,
        ),
        status_code,
    )


def _upload_rate_limit_exceeded(_limit):
    """Render styled 429 response when CSV upload rate limit is breached."""
    flash('Too many statement uploads. Please wait 15 minutes before uploading again.', 'error')
    if not is_logged_in():
        return make_response(render_template('login.html'), 429)
    return _render_upload_page(session['user_id'], status_code=429)


def _resolve_bank_config(form):
    profile = form.get('bank_profile', 'indian_standard').strip()
    if profile == 'custom':
        preamble_raw = form.get('custom_preamble_rows', '0').strip() or '0'
        try:
            preamble_rows = int(preamble_raw)
        except ValueError as exc:
            raise StatementParseError('Preamble rows must be a non-negative integer.') from exc
        return build_custom_bank_config(
            date_column=form.get('custom_date_column', ''),
            date_format_key=form.get('custom_date_format', 'dd/mm/yyyy').strip(),
            narration_column=form.get('custom_narration_column', ''),
            amount_mode=form.get('custom_amount_mode', 'split_columns').strip(),
            debit_column=form.get('custom_debit_column', ''),
            credit_column=form.get('custom_credit_column', ''),
            amount_column=form.get('custom_amount_column', ''),
            signed_debit_negative=(form.get('custom_signed_debit_negative', '1') == '1'),
            ref_column=form.get('custom_ref_column', ''),
            preamble_rows=preamble_rows,
        )
    config = BANK_FORMATS.get(profile)
    if config is None:
        raise StatementParseError('Please select a valid bank statement format.')
    return config


@imports_bp.route('/imports', methods=['GET', 'POST'])
@limiter.limit(
    '10 per 15 minutes',
    methods=['POST'],
    error_message='Too many statement uploads.',
    on_breach=_upload_rate_limit_exceeded,
)
def index():
    """Upload bank statement CSV (parsed strictly in memory into staging) & view history/rules."""
    if not is_logged_in():
        flash('Please log in to import bank statements.', 'error')
        return redirect(url_for('auth.login'))

    user_id = session['user_id']

    if request.method == 'POST':
        try:
            config = _resolve_bank_config(request.form)
        except StatementParseError as exc:
            flash(str(exc), 'error')
            return redirect(url_for('imports.index'))

        uploaded = request.files.get('statement_file')
        if not uploaded or not uploaded.filename:
            flash('Please choose a CSV file to upload.', 'error')
            return redirect(url_for('imports.index'))

        clean_name = sanitize_filename(uploaded.filename)
        if not clean_name.lower().endswith('.csv'):
            flash('Please upload a .csv statement file.', 'error')
            return redirect(url_for('imports.index'))

        raw_bytes = uploaded.stream.read(MAX_IMPORT_FILE_BYTES + 1)
        try:
            parsed = parse_statement_csv(raw_bytes, config)
        except StatementParseError as exc:
            flash(str(exc), 'error')
            return redirect(url_for('imports.index'))

        debit_fps = [r.fingerprint for r in parsed.rows if r.row_status == 'ready' and r.fingerprint]
        existing_fps = _existing_import_fingerprints(user_id, debit_fps)

        debit_dates = [r.txn_date for r in parsed.rows if r.row_status == 'ready' and r.txn_date]
        manual_candidates = []
        if debit_dates:
            min_date = min(debit_dates) - timedelta(days=MANUAL_DUP_DATE_TOLERANCE_DAYS)
            max_date = max(debit_dates) + timedelta(days=MANUAL_DUP_DATE_TOLERANCE_DAYS)
            manual_candidates = (
                Expense.query
                .filter(
                    Expense.user_id == user_id,
                    Expense.import_batch_id.is_(None),
                    Expense.date >= min_date,
                    Expense.date <= max_date,
                )
                .order_by(Expense.id.asc())
                .all()
            )

        user_rules = CategoryRule.query.filter_by(user_id=user_id).all()
        claimed_manual_ids: set[int] = set()

        ready_count = 0
        dup_manual_count = 0
        already_imported_count = 0
        skipped_credit_count = 0
        invalid_count = 0

        batch = ImportBatch(
            user_id=user_id,
            bank_profile=parsed.bank_profile,
            original_filename=clean_name,
            file_sha256=parsed.file_sha256,
            detected_encoding=parsed.detected_encoding,
            status='staged',
            total_rows=len(parsed.rows),
        )
        db.session.add(batch)
        db.session.flush()

        for row in parsed.rows:
            row_status = row.row_status
            matched_expense_id = None
            include_on_confirm = False
            category = 'Uncategorised'

            if row_status == 'ready':
                category = categorise_narration(row.raw_narration, user_rules)
                if row.fingerprint in existing_fps:
                    row_status = 'already_imported'
                    already_imported_count += 1
                else:
                    matched_expense_id = match_manual_duplicate(
                        txn_date=row.txn_date,
                        amount_paise=row.amount_paise,
                        narration=row.raw_narration,
                        manual_candidates=manual_candidates,
                        claimed_ids=claimed_manual_ids,
                    )
                    if matched_expense_id is not None:
                        claimed_manual_ids.add(matched_expense_id)
                        row_status = 'possible_duplicate'
                        include_on_confirm = False
                        dup_manual_count += 1
                    else:
                        include_on_confirm = True
                        ready_count += 1
            elif row_status == 'skipped_credit':
                skipped_credit_count += 1
            else:
                invalid_count += 1

            db.session.add(ImportRow(
                batch_id=batch.id,
                user_id=user_id,
                line_number=row.line_number,
                row_status=row_status,
                txn_date=row.txn_date,
                amount_paise=row.amount_paise,
                raw_narration=row.raw_narration,
                description=row.description,
                ref_no=row.ref_no,
                category=category,
                payment_method=row.payment_method,
                fingerprint=row.fingerprint,
                error_reason=row.error_reason,
                raw_preview=row.raw_preview,
                matched_expense_id=matched_expense_id,
                include_on_confirm=include_on_confirm,
            ))

        batch.ready_count = ready_count
        batch.duplicate_manual_count = dup_manual_count
        batch.already_imported_count = already_imported_count
        batch.skipped_credit_count = skipped_credit_count
        batch.invalid_count = invalid_count

        if commit_session() is not None:
            logger.exception('Database error staging import batch: user=%s', user_id)
            flash('Unable to stage statement right now. Please try again.', 'error')
            return redirect(url_for('imports.index'))

        logger.info(
            'Statement staged: user=%s batch=%s profile=%s total=%d ready=%d dup_manual=%d already=%d credits=%d invalid=%d',
            user_id, batch.id, batch.bank_profile, batch.total_rows,
            ready_count, dup_manual_count, already_imported_count,
            skipped_credit_count, invalid_count,
        )
        return redirect(url_for('imports.review_batch', batch_id=batch.id))

    return _render_upload_page(user_id)


@imports_bp.route('/imports/<int:batch_id>')
def review_batch(batch_id):
    """Render the staging review / confirmed summary / undone summary screen for one batch."""
    if not is_logged_in():
        flash('Please log in to review statement imports.', 'error')
        return redirect(url_for('auth.login'))

    user_id = session['user_id']
    batch = ImportBatch.query.filter_by(id=batch_id, user_id=user_id).first_or_404()
    rows = (
        ImportRow.query
        .filter_by(batch_id=batch.id, user_id=user_id)
        .order_by(ImportRow.line_number.asc())
        .all()
    )

    review_rows = []
    already_imported_rows = []
    skipped_credit_rows = []
    invalid_rows = []
    suggested_patterns: dict[int, str] = {}

    for row in rows:
        if row.row_status in {'ready', 'possible_duplicate', 'imported', 'skipped_user', 'undone'}:
            review_rows.append(row)
            suggested_patterns[row.id] = suggest_rule_pattern(row.raw_narration)
        elif row.row_status == 'already_imported':
            already_imported_rows.append(row)
        elif row.row_status == 'skipped_credit':
            skipped_credit_rows.append(row)
        elif row.row_status == 'invalid':
            invalid_rows.append(row)

    selected_total_paise = sum(
        row.amount_paise or 0
        for row in review_rows
        if row.amount_paise and (row.include_on_confirm or row.row_status == 'imported')
    )
    credit_total_paise = sum(row.amount_paise or 0 for row in skipped_credit_rows)

    profile_cfg = BANK_FORMATS.get(batch.bank_profile)
    profile_label = profile_cfg.label if profile_cfg else 'Custom CSV Mapping'

    return render_template(
        'import_review.html',
        batch=batch,
        profile_label=profile_label,
        review_rows=review_rows,
        already_imported_rows=already_imported_rows,
        skipped_credit_rows=skipped_credit_rows,
        invalid_rows=invalid_rows,
        suggested_patterns=suggested_patterns,
        selected_total_paise=selected_total_paise,
        credit_total_paise=credit_total_paise,
        categories=EXPENSE_CATEGORIES,
        payment_methods=PAYMENT_METHODS,
    )


def _apply_staged_edits_and_collect_rules(user_id: int, rows: list[ImportRow], form):
    """Apply per-row form edits onto staged ImportRow objects and save checked rules."""
    newly_saved_rules: list[CategoryRule] = []
    submitted = form.get('form_submitted') == '1'

    for row in rows:
        if row.row_status not in {'ready', 'possible_duplicate'}:
            continue
        if submitted:
            row.include_on_confirm = (form.get(f'include_{row.id}') == '1')
            cat = form.get(f'category_{row.id}', row.category).strip()
            if cat in VALID_CATEGORIES:
                row.category = cat
            pm = form.get(f'payment_method_{row.id}', row.payment_method).strip()
            if pm in VALID_METHODS:
                row.payment_method = pm
            if f'description_{row.id}' in form:
                desc = clean_untrusted_text(form.get(f'description_{row.id}', ''), 255)
                if desc:
                    row.description = desc

            if form.get(f'always_apply_{row.id}') == '1':
                pat = clean_untrusted_text(form.get(f'rule_pattern_{row.id}', ''), 120) or suggest_rule_pattern(row.raw_narration)
                saved = _upsert_category_rule(user_id, pat, row.category)
                if saved is not None:
                    newly_saved_rules.append(saved)

    return newly_saved_rules


@imports_bp.route('/imports/<int:batch_id>/apply-rules', methods=['POST'])
def apply_rules(batch_id):
    """Save checked 'Always apply' rules and re-categorise Uncategorised staged rows."""
    if not is_logged_in():
        flash('Please log in to manage import rules.', 'error')
        return redirect(url_for('auth.login'))

    user_id = session['user_id']
    batch = ImportBatch.query.filter_by(id=batch_id, user_id=user_id).first_or_404()
    if batch.status != 'staged':
        flash('Rules can only be applied while a batch is staged for review.', 'warning')
        return redirect(url_for('imports.review_batch', batch_id=batch.id))

    rows = (
        ImportRow.query
        .filter_by(batch_id=batch.id, user_id=user_id)
        .order_by(ImportRow.line_number.asc())
        .all()
    )
    saved_rules = _apply_staged_edits_and_collect_rules(user_id, rows, request.form)
    db.session.flush()

    all_rules = CategoryRule.query.filter_by(user_id=user_id).all()
    updated_count = 0
    for row in rows:
        if row.row_status in {'ready', 'possible_duplicate'} and row.category == 'Uncategorised':
            matched_cat = categorise_narration(row.raw_narration, all_rules)
            if matched_cat != 'Uncategorised':
                row.category = matched_cat
                updated_count += 1

    if commit_session() is not None:
        logger.exception('Database error applying rules: user=%s batch=%s', user_id, batch.id)
        flash('Unable to save rules right now. Please try again.', 'error')
        return redirect(url_for('imports.review_batch', batch_id=batch.id))

    if saved_rules:
        flash(
            f'Saved {len(saved_rules)} rule(s) and updated {updated_count} row(s).',
            'success',
        )
    else:
        flash(f'Re-applied saved rules ({updated_count} row(s) updated).', 'info')
    return redirect(url_for('imports.review_batch', batch_id=batch.id))


@imports_bp.route('/imports/<int:batch_id>/confirm', methods=['POST'])
def confirm_batch(batch_id):
    """Atomically write selected staged debit rows to Expense in one DB transaction."""
    if not is_logged_in():
        flash('Please log in to confirm statement imports.', 'error')
        return redirect(url_for('auth.login'))

    user_id = session['user_id']
    batch = ImportBatch.query.filter_by(id=batch_id, user_id=user_id).first_or_404()

    if batch.status == 'confirmed':
        flash('This import batch has already been confirmed.', 'info')
        return redirect(url_for('imports.review_batch', batch_id=batch.id))
    if batch.status == 'undone':
        flash('This import batch was already undone and cannot be re-confirmed.', 'warning')
        return redirect(url_for('imports.review_batch', batch_id=batch.id))

    now = datetime.now(timezone.utc)
    today = datetime.now().date()

    try:
        # Conditional atomic transition prevents concurrent double-confirm
        transitioned = (
            db.session.query(ImportBatch)
            .filter(
                ImportBatch.id == batch.id,
                ImportBatch.user_id == user_id,
                ImportBatch.status == 'staged',
            )
            .update({'status': 'confirmed', 'confirmed_at': now}, synchronize_session=False)
        )
        if transitioned != 1:
            db.session.rollback()
            flash('This import batch has already been processed.', 'info')
            return redirect(url_for('imports.review_batch', batch_id=batch.id))
        db.session.expire(batch)

        rows = (
            ImportRow.query
            .filter_by(batch_id=batch.id, user_id=user_id)
            .order_by(ImportRow.line_number.asc())
            .all()
        )
        newly_saved_rules = _apply_staged_edits_and_collect_rules(user_id, rows, request.form)
        if newly_saved_rules:
            db.session.flush()
            for row in rows:
                if row.row_status in {'ready', 'possible_duplicate'} and row.category == 'Uncategorised':
                    row.category = categorise_narration(row.raw_narration, newly_saved_rules)

        candidate_fps = [
            row.fingerprint
            for row in rows
            if row.row_status in {'ready', 'possible_duplicate'} and row.fingerprint
        ]
        existing_fps = _existing_import_fingerprints(user_id, candidate_fps)

        imported_count = 0
        skipped_count = 0

        for row in rows:
            if row.row_status not in {'ready', 'possible_duplicate'}:
                skipped_count += 1
                continue

            if not row.include_on_confirm:
                row.row_status = 'skipped_user'
                skipped_count += 1
                continue

            if not row.fingerprint or row.fingerprint in existing_fps:
                row.row_status = 'already_imported'
                row.include_on_confirm = False
                skipped_count += 1
                continue

            if row.txn_date is None or row.txn_date > today or not row.amount_paise or row.amount_paise <= 0:
                row.row_status = 'invalid'
                row.error_reason = row.error_reason or 'Row failed final validation before confirm'
                row.include_on_confirm = False
                skipped_count += 1
                continue

            final_category = row.category if row.category in VALID_CATEGORIES else 'Uncategorised'
            final_method = row.payment_method if row.payment_method in VALID_METHODS else 'Net Banking'
            final_desc = (row.description or row.raw_narration or 'Bank Import')[:255]

            exp = Expense(
                user_id=user_id,
                date=row.txn_date,
                category=final_category,
                amount_paise=row.amount_paise,
                description=final_desc,
                payment_method=final_method,
                import_batch_id=batch.id,
                import_fingerprint=row.fingerprint,
            )
            db.session.add(exp)
            db.session.flush()

            row.created_expense_id = exp.id
            row.row_status = 'imported'
            existing_fps.add(row.fingerprint)
            imported_count += 1

        batch.imported_count = imported_count
        batch.skipped_count = skipped_count
        db.session.commit()
    except SQLAlchemyError:
        db.session.rollback()
        logger.exception('Database error confirming batch: user=%s batch=%s', user_id, batch.id)
        flash('Unable to confirm statement import; all changes were rolled back.', 'error')
        return redirect(url_for('imports.review_batch', batch_id=batch.id))

    logger.info(
        'Statement confirmed: user=%s batch=%s imported=%d skipped=%d',
        user_id, batch.id, imported_count, skipped_count,
    )
    if imported_count == 0:
        flash('Import confirmed with 0 new expenses added (duplicates/credits/invalid rows skipped).', 'info')
    else:
        flash(f'Successfully imported {imported_count} expense(s)!', 'success')
    return redirect(url_for('imports.review_batch', batch_id=batch.id))


@imports_bp.route('/imports/<int:batch_id>/undo', methods=['POST'])
def undo_batch(batch_id):
    """Atomically delete every Expense created by a confirmed batch in one DB transaction."""
    if not is_logged_in():
        flash('Please log in to undo statement imports.', 'error')
        return redirect(url_for('auth.login'))

    user_id = session['user_id']
    batch = ImportBatch.query.filter_by(id=batch_id, user_id=user_id).first_or_404()

    if batch.status != 'confirmed':
        flash('Only confirmed import batches can be undone.', 'warning')
        return redirect(url_for('imports.review_batch', batch_id=batch.id))

    now = datetime.now(timezone.utc)
    try:
        transitioned = (
            db.session.query(ImportBatch)
            .filter(
                ImportBatch.id == batch.id,
                ImportBatch.user_id == user_id,
                ImportBatch.status == 'confirmed',
            )
            .update({'status': 'undone', 'undone_at': now}, synchronize_session=False)
        )
        if transitioned != 1:
            db.session.rollback()
            flash('This import batch was already undone.', 'info')
            return redirect(url_for('imports.review_batch', batch_id=batch.id))
        db.session.expire(batch)

        ImportRow.query.filter(
            ImportRow.batch_id == batch.id,
            ImportRow.user_id == user_id,
            ImportRow.row_status == 'imported',
        ).update(
            {'row_status': 'undone', 'created_expense_id': None},
            synchronize_session=False,
        )

        deleted_count = (
            Expense.query
            .filter(Expense.user_id == user_id, Expense.import_batch_id == batch.id)
            .delete(synchronize_session=False)
        )
        db.session.commit()
    except SQLAlchemyError:
        db.session.rollback()
        logger.exception('Database error undoing batch: user=%s batch=%s', user_id, batch.id)
        flash('Unable to undo import batch; no expenses were removed.', 'error')
        return redirect(url_for('imports.review_batch', batch_id=batch.id))

    logger.info('Statement undone: user=%s batch=%s removed=%d', user_id, batch.id, deleted_count)
    flash(f'Import undone — removed {deleted_count} expense(s) from this batch.', 'success')
    return redirect(url_for('imports.review_batch', batch_id=batch.id))


@imports_bp.route('/imports/<int:batch_id>/discard', methods=['POST'])
def discard_batch(batch_id):
    """Delete a staged (unconfirmed) batch cleanly without affecting Expense rows."""
    if not is_logged_in():
        flash('Please log in to manage imports.', 'error')
        return redirect(url_for('auth.login'))

    user_id = session['user_id']
    batch = ImportBatch.query.filter_by(id=batch_id, user_id=user_id).first_or_404()
    if batch.status != 'staged':
        flash('Only staged batches can be discarded.', 'warning')
        return redirect(url_for('imports.review_batch', batch_id=batch.id))

    db.session.delete(batch)
    if commit_session() is not None:
        logger.exception('Database error discarding batch: user=%s batch=%s', user_id, batch_id)
        flash('Unable to discard batch right now.', 'error')
        return redirect(url_for('imports.review_batch', batch_id=batch_id))

    flash('Staged import batch discarded.', 'info')
    return redirect(url_for('imports.index'))


@imports_bp.route('/imports/rules', methods=['POST'])
def create_rule():
    """Save or update a deterministic keyword -> category rule for the logged-in user."""
    if not is_logged_in():
        flash('Please log in to manage rules.', 'error')
        return redirect(url_for('auth.login'))

    user_id = session['user_id']
    pattern = request.form.get('pattern', '')
    category = request.form.get('category', '').strip()

    rule = _upsert_category_rule(user_id, pattern, category)
    if rule is None:
        flash('Please provide a keyword and a specific category.', 'error')
        return redirect(url_for('imports.index'))

    if commit_session() is not None:
        logger.exception('Database error saving category rule: user=%s', user_id)
        flash('Unable to save categorisation rule right now.', 'error')
        return redirect(url_for('imports.index'))

    flash('Categorisation rule saved.', 'success')
    return redirect(url_for('imports.index'))


@imports_bp.route('/imports/rules/<int:rule_id>/delete', methods=['POST'])
def delete_rule(rule_id):
    """Delete a user-owned categorisation rule (returns 404 for other users)."""
    if not is_logged_in():
        flash('Please log in to manage rules.', 'error')
        return redirect(url_for('auth.login'))

    user_id = session['user_id']
    rule = CategoryRule.query.filter_by(id=rule_id, user_id=user_id).first_or_404()
    db.session.delete(rule)
    if commit_session() is not None:
        logger.exception('Database error deleting category rule: user=%s rule=%s', user_id, rule_id)
        flash('Unable to delete rule right now.', 'error')
        return redirect(url_for('imports.index'))

    flash('Categorisation rule deleted.', 'info')
    return redirect(url_for('imports.index'))
