#!/usr/bin/env bash
# Integration test for the files container, run against a real Docker daemon.
# Usage: tests/integration/files_container.sh [image]   (default hertogs/platform:local)
set -euo pipefail

IMAGE="${1:-hertogs/platform:local}"
PROBE_IMAGE="${PROBE_IMAGE:-python:3.12-slim}"
SUFFIX="$$"
NET="hz-test-$SUFFIX"; WS="hz-ws-$SUFFIX"; DATA="hz-data-$SUFFIX"; NAME="hz-files-$SUFFIX"
HERE="$(cd "$(dirname "$0")" && pwd)"
TOKEN="$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')"

cleanup() {
  docker rm -f "$NAME" >/dev/null 2>&1 || true
  docker volume rm "$WS" "$DATA" >/dev/null 2>&1 || true
  docker network rm "$NET" >/dev/null 2>&1 || true
}
trap cleanup EXIT

docker network create --internal "$NET" >/dev/null
docker volume create "$WS" >/dev/null
docker run --rm --network none -v "$WS:/ws" "$PROBE_IMAGE" sh -c '
  mkdir -p /ws/outputs &&
  printf "hello from workspace" > /ws/outputs/a.txt &&
  echo "<h1>x</h1>" > /ws/outputs/p.html &&
  ln -s /etc/passwd /ws/outputs/evil &&
  chown -R 1000:1000 /ws && chmod 600 /ws/outputs/a.txt'

# Same hardening as compose.yaml.
docker run -d --name "$NAME" --network "$NET" --network-alias files \
  --read-only --cap-drop ALL --security-opt no-new-privileges:true --init \
  --user 1000:1000 --tmpfs /tmp:size=16m \
  -e FILES_MINT_TOKEN="$TOKEN" -e WORKSPACE_ROOT=/workspace -e FILES_DATA_DIR=/data \
  -v "$WS:/workspace:ro" -v "$DATA:/data" \
  "$IMAGE" python -m hertogs_platform.files >/dev/null

for _ in $(seq 1 30); do
  if docker exec "$NAME" python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8081/healthz', timeout=2)" 2>/dev/null; then
    break
  fi
  sleep 1
done

docker run --rm --network "$NET" -e TOKEN="$TOKEN" -v "$HERE/files_probe.py:/probe.py:ro" \
  "$PROBE_IMAGE" python /probe.py

start=$(date +%s)
docker stop "$NAME" >/dev/null
elapsed=$(( $(date +%s) - start ))
if [ "$elapsed" -gt 5 ]; then
  echo "FAIL: container took ${elapsed}s to stop (signal handling)"; exit 1
fi
echo "PASS: files container (stopped in ${elapsed}s)"
