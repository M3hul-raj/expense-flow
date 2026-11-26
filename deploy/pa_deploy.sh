#!/usr/bin/env bash
set -euo pipefail

DRY_RUN=false
for arg in "$@"; do
  case "$arg" in
    --dry-run)
      DRY_RUN=true
      ;;
    *)
      echo "Unknown option: $arg" >&2
      echo "Usage: $0 [--dry-run]" >&2
      exit 2
      ;;
  esac
done

TIMESTAMP="$(date +%Y%m%d_%H%M%S)"
BACKUP_DIR="$HOME/backups"
BACKUP_FILE="$BACKUP_DIR/expenses_${TIMESTAMP}.db"

if [ "$DRY_RUN" = true ]; then
  cat <<EOF
[dry-run] set -euo pipefail
[dry-run] cd ~/ExpenseFlow
[dry-run] test "\$(git rev-parse --abbrev-ref HEAD)" = "main"
[dry-run] OLD_HASH="\$(git rev-parse HEAD)"
[dry-run] mkdir -p "$BACKUP_DIR"
[dry-run] sqlite3 instance/expenses.db ".backup '$BACKUP_FILE'"
[dry-run] sqlite3 "$BACKUP_FILE" "PRAGMA integrity_check;"
[dry-run] sqlite3 "$BACKUP_FILE" 'SELECT COUNT(*) AS users FROM "user"; SELECT COUNT(*) AS expenses FROM "expense";'
[dry-run] git pull --ff-only origin main
[dry-run] pip install -r requirements.txt -q
[dry-run] python -m pytest tests/ -q
[dry-run] echo "Rollback command: git reset --hard <old_hash>"
[dry-run] echo "NEXT: click Reload on the Web tab"
EOF
  exit 0
fi

cd ~/ExpenseFlow

CURRENT_BRANCH="$(git rev-parse --abbrev-ref HEAD)"
if [ "$CURRENT_BRANCH" != "main" ]; then
  echo "Error: expected branch 'main', found '$CURRENT_BRANCH'" >&2
  exit 1
fi

OLD_HASH="$(git rev-parse HEAD)"
echo "Current commit: $OLD_HASH"

mkdir -p "$BACKUP_DIR"
sqlite3 instance/expenses.db ".backup '$BACKUP_FILE'"
echo "Backup created: $BACKUP_FILE"

INTEGRITY="$(sqlite3 "$BACKUP_FILE" "PRAGMA integrity_check;")"
echo "Integrity check: $INTEGRITY"
if [ "$INTEGRITY" != "ok" ]; then
  echo "Error: backup integrity check failed" >&2
  exit 1
fi

echo "Backup row counts:"
sqlite3 "$BACKUP_FILE" 'SELECT COUNT(*) AS users FROM "user"; SELECT COUNT(*) AS expenses FROM "expense";'

git pull --ff-only origin main
pip install -r requirements.txt -q
python -m pytest tests/ -q

echo "Deploy succeeded."
echo "Rollback command (if needed): git reset --hard $OLD_HASH"
echo "NEXT: click Reload on the Web tab"
