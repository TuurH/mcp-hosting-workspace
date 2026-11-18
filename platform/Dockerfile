# One image for the platform services: files, deployer, admin, and the helper
# containers used by backups and gaming mode.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONPATH=/opt/platform

# UID/GID 1000 matches the workspace's "ai" user so workspace files are readable.
# Mount points are created here, owned by 1000, so a new named volume mounted
# there inherits that owner (the volume-init service fixes volumes another
# container created first).
RUN apt-get update && apt-get upgrade -y \
    && apt-get install -y --no-install-recommends iptables \
    && rm -rf /var/lib/apt/lists/*

RUN groupadd --gid 1000 app \
    && useradd --uid 1000 --gid 1000 --no-create-home --shell /usr/sbin/nologin app \
    && mkdir -p /data /srv/sites /pomerium /control /logs /run/hertogs /workspace /scratch \
    && chown 1000:1000 /data /srv/sites /pomerium /control /run/hertogs /workspace /scratch

COPY requirements.txt /tmp/requirements.txt
RUN python -m pip install --no-cache-dir --upgrade 'pip>=26.2,<27' 'setuptools>=84.1,<85' \
    && pip install --no-cache-dir -r /tmp/requirements.txt && rm /tmp/requirements.txt

COPY hertogs_platform /opt/platform/hertogs_platform
# Helper scripts used by scripts/backup.ps1 and restore.ps1 (called with plain arguments).
COPY --chmod=0755 bin /opt/platform/bin
WORKDIR /opt/platform
USER 1000:1000
