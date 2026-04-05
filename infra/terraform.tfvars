aws_region   = "ap-southeast-1"
project_name = "msrag"
environment  = "prod"

# ECS task sizing (shared between API + OpenSearch containers)
ecs_cpu    = 1024  # 1 vCPU
ecs_memory = 3072  # 3 GiB

# RDS
db_instance_class = "db.t3.micro"

# Secrets — set via environment variables or a .tfvars.secret file:
#   export TF_VAR_openai_api_key="sk-..."
#   export TF_VAR_tavily_api_key="tvly-..."
#   export TF_VAR_redis_url="rediss://..."
