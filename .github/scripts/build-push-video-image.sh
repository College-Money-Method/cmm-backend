#!/usr/bin/env bash
set -euo pipefail

# The video pipeline image is built from the same source tree as the API image
# but with ffmpeg baked in. The ECS task definition points at the mutable
# "$ENV-latest" tag, so pushing it is the whole deploy — RunTask pulls the image
# fresh on every launch and there is no service to roll.

docker build \
  -f Dockerfile.video \
  -t "$ECR_REGISTRY/$VIDEO_ECR_REPOSITORY:$IMAGE_TAG" \
  -t "$ECR_REGISTRY/$VIDEO_ECR_REPOSITORY:$ENV-latest" \
  .

docker push "$ECR_REGISTRY/$VIDEO_ECR_REPOSITORY:$IMAGE_TAG"
docker push "$ECR_REGISTRY/$VIDEO_ECR_REPOSITORY:$ENV-latest"
