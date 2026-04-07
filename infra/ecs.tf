# ECS Cluster
resource "aws_ecs_cluster" "main" {
  name = "${var.project_name}-cluster"

  setting {
    name  = "containerInsights"
    value = "enabled"
  }
}

# CloudWatch log groups
resource "aws_cloudwatch_log_group" "api" {
  name              = "/ecs/${var.project_name}/api"
  retention_in_days = 14
}

resource "aws_cloudwatch_log_group" "opensearch" {
  name              = "/ecs/${var.project_name}/opensearch"
  retention_in_days = 14
}

# EFS for OpenSearch data persistence
resource "aws_efs_file_system" "opensearch" {
  creation_token = "${var.project_name}-opensearch-data"
  encrypted      = true

  tags = { Name = "${var.project_name}-opensearch-efs" }
}

resource "aws_efs_access_point" "opensearch" {
  file_system_id = aws_efs_file_system.opensearch.id

  posix_user {
    uid = 1000
    gid = 1000
  }

  root_directory {
    path = "/opensearch-data"
    creation_info {
      owner_uid   = 1000
      owner_gid   = 1000
      permissions = "755"
    }
  }

  tags = { Name = "${var.project_name}-opensearch-ap" }
}

resource "aws_efs_mount_target" "opensearch" {
  count           = 2
  file_system_id  = aws_efs_file_system.opensearch.id
  subnet_id       = aws_subnet.public[count.index].id
  security_groups = [aws_security_group.efs.id]
}

# IAM role for ECS task execution
resource "aws_iam_role" "ecs_execution" {
  name = "${var.project_name}-ecs-execution"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Action = "sts:AssumeRole"
      Effect = "Allow"
      Principal = { Service = "ecs-tasks.amazonaws.com" }
    }]
  })
}

resource "aws_iam_role_policy_attachment" "ecs_execution" {
  role       = aws_iam_role.ecs_execution.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}

# Allow ECS to read secrets
resource "aws_iam_role_policy" "ecs_secrets" {
  name = "${var.project_name}-ecs-secrets"
  role = aws_iam_role.ecs_execution.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = ["secretsmanager:GetSecretValue"]
      Resource = [
        aws_secretsmanager_secret.openai_api_key.arn,
        aws_secretsmanager_secret.tavily_api_key.arn,
        aws_secretsmanager_secret.redis_url.arn,
        aws_secretsmanager_secret.db_password.arn,
      ]
    }]
  })
}

# IAM role for ECS task (application permissions)
resource "aws_iam_role" "ecs_task" {
  name = "${var.project_name}-ecs-task"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Action = "sts:AssumeRole"
      Effect = "Allow"
      Principal = { Service = "ecs-tasks.amazonaws.com" }
    }]
  })
}

# Allow ECS Exec (SSM) for task role — needed for port-forwarding and shell access
resource "aws_iam_role_policy" "ecs_exec_ssm" {
  name = "${var.project_name}-ecs-exec-ssm"
  role = aws_iam_role.ecs_task.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow"
      Action = [
        "ssmmessages:CreateControlChannel",
        "ssmmessages:CreateDataChannel",
        "ssmmessages:OpenControlChannel",
        "ssmmessages:OpenDataChannel"
      ]
      Resource = "*"
    }]
  })
}

# Task definition — multi-container: API + OpenSearch sidecar
resource "aws_ecs_task_definition" "api" {
  family                   = "${var.project_name}-api"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = var.ecs_cpu
  memory                   = var.ecs_memory
  execution_role_arn       = aws_iam_role.ecs_execution.arn
  task_role_arn            = aws_iam_role.ecs_task.arn

  volume {
    name = "opensearch-data"

    efs_volume_configuration {
      file_system_id     = aws_efs_file_system.opensearch.id
      transit_encryption = "ENABLED"

      authorization_config {
        access_point_id = aws_efs_access_point.opensearch.id
        iam             = "DISABLED"
      }
    }
  }

  container_definitions = jsonencode([
    {
      name      = "api"
      image     = "${aws_ecr_repository.api.repository_url}:latest"
      essential = true
      cpu       = var.api_container_cpu
      memory    = var.api_container_memory

      portMappings = [{
        containerPort = 8000
        protocol      = "tcp"
      }]

      environment = [
        { name = "OPENSEARCH_HOST", value = "localhost" },
        { name = "OPENSEARCH_PORT", value = "9200" },
        { name = "PG_HOST", value = aws_db_instance.main.address },
        { name = "PG_PORT", value = "5432" },
        { name = "PG_DBNAME", value = var.db_name },
        { name = "PG_USER", value = var.db_username },
        { name = "CACHE_ENABLED", value = "true" },
        { name = "CORPUS_VERSION", value = "v1" },
        { name = "WAIT_FOR_OPENSEARCH", value = "1" },
      ]

      secrets = [
        { name = "OPENAI_API_KEY", valueFrom = aws_secretsmanager_secret.openai_api_key.arn },
        { name = "TAVILY_API_KEY", valueFrom = aws_secretsmanager_secret.tavily_api_key.arn },
        { name = "REDIS_URL", valueFrom = aws_secretsmanager_secret.redis_url.arn },
        { name = "PG_PASSWORD", valueFrom = aws_secretsmanager_secret.db_password.arn },
      ]

      logConfiguration = {
        logDriver = "awslogs"
        options = {
          "awslogs-group"         = aws_cloudwatch_log_group.api.name
          "awslogs-region"        = var.aws_region
          "awslogs-stream-prefix" = "api"
        }
      }

      dependsOn = [{
        containerName = "opensearch"
        condition     = "HEALTHY"
      }]
    },
    {
      name      = "opensearch"
      image     = "opensearchproject/opensearch:2.18.0"
      essential = true
      cpu       = var.opensearch_container_cpu
      memory    = var.opensearch_container_memory

      # Clean stale NFS node lock on startup (EFS doesn't reliably release locks on task stop)
      entryPoint = ["sh", "-c"]
      command    = ["rm -f /usr/share/opensearch/data/nodes/0/node.lock && exec ./opensearch-docker-entrypoint.sh"]

      portMappings = [{
        containerPort = 9200
        protocol      = "tcp"
      }]

      environment = [
        { name = "discovery.type", value = "single-node" },
        { name = "DISABLE_SECURITY_PLUGIN", value = "true" },
        { name = "OPENSEARCH_JAVA_OPTS", value = "-Xms512m -Xmx512m" },
        { name = "plugins.ml_commons.only_run_on_ml_node", value = "false" },
        { name = "plugins.ml_commons.model_access_control_enabled", value = "false" },
        { name = "plugins.ml_commons.native_memory_threshold", value = "99" },
      ]

      mountPoints = [{
        sourceVolume  = "opensearch-data"
        containerPath = "/usr/share/opensearch/data"
        readOnly      = false
      }]

      healthCheck = {
        command     = ["CMD-SHELL", "curl -sf http://localhost:9200/_cluster/health || exit 1"]
        interval    = 15
        timeout     = 5
        retries     = 10
        startPeriod = 60
      }

      logConfiguration = {
        logDriver = "awslogs"
        options = {
          "awslogs-group"         = aws_cloudwatch_log_group.opensearch.name
          "awslogs-region"        = var.aws_region
          "awslogs-stream-prefix" = "opensearch"
        }
      }
    }
  ])
}

# Application Load Balancer
resource "aws_lb" "main" {
  name               = "${var.project_name}-alb"
  internal           = false
  load_balancer_type = "application"
  security_groups    = [aws_security_group.alb.id]
  subnets            = aws_subnet.public[*].id

  tags = { Name = "${var.project_name}-alb" }
}

resource "aws_lb_target_group" "api" {
  name        = "${var.project_name}-api-tg"
  port        = 8000
  protocol    = "HTTP"
  vpc_id      = aws_vpc.main.id
  target_type = "ip"

  health_check {
    path                = "/v1/health"
    port                = "traffic-port"
    healthy_threshold   = 2
    unhealthy_threshold = 5
    timeout             = 10
    interval            = 30
    matcher             = "200"
  }
}

resource "aws_lb_listener" "http" {
  load_balancer_arn = aws_lb.main.arn
  port              = 80
  protocol          = "HTTP"

  default_action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.api.arn
  }
}

# ECS Service
resource "aws_ecs_service" "api" {
  name            = "${var.project_name}-api"
  cluster         = aws_ecs_cluster.main.id
  task_definition = aws_ecs_task_definition.api.arn
  desired_count          = 1
  launch_type            = "FARGATE"
  enable_execute_command = true

  network_configuration {
    subnets          = aws_subnet.public[*].id
    security_groups  = [aws_security_group.ecs.id]
    assign_public_ip = true
  }

  load_balancer {
    target_group_arn = aws_lb_target_group.api.arn
    container_name   = "api"
    container_port   = 8000
  }

  depends_on = [aws_lb_listener.http]
}
