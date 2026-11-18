#!/usr/bin/env bash
# End-to-end test of the app platform: deployer, Pomerium, the static server and
# the admin page, started from the real compose.yaml (without the GPU workspace
# and without the tunnel). A probe container plays the workspace and the owner.
#
# For CI and development machines only: it uses the real container, network
# and volume names, so it refuses to run next to an installed stack.
#
#   tests/integration/platform_stack.sh [image]
set -euo pipefail
cd "$(dirname "$0")/../.."
IMAGE="${1:-mcp-hosting-workspace/platform:local}"
[ "$IMAGE" = "mcp-hosting-workspace/platform:local" ] || docker tag "$IMAGE" mcp-hosting-workspace/platform:local

if docker container inspect mcp-hosting-deployer >/dev/null 2>&1 \
    || docker volume inspect mcp-hosting-workspace-data >/dev/null 2>&1; then
  echo "A MCP Hosting Workspace stack (or its workspace volume) exists on this machine; not running." >&2
  exit 2
fi

ENV_FILE="$(mktemp)"
{
  echo "PLATFORM_OWNER_EMAIL=owner@mcp-hosting.test"
  echo "GOOGLE_CLIENT_ID=dummy.apps.googleusercontent.com"
  echo "GOOGLE_CLIENT_SECRET=dummy"
  echo "ADMIN_LOCAL_TOKEN=$(head -c 18 /dev/urandom | base64 | tr '+/' '-_')"
  # Docker Desktop's socket is group 0; Linux hosts (CI) use a "docker" group.
  echo "DOCKER_SOCKET_GID=$(stat -c %g /var/run/docker.sock)"
  docker run --rm --network none "$IMAGE" python -m mcp_hosting_platform.setup_secrets \
    DEPLOYER_API_TOKEN ADMIN_CSRF_KEY DEPLOYER_SECRETS_KEY \
    POMERIUM_SHARED_SECRET POMERIUM_COOKIE_SECRET POMERIUM_SIGNING_KEY
} > "$ENV_FILE"
TOKEN="$(grep '^DEPLOYER_API_TOKEN=' "$ENV_FILE" | cut -d= -f2-)"
ADMIN_TOKEN="$(grep '^ADMIN_LOCAL_TOKEN=' "$ENV_FILE" | cut -d= -f2-)"
COMPOSE=(docker compose --env-file "$ENV_FILE" -f compose.yaml -f compose.local.yaml)
PROBE=""

cleanup() {
  status=$?
  if [ "$status" -ne 0 ]; then
    for c in mcp-hosting-deployer mcp-hosting-pomerium mcp-hosting-static mcp-hosting-admin; do
      echo "--- logs $c"; docker logs --tail 40 "$c" 2>&1 || true
    done
  fi
  [ -n "$PROBE" ] && docker rm -f "$PROBE" >/dev/null 2>&1 || true
  docker ps -aq --filter label=mcp-hosting-workspace.app | xargs -r docker rm --force >/dev/null 2>&1 || true
  "${COMPOSE[@]}" down --remove-orphans -t 5 >/dev/null 2>&1 || true
  # Only volumes this script created (it refused to start if any existed).
  docker volume rm mcp-hosting-deployer-data mcp-hosting-sites mcp-hosting-pomerium-config mcp-hosting-pomerium-data \
    mcp-hosting-static-logs mcp-hosting-control mcp-hosting-admin-socket mcp-hosting-workspace-secrets mcp-hosting-workspace-data mcp-hosting-build-scratch >/dev/null 2>&1 || true
  docker volume ls -q --filter label=mcp-hosting-workspace.buildcache | xargs -r docker volume rm >/dev/null 2>&1 || true
  docker volume ls -q --filter label=mcp-hosting-workspace.platform=runtime | xargs -r docker volume rm >/dev/null 2>&1 || true
  docker network ls -q --filter label=mcp-hosting-workspace.platform=runtime | xargs -r docker network rm >/dev/null 2>&1 || true
  rm -f "$ENV_FILE"
  exit "$status"
}
trap cleanup EXIT

echo "Starting deployer, pomerium, static and admin (admin in local test mode)..."
"${COMPOSE[@]}" up -d --wait --wait-timeout 180 deployer pomerium static admin

PROBE="$(docker create -e DEPLOYER_API_TOKEN="$TOKEN" -e ADMIN_LOCAL_TOKEN="$ADMIN_TOKEN" --network mcp-hosting-deployer-api \
  -v "$PWD/tests/integration/platform_probe.py:/probe.py:ro" "$IMAGE" python /probe.py)"
docker network connect mcp-hosting-platform "$PROBE"
docker network connect mcp-hosting-edge-admin "$PROBE"
docker start -a "$PROBE"

echo "Runtime isolation and owner controls:"
docker exec -i mcp-hosting-deployer python - < tests/integration/runtime_probe.py
echo "Capabilities, development, exports and image builds:"
docker exec -i mcp-hosting-deployer python - < tests/integration/capabilities_probe.py

# The probe sent a forged X-Pomerium-Claim-Email with every visit; the static
# server (which logs request headers) must never have received it.
if docker exec mcp-hosting-static grep -qi "x-pomerium-claim-email" /logs/access.log; then
  echo "FAIL forged identity header reached the app"; exit 1
fi
echo "ok   forged identity header is stripped before the app"
echo "Backup hook:"
docker exec mcp-hosting-deployer python -m mcp_hosting_platform.deployer.cli snapshot-db >/dev/null
docker exec mcp-hosting-deployer test -s /data/backup/deployer.sqlite3
echo "ok   snapshot-db"
docker exec mcp-hosting-deployer python -m mcp_hosting_platform.deployer.cli gaming status >/dev/null
echo "ok   Windows CLI"
echo "PASS: platform stack"
