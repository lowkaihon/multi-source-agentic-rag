#!/bin/bash
set -e

# Wait for OpenSearch if WAIT_FOR_OPENSEARCH is set
if [ -n "$WAIT_FOR_OPENSEARCH" ]; then
  echo "Waiting for OpenSearch at ${OPENSEARCH_HOST:-localhost}:${OPENSEARCH_PORT:-9200}..."
  until curl -sf "http://${OPENSEARCH_HOST:-localhost}:${OPENSEARCH_PORT:-9200}/_cluster/health" > /dev/null 2>&1; do
    sleep 2
  done
  echo "OpenSearch is ready"
fi

# Wait for PostgreSQL if WAIT_FOR_POSTGRES is set
if [ -n "$WAIT_FOR_POSTGRES" ]; then
  echo "Waiting for PostgreSQL at ${PG_HOST:-localhost}:${PG_PORT:-5432}..."
  until python -c "import psycopg2; psycopg2.connect(host='${PG_HOST:-localhost}', port=${PG_PORT:-5432}, dbname='${PG_DBNAME:-mas_compliance}', user='${PG_USER:-msrag}', password='${PG_PASSWORD:-msrag_dev}')" 2>/dev/null; do
    sleep 2
  done
  echo "PostgreSQL is ready"
fi

exec uvicorn msrag.server:app --host 0.0.0.0 --port 8000
