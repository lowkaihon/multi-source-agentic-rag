variable "aws_region" {
  description = "AWS region for all resources"
  type        = string
  default     = "ap-southeast-1"
}

variable "project_name" {
  description = "Project name used for resource naming"
  type        = string
  default     = "msrag"
}

variable "environment" {
  description = "Environment name (dev, staging, prod)"
  type        = string
  default     = "prod"
}

# ECS
variable "ecs_cpu" {
  description = "ECS task CPU units (1024 = 1 vCPU)"
  type        = number
  default     = 1024
}

variable "ecs_memory" {
  description = "ECS task memory in MiB"
  type        = number
  default     = 3072
}

variable "api_container_cpu" {
  description = "CPU units for API container"
  type        = number
  default     = 512
}

variable "api_container_memory" {
  description = "Memory in MiB for API container"
  type        = number
  default     = 1024
}

variable "opensearch_container_cpu" {
  description = "CPU units for OpenSearch sidecar"
  type        = number
  default     = 512
}

variable "opensearch_container_memory" {
  description = "Memory in MiB for OpenSearch sidecar"
  type        = number
  default     = 2048
}

# RDS
variable "db_instance_class" {
  description = "RDS instance class"
  type        = string
  default     = "db.t3.micro"
}

variable "db_name" {
  description = "PostgreSQL database name"
  type        = string
  default     = "mas_compliance"
}

variable "db_username" {
  description = "PostgreSQL master username"
  type        = string
  default     = "msrag"
}

# Secrets (set via terraform.tfvars or environment)
variable "openai_api_key" {
  description = "OpenAI API key"
  type        = string
  sensitive   = true
}

variable "tavily_api_key" {
  description = "Tavily API key (optional)"
  type        = string
  default     = ""
  sensitive   = true
}

variable "redis_url" {
  description = "Upstash Redis URL"
  type        = string
  default     = ""
  sensitive   = true
}

variable "langchain_api_key" {
  description = "LangChain/LangSmith API key for tracing"
  type        = string
  default     = ""
  sensitive   = true
}
