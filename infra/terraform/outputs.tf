output "mcp_function_url" {
  description = "HTTPS Lambda Function URL (no path); MCP defaults to /mcp. Set mcp_public_url in tfvars to this value (then re-apply if Auth0 resource URL must match exactly)."
  value       = aws_lambda_function_url.mcp.function_url
}

output "ecr_repository_url" {
  description = "ECR repository URL to docker push."
  value       = aws_ecr_repository.mcp.repository_url
}

output "rate_limit_table_name" {
  description = "DynamoDB table name; also injected as RATE_LIMIT_DYNAMODB_TABLE."
  value       = aws_dynamodb_table.rate_limit.name
}

output "lambda_function_name" {
  description = "Deployed Lambda function name."
  value       = aws_lambda_function.mcp.function_name
}

output "cloudwatch_log_group" {
  description = "CloudWatch log group for Lambda."
  value       = aws_cloudwatch_log_group.lambda_mcp.name
}
