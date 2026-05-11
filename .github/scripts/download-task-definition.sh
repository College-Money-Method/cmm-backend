#!/usr/bin/env bash
set -euo pipefail

aws ecs describe-task-definition \
  --task-definition "cmm-$ENV-backend" \
  --query taskDefinition \
  > task-definition.json
