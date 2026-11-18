FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends git libgl1 libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*
# The release is pinned. Build logs/inventory record its commit and resolved wheels.
# For repeatable rebuilds, replace the base tag with its digest and use a reviewed
# constraints lock generated from the successful build's resolved-dependencies.txt.
RUN git clone --depth 1 --branch v0.39.0 https://github.com/Comfy-Org/ComfyUI.git /opt/ComfyUI
RUN pip install --no-cache-dir torch torchvision torchaudio --index-url https://download.pytorch.org/whl/cu130 \
    && pip install --no-cache-dir -r /opt/ComfyUI/requirements.txt \
    && pip freeze --all > /opt/resolved-dependencies.txt \
    && git -C /opt/ComfyUI rev-parse HEAD > /opt/comfyui-commit.txt
COPY . /app
RUN mkdir -p /data && chown 1000:1000 /data
WORKDIR /app
USER 1000:1000
