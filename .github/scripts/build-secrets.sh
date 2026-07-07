#!/usr/bin/env bash
set -euo pipefail

SSM_PREFIX="arn:aws:ssm:${AWS_REGION}:${AWS_ACCOUNT_ID}:parameter/copilot/${ECS_APP_NAME}/${ENV}/secrets"
SSM_PATH="/copilot/${ECS_APP_NAME}/${ENV}/secrets"

# Preflight: every secret in the manifest must exist in SSM for this env.
# ECS only resolves secrets at task launch, so a missing parameter would
# otherwise surface as an opaque TaskFailedToStart with exit code None.
MISSING=()
while IFS= read -r batch; do
  # get-parameters accepts max 10 names per call; only query InvalidParameters
  # so secret values never touch the CI log
  INVALID=$(aws ssm get-parameters --names $batch --query 'InvalidParameters[]' --output text)
  [[ -n "$INVALID" && "$INVALID" != "None" ]] && MISSING+=($INVALID)
done < <(yq -r '.secrets[]' manifest.yml | sed "s|^|$SSM_PATH/|" | xargs -n 10 echo)

if (( ${#MISSING[@]} > 0 )); then
  echo "ERROR: manifest.yml lists secrets that do not exist in SSM for env '$ENV':" >&2
  printf '  %s\n' "${MISSING[@]}" >&2
  echo "Create them with: aws ssm put-parameter --name <path> --type SecureString --value <value>" >&2
  exit 1
fi

SECRETS=$(yq -r '.secrets[]' manifest.yml | jq -R -s -c '
  split("\n") | map(select(length > 0)) | map({
    name: .,
    valueFrom: ("'"$SSM_PREFIX"'/" + .)
  })
')

jq --argjson secrets "$SECRETS" \
  '.containerDefinitions[0].secrets = $secrets' \
  task-definition.json > task-definition-updated.json

mv task-definition-updated.json task-definition.json
