terraform {
  required_version = ">= 1.5.0"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }
}

provider "aws" {
  region = var.aws_region
}

locals {
  common_tags = {
    Name    = "${var.project_name}-stack"
    Creator = var.creator_tag
    Purpose = var.purpose_tag
  }

  mcp_environment = concat(
    [
      { name = "MCP_TRANSPORT", value = "streamable-http" },
      { name = "MCP_HOST", value = "0.0.0.0" },
      { name = "MCP_PORT", value = tostring(var.container_port) },
      { name = "MCP_JSON_RESPONSE", value = "true" },
      { name = "MCP_PUBLIC_URL", value = var.mcp_public_url },
      { name = "AUTH0_DOMAIN", value = var.auth0_domain },
      { name = "AUTH0_AUDIENCE", value = var.auth0_audience },
      { name = "AUTH0_TIER_CLAIM", value = var.auth0_tier_claim },
      { name = "RATE_LIMIT_DYNAMODB_TABLE", value = aws_dynamodb_table.rate_limit.name },
      { name = "RATE_LIMIT_AWS_REGION", value = var.aws_region },
      { name = "REDSHIFT_PORT", value = tostring(var.redshift_port) },
      { name = "REDSHIFT_DATABASE", value = var.redshift_database },
      { name = "REDSHIFT_USER", value = var.redshift_user },
      { name = "REDSHIFT_IAM", value = var.redshift_iam ? "true" : "false" },
    ],
    var.redshift_host != "" ? [{ name = "REDSHIFT_HOST", value = var.redshift_host }] : [],
    var.redshift_iam ? [
      { name = "REDSHIFT_CLUSTER_IDENTIFIER", value = var.redshift_cluster_identifier },
      { name = "REDSHIFT_AWS_REGION", value = var.redshift_aws_region },
    ] : [],
    length(var.mcp_allowed_hosts) > 0 ? [{ name = "MCP_ALLOWED_HOSTS", value = join(",", var.mcp_allowed_hosts) }] : [],
    length(var.mcp_allowed_origins) > 0 ? [{ name = "MCP_ALLOWED_ORIGINS", value = join(",", var.mcp_allowed_origins) }] : [],
  )

  mcp_container = merge(
    {
      name      = "mcp"
      image     = var.container_image
      essential = true
      portMappings = [{
        containerPort = var.container_port
        hostPort        = var.container_port
        protocol        = "tcp"
      }]
      logConfiguration = {
        logDriver = "awslogs"
        options = {
          awslogs-group         = aws_cloudwatch_log_group.mcp.name
          awslogs-region        = var.aws_region
          awslogs-stream-prefix = "mcp"
        }
      }
      environment = local.mcp_environment
    },
    var.redshift_iam ? {} : {
      secrets = [
        {
          name      = "REDSHIFT_PASSWORD"
          valueFrom = var.redshift_password_secret_arn
        }
      ]
    },
  )
}

data "aws_vpc" "default" {
  default = true
}

data "aws_subnets" "default" {
  filter {
    name   = "vpc-id"
    values = [data.aws_vpc.default.id]
  }
}

resource "aws_dynamodb_table" "rate_limit" {
  name         = "${var.project_name}-mcp-rate"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "pk"
  range_key    = "sk"

  attribute {
    name = "pk"
    type = "S"
  }
  attribute {
    name = "sk"
    type = "S"
  }

  tags = merge(local.common_tags, { Name = "${var.project_name}-mcp-rate" })
}

resource "aws_cloudwatch_log_group" "mcp" {
  name              = "/ecs/${var.project_name}-mcp"
  retention_in_days = 14
  tags              = merge(local.common_tags, { Name = "${var.project_name}-mcp-logs" })
}

resource "aws_ecs_cluster" "this" {
  name = "${var.project_name}-mcp"
  tags = merge(local.common_tags, { Name = "${var.project_name}-ecs-cluster" })
}

resource "aws_ecr_repository" "mcp" {
  name                 = var.project_name
  image_tag_mutability = "MUTABLE"
  tags                 = merge(local.common_tags, { Name = "${var.project_name}-ecr" })
}

resource "aws_security_group" "alb" {
  name_prefix = "${var.project_name}-alb-"
  vpc_id      = data.aws_vpc.default.id
  description = "ALB for MCP HTTP"

  ingress {
    from_port   = 80
    to_port     = 80
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = merge(local.common_tags, { Name = "${var.project_name}-alb-sg" })
}

resource "aws_security_group" "svc" {
  name_prefix = "${var.project_name}-svc-"
  vpc_id      = data.aws_vpc.default.id
  description = "MCP Fargate tasks"

  ingress {
    from_port       = var.container_port
    to_port         = var.container_port
    protocol        = "tcp"
    security_groups = [aws_security_group.alb.id]
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = merge(local.common_tags, { Name = "${var.project_name}-task-sg" })
}

resource "aws_lb" "this" {
  name               = "${var.project_name}-mcp"
  load_balancer_type = "application"
  security_groups    = [aws_security_group.alb.id]
  subnets            = data.aws_subnets.default.ids

  tags = merge(local.common_tags, { Name = "${var.project_name}-alb" })
}

resource "aws_lb_target_group" "mcp" {
  name        = substr("${var.project_name}-mcp", 0, 32)
  port        = var.container_port
  protocol    = "HTTP"
  vpc_id      = data.aws_vpc.default.id
  target_type = "ip"

  health_check {
    path                = "/healthz"
    healthy_threshold   = 2
    unhealthy_threshold = 3
    timeout             = 5
    interval            = 30
    matcher             = "200"
  }

  tags = merge(local.common_tags, { Name = "${var.project_name}-tg" })
}

resource "aws_lb_listener" "http" {
  load_balancer_arn = aws_lb.this.arn
  port              = 80
  protocol          = "HTTP"

  default_action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.mcp.arn
  }
}

data "aws_iam_policy_document" "task_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["ecs-tasks.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "execution" {
  name_prefix        = "${var.project_name}-exec-"
  assume_role_policy = data.aws_iam_policy_document.task_assume.json
  tags               = merge(local.common_tags, { Name = "${var.project_name}-exec-role" })
}

resource "aws_iam_role_policy_attachment" "execution_managed" {
  role       = aws_iam_role.execution.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}

resource "aws_iam_role_policy" "execution_secrets" {
  count = var.redshift_iam ? 0 : 1
  name  = "${var.project_name}-read-db-secret"
  role  = aws_iam_role.execution.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = ["secretsmanager:GetSecretValue"]
      Resource = var.redshift_password_secret_arn
    }]
  })
}

resource "aws_iam_role" "task" {
  name_prefix        = "${var.project_name}-task-"
  assume_role_policy = data.aws_iam_policy_document.task_assume.json
  tags               = merge(local.common_tags, { Name = "${var.project_name}-task-role" })
}

data "aws_iam_policy_document" "task_policy" {
  statement {
    sid     = "Logs"
    actions = ["logs:CreateLogStream", "logs:PutLogEvents"]
    resources = ["${aws_cloudwatch_log_group.mcp.arn}:*"]
  }
  statement {
    sid = "RateLimit"
    actions = [
      "dynamodb:UpdateItem",
      "dynamodb:DescribeTable",
    ]
    resources = [aws_dynamodb_table.rate_limit.arn]
  }
}

resource "aws_iam_role_policy" "task" {
  name_prefix = "${var.project_name}-task-"
  role        = aws_iam_role.task.id
  policy      = data.aws_iam_policy_document.task_policy.json
}

resource "aws_ecs_task_definition" "mcp" {
  family                   = "${var.project_name}-mcp"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = "512"
  memory                   = "1024"
  execution_role_arn       = aws_iam_role.execution.arn
  task_role_arn            = aws_iam_role.task.arn

  container_definitions = jsonencode([local.mcp_container])

  tags = merge(local.common_tags, { Name = "${var.project_name}-taskdef" })
}

resource "aws_ecs_service" "mcp" {
  name            = "${var.project_name}-mcp"
  cluster         = aws_ecs_cluster.this.id
  task_definition = aws_ecs_task_definition.mcp.arn
  desired_count   = var.desired_count
  launch_type     = "FARGATE"

  network_configuration {
    subnets          = data.aws_subnets.default.ids
    security_groups  = [aws_security_group.svc.id]
    assign_public_ip = true
  }

  load_balancer {
    target_group_arn = aws_lb_target_group.mcp.arn
    container_name   = "mcp"
    container_port   = var.container_port
  }

  depends_on = [aws_lb_listener.http, aws_iam_role_policy_attachment.execution_managed]

  tags = merge(local.common_tags, { Name = "${var.project_name}-ecs-service" })
}
