#!/bin/bash
# Seed the RDS PostgreSQL instance with schema and data.
# Usage: bash scripts/seed-rds.sh
#
# Requires: psql, AWS CLI (to retrieve RDS endpoint and password)
set -euo pipefail

INFRA_DIR="$(cd "$(dirname "$0")/../infra" && pwd)"

echo "=== Seed RDS PostgreSQL ==="

# Get RDS connection info from Terraform outputs
cd "$INFRA_DIR"
RDS_ENDPOINT=$(terraform output -raw rds_endpoint)
RDS_HOST=$(echo "$RDS_ENDPOINT" | cut -d: -f1)
RDS_PORT=$(echo "$RDS_ENDPOINT" | cut -d: -f2)

# Get password from Secrets Manager
DB_PASSWORD_ARN=$(terraform output -raw rds_password_secret_arn)
DB_PASSWORD=$(aws secretsmanager get-secret-value --secret-id "$DB_PASSWORD_ARN" --query SecretString --output text)

DB_NAME="mas_compliance"
DB_USER="msrag"

export PGPASSWORD="$DB_PASSWORD"

echo "Connecting to $RDS_HOST:$RDS_PORT..."

# Run schema
SQL_DIR="$(cd "$(dirname "$0")/../corpus/data/sql" && pwd)"

echo "Applying schema..."
psql -h "$RDS_HOST" -p "$RDS_PORT" -U "$DB_USER" -d "$DB_NAME" -f "$SQL_DIR/init_schema.sql"

# Run seed files
for seed_file in "$SQL_DIR"/seed/*.sql; do
  echo "Seeding: $(basename "$seed_file")"
  psql -h "$RDS_HOST" -p "$RDS_PORT" -U "$DB_USER" -d "$DB_NAME" -f "$seed_file"
done

echo ""
echo "=== RDS seeding complete ==="
echo "Verify: psql -h $RDS_HOST -p $RDS_PORT -U $DB_USER -d $DB_NAME -c 'SELECT count(*) FROM enforcement_actions;'"
