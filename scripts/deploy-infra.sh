#!/bin/bash
# One-command AWS infrastructure provisioning.
# Usage: bash scripts/deploy-infra.sh
set -euo pipefail

INFRA_DIR="$(cd "$(dirname "$0")/../infra" && pwd)"

echo "=== MSRAG AWS Infrastructure Deployment ==="
echo ""

# Check prerequisites
for cmd in aws terraform docker; do
  if ! command -v $cmd &>/dev/null; then
    echo "ERROR: $cmd is not installed"
    exit 1
  fi
done

# Check AWS auth
echo "Checking AWS credentials..."
AWS_ACCOUNT=$(aws sts get-caller-identity --query Account --output text 2>/dev/null) || {
  echo "ERROR: AWS credentials not configured. Run 'aws configure' or set AWS_PROFILE."
  exit 1
}
echo "AWS Account: $AWS_ACCOUNT"
echo ""

# Check required env vars for secrets
if [ -z "${TF_VAR_openai_api_key:-}" ]; then
  echo "ERROR: TF_VAR_openai_api_key environment variable is required"
  echo "  export TF_VAR_openai_api_key='sk-...'"
  exit 1
fi

# Terraform init + apply
cd "$INFRA_DIR"
echo "Running terraform init..."
terraform init

echo ""
echo "Running terraform plan..."
terraform plan -out=tfplan

echo ""
read -p "Apply this plan? (y/N) " confirm
if [ "$confirm" != "y" ] && [ "$confirm" != "Y" ]; then
  echo "Aborted."
  exit 0
fi

terraform apply tfplan
rm -f tfplan

# Output key values
echo ""
echo "=== Deployment Complete ==="
echo ""
ECR_URL=$(terraform output -raw ecr_repository_url)
ALB_URL=$(terraform output -raw alb_url)
RDS_ENDPOINT=$(terraform output -raw rds_endpoint)

echo "ECR Repository:  $ECR_URL"
echo "ALB URL:         $ALB_URL"
echo "RDS Endpoint:    $RDS_ENDPOINT"
echo ""
echo "=== Next Steps ==="
echo ""
echo "1. Push Docker image:"
echo "   aws ecr get-login-password --region ap-southeast-1 | docker login --username AWS --password-stdin $ECR_URL"
echo "   docker build -t $ECR_URL:latest ."
echo "   docker push $ECR_URL:latest"
echo ""
echo "2. Seed the databases:"
echo "   bash scripts/seed-rds.sh"
echo "   bash scripts/seed-opensearch.sh"
echo ""
echo "3. Set GitHub Actions secrets:"
echo "   AWS_ROLE_ARN  — IAM role for OIDC federation"
echo ""
echo "4. Test:"
echo "   curl $ALB_URL/v1/health"
