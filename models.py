from sqlalchemy import MetaData
from flask_sqlalchemy import SQLAlchemy
from werkzeug.security import generate_password_hash, check_password_hash
from datetime import datetime, timezone

NAMING_CONVENTION = {
    'ix': 'ix_%(column_0_label)s',
    'uq': 'uq_%(table_name)s_%(column_0_name)s',
    'ck': 'ck_%(table_name)s_%(constraint_name)s',
    'fk': 'fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s',
    'pk': 'pk_%(table_name)s',
}

db = SQLAlchemy(metadata=MetaData(naming_convention=NAMING_CONVENTION))


class User(db.Model):
    """Stores registered user accounts."""
    id            = db.Column(db.Integer, primary_key=True)
    username      = db.Column(db.String(80), nullable=False)
    email         = db.Column(db.String(120), nullable=False)
    password_hash = db.Column(db.String(256), nullable=False)
    recurring_budget_paise = db.Column(db.BigInteger, nullable=True)
    registration_date = db.Column(db.DateTime, nullable=False, default=lambda: datetime.now(timezone.utc))
    avatar        = db.Column(db.String(50), nullable=False, default='avatar_1', server_default=db.text("'avatar_1'"))
    display_name  = db.Column(db.String(80))

    __table_args__ = (
        db.CheckConstraint('recurring_budget_paise > 0', name='recurring_budget_paise_positive'),
        db.Index('ix_user_username_lower', db.func.lower(username), unique=True),
        db.Index('ix_user_email_lower', db.func.lower(email), unique=True),
    )

    expenses       = db.relationship('Expense', backref='user', lazy=True, cascade="all, delete-orphan")
    budgets        = db.relationship('Budget',  backref='user', lazy=True, cascade="all, delete-orphan")
    import_batches = db.relationship('ImportBatch', backref='user', lazy=True, cascade="all, delete-orphan")
    category_rules = db.relationship('CategoryRule', backref='user', lazy=True, cascade="all, delete-orphan")

    def set_password(self, password):
        """Hash and store the user's password."""
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        """Verify a plain-text password against the stored hash."""
        return check_password_hash(self.password_hash, password)


class Expense(db.Model):
    """Stores individual expense records for a user."""
    id                 = db.Column(db.Integer, primary_key=True)
    # Indexed — most queries filter by user_id and/or date
    user_id            = db.Column(db.Integer, db.ForeignKey('user.id', ondelete='CASCADE'), nullable=False, index=True)
    date               = db.Column(db.Date, nullable=False, index=True)
    category           = db.Column(db.String(50), nullable=False)
    amount_paise       = db.Column(db.BigInteger, nullable=False)
    description        = db.Column(db.String(255))
    payment_method     = db.Column(db.String(50), nullable=False, default='Cash')
    created_at         = db.Column(db.DateTime, nullable=False, default=lambda: datetime.now(timezone.utc))
    import_batch_id    = db.Column(db.Integer, db.ForeignKey('import_batch.id', ondelete='SET NULL'), nullable=True, index=True)
    import_fingerprint = db.Column(db.String(64), nullable=True)

    # Composite index covering WHERE user_id = ? ORDER BY date DESC, id DESC via backward scan
    __table_args__ = (
        db.CheckConstraint('amount_paise > 0', name='amount_paise_positive'),
        db.Index('idx_expense_user_date_id', 'user_id', 'date', 'id'),
        db.UniqueConstraint('user_id', 'import_fingerprint', name='uq_expense_user_import_fingerprint'),
    )


class Budget(db.Model):
    """Stores monthly budget targets per user."""
    id           = db.Column(db.Integer, primary_key=True)
    user_id      = db.Column(db.Integer, db.ForeignKey('user.id', ondelete='CASCADE'), nullable=False, index=True)
    year_month   = db.Column(db.String(7), nullable=False)  # YYYY-MM
    amount_paise = db.Column(db.BigInteger, nullable=False)

    __table_args__ = (
        db.UniqueConstraint('user_id', 'year_month', name='unique_user_month_budget'),
        db.CheckConstraint('amount_paise > 0', name='amount_paise_positive'),
    )


class ImportBatch(db.Model):
    """Tracks a CSV statement upload batch through staged review, confirm, and undo."""
    __tablename__ = 'import_batch'

    id                     = db.Column(db.Integer, primary_key=True)
    user_id                = db.Column(db.Integer, db.ForeignKey('user.id', ondelete='CASCADE'), nullable=False, index=True)
    bank_profile           = db.Column(db.String(64), nullable=False)
    original_filename      = db.Column(db.String(255), nullable=False)
    file_sha256            = db.Column(db.String(64), nullable=False)
    detected_encoding      = db.Column(db.String(32), nullable=False)
    status                 = db.Column(db.String(20), nullable=False, default='staged')
    total_rows             = db.Column(db.Integer, nullable=False, default=0)
    ready_count            = db.Column(db.Integer, nullable=False, default=0)
    duplicate_manual_count = db.Column(db.Integer, nullable=False, default=0)
    already_imported_count = db.Column(db.Integer, nullable=False, default=0)
    skipped_credit_count   = db.Column(db.Integer, nullable=False, default=0)
    invalid_count          = db.Column(db.Integer, nullable=False, default=0)
    imported_count         = db.Column(db.Integer, nullable=False, default=0)
    skipped_count          = db.Column(db.Integer, nullable=False, default=0)
    created_at             = db.Column(db.DateTime, nullable=False, default=lambda: datetime.now(timezone.utc))
    confirmed_at           = db.Column(db.DateTime, nullable=True)
    undone_at              = db.Column(db.DateTime, nullable=True)

    rows     = db.relationship('ImportRow', backref='batch', lazy=True, cascade="all, delete-orphan")
    expenses = db.relationship('Expense', backref='import_batch', lazy=True, passive_deletes=True)

    __table_args__ = (
        db.CheckConstraint("status IN ('staged', 'confirmed', 'undone')", name='status_valid'),
        db.Index('idx_import_batch_user_created', 'user_id', 'created_at', 'id'),
    )


class ImportRow(db.Model):
    """Stores parsed statement rows (debits, skipped credits, invalid rows) in staging."""
    __tablename__ = 'import_row'

    id                 = db.Column(db.Integer, primary_key=True)
    batch_id           = db.Column(db.Integer, db.ForeignKey('import_batch.id', ondelete='CASCADE'), nullable=False, index=True)
    user_id            = db.Column(db.Integer, db.ForeignKey('user.id', ondelete='CASCADE'), nullable=False, index=True)
    line_number        = db.Column(db.Integer, nullable=False)
    row_status         = db.Column(db.String(32), nullable=False)
    txn_date           = db.Column(db.Date, nullable=True)
    amount_paise       = db.Column(db.BigInteger, nullable=True)
    raw_narration      = db.Column(db.String(500), nullable=False, default='')
    description        = db.Column(db.String(255), nullable=False, default='')
    ref_no             = db.Column(db.String(100), nullable=False, default='')
    category           = db.Column(db.String(50), nullable=False, default='Uncategorised')
    payment_method     = db.Column(db.String(50), nullable=False, default='Net Banking')
    fingerprint        = db.Column(db.String(64), nullable=True)
    error_reason       = db.Column(db.String(255), nullable=True)
    raw_preview        = db.Column(db.String(500), nullable=False, default='')
    matched_expense_id = db.Column(db.Integer, db.ForeignKey('expense.id', ondelete='SET NULL'), nullable=True)
    created_expense_id = db.Column(db.Integer, db.ForeignKey('expense.id', ondelete='SET NULL'), nullable=True)
    include_on_confirm = db.Column(db.Boolean, nullable=False, default=True)

    matched_expense = db.relationship('Expense', foreign_keys=[matched_expense_id], passive_deletes=True)
    created_expense = db.relationship('Expense', foreign_keys=[created_expense_id], passive_deletes=True)

    __table_args__ = (
        db.CheckConstraint('amount_paise IS NULL OR amount_paise > 0', name='amount_paise_positive'),
        db.CheckConstraint(
            "row_status IN ('ready', 'possible_duplicate', 'already_imported', 'skipped_credit', 'invalid', 'imported', 'skipped_user', 'undone')",
            name='row_status_valid',
        ),
        db.Index('idx_import_row_batch_line', 'batch_id', 'line_number'),
    )


class CategoryRule(db.Model):
    """Per-user deterministic keyword-to-category rule saved from review or rules manager."""
    __tablename__ = 'category_rule'

    id                 = db.Column(db.Integer, primary_key=True)
    user_id            = db.Column(db.Integer, db.ForeignKey('user.id', ondelete='CASCADE'), nullable=False, index=True)
    pattern            = db.Column(db.String(120), nullable=False)
    pattern_normalized = db.Column(db.String(120), nullable=False)
    category           = db.Column(db.String(50), nullable=False)
    created_at         = db.Column(db.DateTime, nullable=False, default=lambda: datetime.now(timezone.utc))
    updated_at         = db.Column(db.DateTime, nullable=False, default=lambda: datetime.now(timezone.utc))

    __table_args__ = (
        db.UniqueConstraint('user_id', 'pattern_normalized', name='uq_category_rule_user_pattern'),
        db.CheckConstraint("pattern_normalized <> ''", name='pattern_nonempty'),
    )
