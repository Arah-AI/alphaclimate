#!/usr/bin/env bash
# Pull the images GitHub Actions published and roll the stack.
#
# Nothing is built here or on the host. CI runners are natively amd64, so they
# build in minutes where this laptop needed over an hour under emulation and
# the VPS has no headroom to build at all.
set -euo pipefail

HOST="${HOST:-tradevantage}"
STACK="${STACK:-alphaclimate}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

echo "==> installing stack + traefik route on $HOST"
ssh "$HOST" "mkdir -p /opt/$STACK"
scp -q deploy/stack.yml "$HOST:/opt/$STACK/stack.yml"
scp -q deploy/traefik-alphaclimate.yml "$HOST:/etc/dokploy/traefik/dynamic/$STACK.yml"

echo "==> pulling images"
ssh "$HOST" "docker pull ghcr.io/rafieamandio/alphaclimate-api:latest \
          && docker pull ghcr.io/rafieamandio/alphaclimate-web:latest"

echo "==> deploying"
ssh "$HOST" "docker stack deploy -c /opt/$STACK/stack.yml $STACK --with-registry-auth"

echo "==> waiting for replicas"
ssh "$HOST" "for i in \$(seq 1 60); do
  out=\$(docker service ls --filter label=com.docker.stack.namespace=$STACK --format '{{.Name}} {{.Replicas}}')
  if ! echo \"\$out\" | grep -qv '1/1'; then break; fi
  sleep 5
done; docker service ls --filter label=com.docker.stack.namespace=$STACK --format '{{.Name}} {{.Replicas}}'"

echo "==> health via origin"
ssh "$HOST" "curl -s --max-time 20 -H 'Host: alphaclimate.tradevantage.gg' http://127.0.0.1/api/health"
echo
