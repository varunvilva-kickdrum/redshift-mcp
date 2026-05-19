#!/usr/bin/env bash
# Build, push to ECR, and update Lambda (via Terraform) for redshift-mcp.
#
# Prerequisites:
#   - Docker
#   - AWS CLI credentials for account 503226040441 (default profile or AWS_PROFILE)
#   - Terraform >= 1.5
#
# Usage (uses your default ~/.aws/credentials unless AWS_PROFILE is set):
#   ./scripts/deploy-image.sh v4
#
set -euo pipefail

TAG="${1:-v4}"
REGION="${AWS_REGION:-us-east-1}"
ACCOUNT_ID="${AWS_ACCOUNT_ID:-503226040441}"
REPO="${ACCOUNT_ID}.dkr.ecr.${REGION}.amazonaws.com/redshift-mcp"
IMAGE="${REPO}:${TAG}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
TF_DIR="${ROOT}/infra/terraform"

echo "==> AWS identity"
aws sts get-caller-identity
ACTUAL_ACCOUNT="$(aws sts get-caller-identity --query Account --output text)"
if [[ "${ACTUAL_ACCOUNT}" != "${ACCOUNT_ID}" ]]; then
  echo "ERROR: Expected AWS account ${ACCOUNT_ID}, got ${ACTUAL_ACCOUNT}." >&2
  echo "Use credentials for account ${ACCOUNT_ID} (default profile or: export AWS_PROFILE=...)." >&2
  exit 1
fi

echo "==> ECR login"
aws ecr get-login-password --region "${REGION}" \
  | docker login --username AWS --password-stdin "${ACCOUNT_ID}.dkr.ecr.${REGION}.amazonaws.com"

echo "==> Docker build (linux/amd64 for Lambda)"
# Lambda requires Docker Image Manifest V2 Schema 2. Disable BuildKit attestations
# (provenance/SBOM) or ECR push uses an OCI index Lambda rejects with:
#   InvalidParameterValueException: The image manifest, config or layer media type ... is not supported.
docker build \
  --platform linux/amd64 \
  --provenance=false \
  --sbom=false \
  -t "${IMAGE}" \
  "${ROOT}"

echo "==> Push ${IMAGE}"
docker push "${IMAGE}"

echo "==> Verify ECR manifest (Lambda requires Docker Schema 2, not OCI index)"
MANIFEST_TYPE="$(
  aws ecr batch-get-image \
    --repository-name redshift-mcp \
    --image-ids "imageTag=${TAG}" \
    --region "${REGION}" \
    --query 'images[0].imageManifestMediaType' \
    --output text
)"
if [[ "${MANIFEST_TYPE}" != "application/vnd.docker.distribution.manifest.v2+json" ]]; then
  echo "ERROR: ${IMAGE} has manifest type ${MANIFEST_TYPE}" >&2
  echo "Rebuild with: docker build --platform linux/amd64 --provenance=false --sbom=false ..." >&2
  exit 1
fi

echo "==> Update terraform.tfvars container_image"
TFVARS="${TF_DIR}/terraform.tfvars"
if [[ ! -f "${TFVARS}" ]]; then
  echo "ERROR: Missing ${TFVARS}" >&2
  exit 1
fi
if grep -q '^container_image' "${TFVARS}"; then
  sed -i.bak "s|^container_image = .*|container_image = \"${IMAGE}\"|" "${TFVARS}"
  rm -f "${TFVARS}.bak"
else
  echo "container_image = \"${IMAGE}\"" >> "${TFVARS}"
fi

echo "==> Terraform apply (Lambda image only)"
cd "${TF_DIR}"
terraform apply -auto-approve -target=aws_lambda_function.mcp

echo "==> Done. Image: ${IMAGE}"
echo "    Health: curl -sS \"\$(terraform output -raw mcp_function_url)healthz\""
