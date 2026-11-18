ARG CUDA_BASE_IMAGE=nvidia/cuda:12.8.1-cudnn-runtime-ubuntu24.04
FROM ${CUDA_BASE_IMAGE}

ARG DEBIAN_FRONTEND=noninteractive
ARG AI_UID=1000
ARG AI_GID=1000

ENV LANG=C.UTF-8 \
    LC_ALL=C.UTF-8 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PATH=/opt/venv/bin:/home/ai/.local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin

RUN apt-get update && apt-get upgrade -y && apt-get install -y --no-install-recommends \
    sudo \
    python3 python3-venv python3-pip python-is-python3 pipx \
    build-essential gcc g++ gfortran make cmake ninja-build pkg-config \
    git git-lfs openssh-client rsync \
    curl wget aria2 ca-certificates gnupg \
    jq yq ripgrep fd-find bat tree \
    nano vim-tiny less tmux \
    unzip zip p7zip-full xz-utils \
    file libmagic1 \
    procps htop lsof strace gdb \
    iproute2 iputils-ping dnsutils netcat-openbsd \
    ffmpeg mediainfo imagemagick sox libsndfile1 \
    pandoc poppler-utils tesseract-ocr \
    sqlite3 postgresql-client redis-tools mariadb-client \
    libgl1 libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/* \
    && ln -sf /usr/bin/fdfind /usr/local/bin/fd \
    && ln -sf /usr/bin/batcat /usr/local/bin/bat \
    && git lfs install --system

# Ubuntu 24.04 ships a default "ubuntu" user at UID/GID 1000. Remove whoever
# occupies the requested IDs before creating the ai user.
RUN if getent passwd "${AI_UID}" >/dev/null; then \
        userdel --remove "$(getent passwd "${AI_UID}" | cut -d: -f1)"; \
    fi \
    && if getent group "${AI_GID}" >/dev/null; then \
        groupdel "$(getent group "${AI_GID}" | cut -d: -f1)"; \
    fi \
    && groupadd --gid "${AI_GID}" ai \
    && useradd --uid "${AI_UID}" --gid "${AI_GID}" --create-home --shell /bin/bash ai \
    && printf 'ai ALL=(ALL) NOPASSWD:ALL\n' >/etc/sudoers.d/ai \
    && chmod 0440 /etc/sudoers.d/ai \
    && mkdir -p /opt/app /opt/venv /workspace \
    && chown -R ai:ai /opt/app /opt/venv /workspace

COPY requirements-core.txt requirements-ai.txt /tmp/

USER ai
RUN python -m venv /opt/venv \
    && /opt/venv/bin/pip install --no-cache-dir --upgrade 'pip>=26.2,<27' 'setuptools>=78.1.1,<82' wheel 'uv>=0.13.4,<1' \
    && /opt/venv/bin/pip install --no-cache-dir -r /tmp/requirements-core.txt \
    && /opt/venv/bin/pip install --no-cache-dir \
       torch==2.11.0 torchvision==0.26.0 torchaudio==2.11.0 \
       --index-url https://download.pytorch.org/whl/cu128 \
    && /opt/venv/bin/pip install --no-cache-dir -r /tmp/requirements-ai.txt

USER root
COPY --chown=ai:ai app /opt/app/app
COPY --chown=ai:ai scripts /opt/app/scripts
COPY --chown=ai:ai entrypoint.sh /opt/app/entrypoint.sh
RUN sed -i 's/\r$//' /opt/app/entrypoint.sh \
    && chmod 0755 /opt/app/entrypoint.sh \
    && find /opt/app/scripts -type f -name '*.py' -exec chmod 0644 {} +

USER ai
WORKDIR /workspace
EXPOSE 8000

ENTRYPOINT ["/opt/app/entrypoint.sh"]
