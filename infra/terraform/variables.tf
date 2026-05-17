variable "aws_region" {
  type        = string
  description = "AWS region for all resources."
  default     = "us-east-1"
}

variable "project_name" {
  type        = string
  description = "Short name prefix for resources (letters/digits/hyphen)."
  default     = "redshift-mcp"
}

variable "creator_tag" {
  type        = string
  description = "Creator tag for Playground policy (who owns this stack)."
  default     = "unset"
}

variable "purpose_tag" {
  type        = string
  description = "Purpose tag for Playground policy (why this exists)."
  default     = "unset"
}

variable "container_image" {
  type        = string
  description = "ECR image URI including tag (Lambda package_type=Image)."
}

variable "lambda_memory_mb" {
  type        = number
  description = "Lambda memory (MB)."
  default     = 1024
}

variable "lambda_timeout_seconds" {
  type        = number
  description = "Lambda timeout (seconds); should exceed worst-case Redshift query time."
  default     = 120
}

variable "lambda_subnet_ids" {
  type        = list(string)
  description = "Optional VPC subnets for Lambda (use with lambda_security_group_ids when Redshift is VPC-only)."
  default     = []
}

variable "lambda_security_group_ids" {
  type        = list(string)
  description = "Optional security groups for Lambda (pair with lambda_subnet_ids)."
  default     = []
}

variable "mcp_public_url" {
  type        = string
  description = "Public MCP endpoint URL (function URL + /mcp, no trailing slash). Must match exactly what users enter in Claude Connectors. Example: https://xxx.lambda-url.us-east-1.on.aws/mcp"
}

variable "auth0_domain" {
  type        = string
  description = "Auth0 tenant domain, e.g. dev-xyz.us.auth0.com"
}

variable "auth0_audience" {
  type        = string
  description = "Auth0 API audience string configured for this MCP."
}

variable "auth0_tier_claim" {
  type        = string
  description = "JWT claim that carries tier (string or list of strings)."
  default     = "https://redshift-mcp/tier"
}

variable "mcp_allowed_hosts" {
  type        = list(string)
  description = "Optional extra Host header allowlist entries. If empty, Host is derived from mcp_public_url when possible."
  default     = []
}

variable "mcp_allowed_origins" {
  type        = list(string)
  description = "Optional Origin allowlist for DNS rebinding middleware."
  default     = []
}

variable "redshift_host" {
  type        = string
  description = "Redshift endpoint hostname (omit when using IAM-only driver settings)."
  default     = ""
}

variable "redshift_port" {
  type    = number
  default = 5439
}

variable "redshift_database" {
  type = string
}

variable "redshift_user" {
  type = string
}

variable "redshift_iam" {
  type        = bool
  description = "When true, use IAM database auth (no Secrets Manager DB password)."
  default     = false

  validation {
    condition = (
      !var.redshift_iam
      || (length(var.redshift_cluster_identifier) > 0 && length(var.redshift_aws_region) > 0)
    )
    error_message = "redshift_cluster_identifier and redshift_aws_region are required when redshift_iam is true."
  }
}

variable "redshift_cluster_identifier" {
  type    = string
  default = ""
}

variable "redshift_aws_region" {
  type        = string
  description = "Region of the Redshift cluster when using IAM auth."
  default     = ""
}

variable "redshift_password_secret_arn" {
  type        = string
  description = "Secrets Manager ARN for the DB password (plain string secret). Lambda reads at cold start via REDSHIFT_PASSWORD_SECRET_ARN; not used when redshift_iam=true."
  default     = ""

  validation {
    condition     = var.redshift_iam || length(var.redshift_password_secret_arn) > 0
    error_message = "redshift_password_secret_arn is required when redshift_iam is false."
  }
}
