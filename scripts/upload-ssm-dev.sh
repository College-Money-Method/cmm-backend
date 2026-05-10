#!/usr/bin/env bash
# Uploads secrets from a .env file to AWS SSM Parameter Store.
# Usage: bash scripts/upload-ssm-dev.sh <dev|prod>
# Reads: .env.<environment> in the repo root
# Writes to: /copilot/cmm-backend/<environment>/secrets/<KEY>

set -euo pipefail

ENV="${1:?Usage: $0 <dev|prod>}"

if [[ "$ENV" != "dev" && "$ENV" != "prod" ]]; then
  echo "Error: environment must be 'dev' or 'prod'"
  exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
ENV_FILE="$REPO_ROOT/.env.${ENV}"
PREFIX="/copilot/cmm-backend/${ENV}/secrets"
REGION="${AWS_REGION:-us-east-1}"

if [[ ! -f "$ENV_FILE" ]]; then
  echo "Error: '$ENV_FILE' not found"
  exit 1
fi

echo "Uploading '$ENV_FILE' → SSM path: $PREFIX"
echo "Region: $REGION"
echo ""

count=0

while IFS= read -r line || [[ -n "$line" ]]; do
  [[ -z "$line" || "$line" =~ ^[[:space:]]*# ]] && continue

  key="${line%%=*}"
  value="${line#*=}"
  key="$(echo "$key" | xargs)"
  [[ -z "$key" ]] && continue

  aws ssm put-parameter \
    --name "${PREFIX}/${key}" \
    --value "${value}" \
    --type SecureString \
    --overwrite \
    --region "$REGION" \
    --no-cli-pager > /dev/null

  echo "  ✓ ${key}"
  count=$((count + 1))
done < "$ENV_FILE"

echo ""
echo "Done! Uploaded $count parameters to SSM."
