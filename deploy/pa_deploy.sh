#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat >&2 <<EOF
Usage: $0 (--stamp REVISION | --upgrade-to REVISION) [--skip-tests] [--dry-run]
EOF
}

DRY_RUN=false
SKIP_TESTS=false
MODE=""
TARGET_REVISION=""

while [ "$#" -gt 0 ]; do
  case "$1" in
    --stamp)
      if [ -n "$MODE" ]; then
        echo "Error: specify only one of --stamp or --upgrade-to" >&2
        usage
        exit 2
      fi
      if [ "$#" -lt 2 ] || [[ "$2" == -* ]] || [ -z "$2" ]; then
        echo "Error: --stamp requires a target REVISION" >&2
        usage
        exit 2
      fi
      MODE="stamp"
      TARGET_REVISION="$2"
      shift 2
      ;;
    --upgrade-to)
      if [ -n "$MODE" ]; then
        echo "Error: specify only one of --stamp or --upgrade-to" >&2
        usage
        exit 2
      fi
      if [ "$#" -lt 2 ] || [[ "$2" == -* ]] || [ -z "$2" ]; then
        echo "Error: --upgrade-to requires a target REVISION" >&2
        usage
        exit 2
      fi
      MODE="upgrade-to"
      TARGET_REVISION="$2"
      shift 2
      ;;
    --dry-run)
      DRY_RUN=true
      shift
      ;;
    --skip-tests)
      SKIP_TESTS=true
      shift
      ;;
    *)
      echo "Unknown option or argument: $1" >&2
      usage
      exit 2
      ;;
  esac
done

if [ -z "$MODE" ]; then
  echo "Error: missing deploy migration mode (--stamp REVISION or --upgrade-to REVISION)" >&2
  usage
  exit 2
fi

case "$TARGET_REVISION" in
  head|heads|base|current)
    echo "Error: explicit revision ID required (got '$TARGET_REVISION')" >&2
    exit 2
    ;;
esac

if ! [[ "$TARGET_REVISION" =~ ^[0-9A-Za-z_.-]+$ ]]; then
  echo "Error: invalid revision format '$TARGET_REVISION'" >&2
  exit 2
fi

if [ "$MODE" = "stamp" ]; then
  ALEMBIC_CMD="alembic stamp \"$TARGET_REVISION\""
else
  ALEMBIC_CMD="alembic upgrade \"$TARGET_REVISION\""
fi

TIMESTAMP="$(date +%Y%m%d_%H%M%S)"
BACKUP_DIR="$HOME/backups"
BACKUP_FILE="$BACKUP_DIR/expenses_${TIMESTAMP}.db"

if [ "$SKIP_TESTS" = true ]; then
  TEST_STEP_MSG="[dry-run] skipping pytest (--skip-tests passed)"
else
  TEST_STEP_MSG="[dry-run] python -m pytest tests/ -q"
fi

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
[dry-run] pip install -r requirements.lock -q
[dry-run] export EXPENSEFLOW_DATABASE_URI="\${EXPENSEFLOW_DATABASE_URI:-sqlite:///\$PWD/instance/expenses.db}"
[dry-run] $ALEMBIC_CMD
$TEST_STEP_MSG
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
pip install -r requirements.lock -q
export EXPENSEFLOW_DATABASE_URI="${EXPENSEFLOW_DATABASE_URI:-sqlite:///$PWD/instance/expenses.db}"
if [ "$MODE" = "stamp" ]; then
  alembic stamp "$TARGET_REVISION"
else
  alembic upgrade "$TARGET_REVISION"
fi
if [ "$SKIP_TESTS" = true ]; then
  echo "Skipping pytest suite (--skip-tests passed; ensure CI checks already passed)."
else
  python -m pytest tests/ -q
fi

echo "Deploy succeeded."
echo "Rollback command (if needed): git reset --hard $OLD_HASH"
echo "NEXT: click Reload on the Web tab"
