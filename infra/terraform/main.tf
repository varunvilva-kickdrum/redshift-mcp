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

data "aws_caller_identity" "current" {}

locals {
  common_tags = {
    Name    = "${var.project_name}-stack"
    Creator = var.creator_tag
    Purpose = var.purpose_tag
  }

  # Host header for FastMCP transport security (no port for default HTTPS).
  mcp_host_from_public_url = trimspace(try(regex("https?://([^/]+)", var.mcp_public_url)[0], ""))

  mcp_environment = merge(
    {
      MCP_TRANSPORT       = "streamable-http"
      MCP_JSON_RESPONSE   = "true"
      MCP_PUBLIC_URL      = var.mcp_public_url
      AUTH0_DOMAIN        = var.auth0_domain
      AUTH0_AUDIENCE      = var.auth0_audience
      AUTH0_TIER_CLAIM    = var.auth0_tier_claim
      RATE_LIMIT_DYNAMODB_TABLE = aws_dynamodb_table.rate_limit.name
      RATE_LIMIT_AWS_REGION     = var.aws_region
      REDSHIFT_PORT         = tostring(var.redshift_port)
      REDSHIFT_DATABASE     = var.redshift_database
      REDSHIFT_USER         = var.redshift_user
      REDSHIFT_IAM          = var.redshift_iam ? "true" : "false"
    },
    var.redshift_host != "" ? { REDSHIFT_HOST = var.redshift_host } : {},
    var.redshift_iam ? {
      REDSHIFT_CLUSTER_IDENTIFIER = var.redshift_cluster_identifier
      REDSHIFT_AWS_REGION         = var.redshift_aws_region
    } : {},
    length(var.mcp_allowed_hosts) > 0 ? { MCP_ALLOWED_HOSTS = join(",", var.mcp_allowed_hosts) } : (
      local.mcp_host_from_public_url != "" ? { MCP_ALLOWED_HOSTS = local.mcp_host_from_public_url } : {}
    ),
    length(var.mcp_allowed_origins) > 0 ? { MCP_ALLOWED_ORIGINS = join(",", var.mcp_allowed_origins) } : {},
    var.redshift_iam ? {} : { REDSHIFT_PASSWORD_SECRET_ARN = var.redshift_password_secret_arn },
  )
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

resource "aws_cloudwatch_log_group" "lambda_mcp" {
  name              = "/aws/lambda/${var.project_name}-mcp"
  retention_in_days = 14
  tags              = merge(local.common_tags, { Name = "${var.project_name}-lambda-logs" })
}

resource "aws_ecr_repository" "mcp" {
  name                 = var.project_name
  image_tag_mutability = "MUTABLE"
  tags                 = merge(local.common_tags, { Name = "${var.project_name}-ecr" })
}

data "aws_iam_policy_document" "lambda_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "lambda" {
  name_prefix        = "${var.project_name}-lambda-"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume.json
  tags               = merge(local.common_tags, { Name = "${var.project_name}-lambda-role" })
}

resource "aws_iam_role_policy_attachment" "lambda_basic" {
  role       = aws_iam_role.lambda.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

resource "aws_iam_role_policy_attachment" "lambda_vpc" {
  count      = length(var.lambda_subnet_ids) > 0 && length(var.lambda_security_group_ids) > 0 ? 1 : 0
  role       = aws_iam_role.lambda.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaVPCAccessExecutionRole"
}

data "aws_iam_policy_document" "lambda_policy" {
  statement {
    sid     = "Logs"
    actions = ["logs:CreateLogStream", "logs:PutLogEvents"]
    resources = ["${aws_cloudwatch_log_group.lambda_mcp.arn}:*"]
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

resource "aws_iam_role_policy" "lambda_core" {
  name_prefix = "${var.project_name}-lambda-"
  role        = aws_iam_role.lambda.id
  policy      = data.aws_iam_policy_document.lambda_policy.json
}

resource "aws_iam_role_policy" "lambda_secrets" {
  count = var.redshift_iam ? 0 : 1
  name  = "${var.project_name}-read-db-secret"
  role  = aws_iam_role.lambda.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = ["secretsmanager:GetSecretValue"]
      Resource = var.redshift_password_secret_arn
    }]
  })
}

resource "aws_iam_role_policy" "lambda_redshift_iam" {
  count = var.redshift_iam ? 1 : 0
  name  = "${var.project_name}-redshift-dbuser"
  role  = aws_iam_role.lambda.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow"
      Action = [
        "redshift:GetClusterCredentials",
        "redshift:DescribeClusters",
      ]
      Resource = [
        "arn:aws:redshift:${var.aws_region}:${data.aws_caller_identity.current.account_id}:dbname:${var.redshift_cluster_identifier}/*",
        "arn:aws:redshift:${var.aws_region}:${data.aws_caller_identity.current.account_id}:cluster:${var.redshift_cluster_identifier}",
      ]
    }]
  })
}

resource "aws_lambda_function" "mcp" {
  function_name = "${var.project_name}-mcp"
  role          = aws_iam_role.lambda.arn
  package_type  = "Image"
  image_uri     = var.container_image
  timeout       = var.lambda_timeout_seconds
  memory_size   = var.lambda_memory_mb

  logging_config {
    log_format = "Text"
    log_group  = aws_cloudwatch_log_group.lambda_mcp.name
  }

  image_config {
    command = ["redshift_mcp.lambda_handler.handler"]
  }

  dynamic "vpc_config" {
    for_each = length(var.lambda_subnet_ids) > 0 && length(var.lambda_security_group_ids) > 0 ? [1] : []
    content {
      subnet_ids         = var.lambda_subnet_ids
      security_group_ids = var.lambda_security_group_ids
    }
  }

  environment {
    variables = local.mcp_environment
  }

  tags = merge(local.common_tags, { Name = "${var.project_name}-lambda" })

  depends_on = [
    aws_iam_role_policy_attachment.lambda_basic,
    aws_cloudwatch_log_group.lambda_mcp,
  ]
}

resource "aws_lambda_function_url" "mcp" {
  function_name      = aws_lambda_function.mcp.function_name
  authorization_type = "NONE"
  invoke_mode        = "BUFFERED"

  cors {
    # Wildcard origins are incompatible with allow_credentials=true on Function URLs.
    allow_credentials = false
    allow_origins     = ["*"]
    allow_methods     = ["*"]
    allow_headers     = ["*"]
    max_age           = 86400
  }
}
