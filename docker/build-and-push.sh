#!/usr/bin/env bash
# Build & push the LAMA single-image bundle to Docker Hub.
#
# Usage:
#   ./docker/build-and-push.sh               # builds + pushes :latest
#   ./docker/build-and-push.sh v1.2.0        # builds + pushes :v1.2.0 (+ :latest)
#   PUSH=0 ./docker/build-and-push.sh        # build only, skip push
#
# Requires `docker login` to a Docker Hub account that can push to mishramesh/lama.

set -euo pipefail

IMAGE="${IMAGE:-mishramesh/lama}"
TAG="${1:-latest}"
PUSH="${PUSH:-1}"

cd "$(dirname "$0")/.."

echo "[lama] building image $IMAGE:$TAG (and :latest) …"
docker build \
    -f Dockerfile \
    -t "$IMAGE:$TAG" \
    -t "$IMAGE:latest" \
    .

if [ "$PUSH" = "1" ]; then
    echo "[lama] pushing $IMAGE:$TAG and $IMAGE:latest to Docker Hub …"
    docker push "$IMAGE:$TAG"
    [ "$TAG" != "latest" ] && docker push "$IMAGE:latest"
else
    echo "[lama] PUSH=0 — skipping docker push."
fi

echo "[lama] done. Image size:"
docker images "$IMAGE" --format "table {{.Repository}}:{{.Tag}}\t{{.Size}}"
