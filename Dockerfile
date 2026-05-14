# syntax=docker/dockerfile:1
# Container image for AWS Lambda (Python 3.12). See README Deployment.
FROM public.ecr.aws/lambda/python:3.12
WORKDIR ${LAMBDA_TASK_ROOT}
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
RUN pip install --no-cache-dir uv
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
RUN uv pip install --system --no-cache .
ENV MCP_TRANSPORT=streamable-http MCP_JSON_RESPONSE=true
CMD ["redshift_mcp.lambda_handler.handler"]
