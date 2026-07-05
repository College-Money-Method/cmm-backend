#!/usr/bin/env bash
set -euo pipefail

NETWORK_CONFIG=$(aws ecs describe-services \
  --cluster "cmm-$ENV-cluster" \
  --services "cmm-$ENV-backend" \
  --query 'services[0].networkConfiguration' \
  --output json)

TASK_ARN=$(aws ecs run-task \
  --cluster "cmm-$ENV-cluster" \
  --task-definition "$TASK_DEF_ARN" \
  --launch-type FARGATE \
  --network-configuration "$NETWORK_CONFIG" \
  --overrides '{"containerOverrides":[{"name":"backend","command":["alembic","upgrade","heads"]}]}' \
  --query 'tasks[0].taskArn' \
  --output text)

echo "Waiting for migration task $TASK_ARN to complete..."
aws ecs wait tasks-stopped --cluster "cmm-$ENV-cluster" --tasks "$TASK_ARN"

TASK_INFO=$(aws ecs describe-tasks \
  --cluster "cmm-$ENV-cluster" \
  --tasks "$TASK_ARN" \
  --query 'tasks[0].{exitCode:containers[0].exitCode,stopCode:stopCode,stoppedReason:stoppedReason,containerReason:containers[0].reason}' \
  --output json)

EXIT_CODE=$(jq -r '.exitCode' <<< "$TASK_INFO")

if [[ "$EXIT_CODE" != "0" ]]; then
  echo "Migration task failed (exit code: $EXIT_CODE)"
  echo "  stopCode:        $(jq -r '.stopCode' <<< "$TASK_INFO")"
  echo "  stoppedReason:   $(jq -r '.stoppedReason' <<< "$TASK_INFO")"
  echo "  containerReason: $(jq -r '.containerReason' <<< "$TASK_INFO")"
  # exit code null = container never started (image pull / secrets / network init)
  exit 1
fi

echo "Migrations completed successfully"
