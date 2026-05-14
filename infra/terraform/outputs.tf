output "alb_dns_name" {
  description = "ALB DNS name; MCP streamable HTTP is on port 80 at path /mcp (FastMCP default)."
  value       = aws_lb.this.dns_name
}

output "ecr_repository_url" {
  description = "ECR repository URL to docker push."
  value       = aws_ecr_repository.mcp.repository_url
}

output "rate_limit_table_name" {
  description = "DynamoDB table name; inject as RATE_LIMIT_DYNAMODB_TABLE if not using Terraform env injection."
  value       = aws_dynamodb_table.rate_limit.name
}

output "ecs_cluster_name" {
  value = aws_ecs_cluster.this.name
}

output "ecs_service_name" {
  value = aws_ecs_service.mcp.name
}

output "cloudwatch_log_group" {
  value = aws_cloudwatch_log_group.mcp.name
}
