#!/usr/bin/env bash
set -euo pipefail

aws ecs describe-task-definition \
  --task-definition "cmm-$ENV-backend" \
  --query taskDefinition \
  | jq 'del(.taskDefinitionArn,.revision,.status,.registeredAt,.registeredBy,.deregisteredAt,.requiresAttributes,.compatibilities)' \
  > task-definition.json
