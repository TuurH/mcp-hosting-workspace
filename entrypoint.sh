#!/usr/bin/env bash
set -euo pipefail

mkdir -p \
  /workspace/inbox \
  /workspace/outputs \
  /workspace/projects \
  /workspace/models \
  /workspace/.cache/huggingface \
  /workspace/.cache/torch \
  /workspace/.cache/uv \
  /workspace/.cache/pip \
  /workspace/.mcp/jobs \
  /workspace/.mcp/runs

# Persistent overlay environment. It contains user-installed packages while a
# .pth file exposes the tested packages baked into /opt/venv.
if [[ ! -x /workspace/.venv/bin/python ]]; then
  /usr/bin/python3 -m venv /workspace/.venv
fi

OVERLAY_SITE="$('/workspace/.venv/bin/python' -c 'import site; print(site.getsitepackages()[0])')"
BASE_SITE="$('/opt/venv/bin/python' -c 'import site; print(site.getsitepackages()[0])')"
printf '%s\n' "$BASE_SITE" >"${OVERLAY_SITE}/hertogs-base-packages.pth"

export VIRTUAL_ENV=/workspace/.venv
export PATH="/workspace/.venv/bin:/opt/venv/bin:/home/ai/.local/bin:${PATH}"
export HF_HOME=/workspace/.cache/huggingface
export TORCH_HOME=/workspace/.cache/torch
export XDG_CACHE_HOME=/workspace/.cache
export UV_CACHE_DIR=/workspace/.cache/uv
export PIP_CACHE_DIR=/workspace/.cache/pip

# Only cloudflared (fixed address on the edge-mcp network) may set
# X-Forwarded-For / X-Forwarded-Proto.
exec /opt/venv/bin/uvicorn app.server:app \
  --app-dir /opt/app \
  --host 0.0.0.0 \
  --port 8000 \
  --proxy-headers \
  --forwarded-allow-ips="${FORWARDED_ALLOW_IPS:-10.204.1.2}" \
  --log-level info
