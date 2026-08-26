#!/usr/bin/env bash
# Build for the VPS architecture, ship the images over SSH, deploy the stack.
#
# The images are built here rather than on the host: that box runs the live
# TradeVantage services in ~3.8 GB of RAM with roughly 1.3 GB free, and a Next
# production build there gets OOM-killed.
set -euo pipefail

HOST="${HOST:-tradevantage}"
STACK="${STACK:-alphaclimate}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

echo "==> building linux/amd64 images"
docker buildx build --platform linux/amd64 -f Dockerfile.api -t alphaclimate-api:latest --load .
docker buildx build --platform linux/amd64 -f Dockerfile.web -t alphaclimate-web:latest --load .

echo "==> shipping images to $HOST"
docker save alphaclimate-api:latest alphaclimate-web:latest | gzip -1 | \
  ssh "$HOST" 'gunzip | docker load'

echo "==> deploying stack"
ssh "$HOST" "mkdir -p /opt/$STACK"
scp deploy/stack.yml "$HOST:/opt/$STACK/stack.yml"
scp deploy/traefik-alphaclimate.yml "$HOST:/etc/dokploy/traefik/dynamic/$STACK.yml"
ssh "$HOST" "docker stack deploy -c /opt/$STACK/stack.yml $STACK --resolve-image never"

echo "==> waiting for services"
ssh "$HOST" "for i in \$(seq 1 40); do
  out=\$(docker service ls --filter label=com.docker.stack.namespace=$STACK --format '{{.Name}} {{.Replicas}}')
  echo \"\$out\"
  echo \"\$out\" | grep -qv '1/1' || break
  sleep 5
done"

echo "==> health check via origin"
ssh "$HOST" "curl -s --max-time 15 -H 'Host: alphaclimate.tradevantage.gg' http://127.0.0.1/api/health || true"
echo
echo "done. add the DNS A record for alphaclimate.tradevantage.gg -> 76.13.198.76 if it is not there yet."
