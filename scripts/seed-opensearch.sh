#!/bin/bash
# Seed OpenSearch in the ECS task with index + documents.
# Usage: bash scripts/seed-opensearch.sh
#
# This script port-forwards to the ECS task's OpenSearch sidecar,
# then runs the setup_opensearch.py script against it.
set -euo pipefail

INFRA_DIR="$(cd "$(dirname "$0")/../infra" && pwd)"
REGION="ap-southeast-1"

echo "=== Seed OpenSearch (ECS Sidecar) ==="

# Get ECS task info
cd "$INFRA_DIR"
CLUSTER=$(terraform output -raw ecs_cluster_name)
SERVICE=$(terraform output -raw ecs_service_name)

echo "Finding running ECS task..."
TASK_ARN=$(aws ecs list-tasks \
  --cluster "$CLUSTER" \
  --service-name "$SERVICE" \
  --desired-status RUNNING \
  --query 'taskArns[0]' \
  --output text \
  --region "$REGION")

if [ "$TASK_ARN" = "None" ] || [ -z "$TASK_ARN" ]; then
  echo "ERROR: No running tasks found for service $SERVICE"
  exit 1
fi

TASK_ID=$(echo "$TASK_ARN" | awk -F/ '{print $NF}')
echo "Task: $TASK_ID"

# Port-forward to OpenSearch container (requires ECS Exec enabled)
echo "Starting port-forward to OpenSearch (port 9200)..."
echo "NOTE: This requires ECS Exec to be enabled on the service."
echo ""

# Start port-forward in background
aws ssm start-session \
  --target "ecs:${CLUSTER}_${TASK_ID}_opensearch" \
  --document-name AWS-StartPortForwardingSession \
  --parameters '{"portNumber":["9200"],"localPortNumber":["19200"]}' \
  --region "$REGION" &
PF_PID=$!

# Wait for port-forward to establish
sleep 5

# Verify connectivity
if ! curl -sf "http://localhost:19200/_cluster/health" > /dev/null 2>&1; then
  echo "ERROR: Cannot reach OpenSearch via port-forward"
  echo ""
  echo "Alternative: Run setup_opensearch.py from within the ECS task:"
  echo "  aws ecs execute-command --cluster $CLUSTER --task $TASK_ID --container api --interactive --command 'python scripts/setup_opensearch.py'"
  kill $PF_PID 2>/dev/null || true
  exit 1
fi

echo "OpenSearch reachable at localhost:19200"

# Run setup script against forwarded port
OPENSEARCH_HOST=localhost OPENSEARCH_PORT=19200 uv run python scripts/setup_opensearch.py

# Cleanup
kill $PF_PID 2>/dev/null || true

echo ""
echo "=== OpenSearch seeding complete ==="
