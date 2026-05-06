#!/bin/bash
set -e # Stop on any error

echo "↩️ Reversing Alert Schema Migration..."

# 1. Restore the 'event' column and re-populate it from the flat columns
echo "Step 1/2: Restoring event column and re-building JSON payload..."
poetry run alembic downgrade 5dfe012ad560

# 2. Remove the new flat columns
echo "Step 2/2: Dropping flat columns..."
poetry run alembic downgrade 9dd1be4539e0

echo "✅ Rollback completed. Database is back to JSON-only structure."
