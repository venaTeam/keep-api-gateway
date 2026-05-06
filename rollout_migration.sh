#!/bin/bash
set -e # Stop on any error

echo "🚀 Starting Alert Schema Migration Rollout..."

# 1. Step 1: Add new columns (Instant)
echo "Step 1/3: Adding new columns..."
poetry run alembic upgrade e4ad6ddc7e90

# 2. Step 2: Backfill data (Batched, Safe)
echo "Step 2/3: Moving data from JSON to columns (this may take a few minutes)..."
poetry run alembic upgrade 5dfe012ad560

# 3. Step 3: Cleanup (Drop old event column)
echo "Step 3/3: Cleaning up old 'event' column..."
poetry run alembic upgrade 2b4dd4b88121

echo "✅ Migration completed successfully!"
