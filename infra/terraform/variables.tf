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
  description = "ECR image URI including tag (e.g. 123456789012.dkr.ecr.us-east-1.amazonaws.com/redshift-mcp:v1)."
}

variable "container_port" {
  type        = number
  description = "Container listen port (must match MCP_PORT / Dockerfile EXPOSE)."
  default     = 8000
}

variable "desired_count" {
  type        = number
  description = "Fargate desired task count."
  default     = 1
}

variable "mcp_public_url" {
  type        = string
  description = "Public base URL of the MCP server (must match ALB URL + path clients use; Auth0 resource metadata)."
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
  description = "Optional Host header allowlist entries (e.g. my-alb.us-east-1.elb.amazonaws.com:80)."
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
  type        = number
  default     = 5439
}

variable "redshift_database" {
  type = string
}

variable "redshift_user" {
  type = string
}

variable "redshift_iam" {
  type        = bool
  description = "When true, use IAM database auth (no REDSHIFT_PASSWORD secret)."
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
  type        = string
  default     = ""
}

variable "redshift_aws_region" {
  type        = string
  description = "Region of the Redshift cluster when using IAM auth."
  default     = ""
}

variable "redshift_password_secret_arn" {
  type        = string
  description = "Secrets Manager ARN holding REDSHIFT_PASSWORD (required when redshift_iam=false)."
  default     = ""

  validation {
    condition     = var.redshift_iam || length(var.redshift_password_secret_arn) > 0
    error_message = "redshift_password_secret_arn is required when redshift_iam is false."
  }
}
