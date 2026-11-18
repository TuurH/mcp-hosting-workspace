# Managed ComfyUI example

This is an optional template; it is not a preinstalled app and supplies no models.

Copy this folder into `apps/comfy` in the main workspace and submit with `app_deploy`.
This uses the general managed-app runtime, not a separate ComfyUI control plane.
It is an example to validate on your actual NVIDIA/Windows host, not a claim that
ComfyUI generation was tested on GitHub's CPU-only runners.

1. Increase **Settings → Application defaults and request limits → Maximum requested component memory (MiB)** to at least 8192. Choose app
   CPU/memory/storage budgets appropriate to available host memory and your models.
2. Provision the pinned rootless worker with `docker compose --profile tools pull builder-image`.
   Review rootless host requirements in [APP-CAPABILITIES](../../docs/APP-CAPABILITIES.md).
3. Review the Dockerfile and source, then approve its build with DNS and public TCP
   80/443 for Git, apt and Python wheels. Review the build log, Git commit and resolved
   dependency inventory. Add a constraints lock/base digest for repeatable rebuilds.
4. Grant the pending revision's GPU and public TCP 443/DNS requests. This takes the
   app offline before grants change. Release it and bring it online. Its web route
   supports authenticated WebSockets through Pomerium without publishing port 8188.
5. The CUDA 13.0 wheels require a compatible Windows NVIDIA driver. Check
   `torch.cuda.is_available()` in an approved development session before relying on
   the deployment. A build runs without a GPU; it cannot prove driver compatibility.
6. Models, inputs, outputs and user settings live under the private `/data` volume.
   Populate `models.json` with reviewed HTTPS URLs, relative model paths, maximum
   byte sizes and expected SHA256 hashes before submitting. The one-off task fetches
   these into `/data/models`; the initial list is empty. Refresh the ComfyUI model
   list after downloads finish. Gated downloads require separate credential grants
   and corresponding download code; this example fetches public files only.
7. Owner-approved exports can copy `output`, `input`, `user` or selected model files
   into the main workspace inbox. They stop the app for consistency and enforce the
   reviewed byte limit. Increase the export quota before exporting large models.

Use an app development grant to install/customize dependencies and custom nodes.
Package operations and Git revisions are recorded; freeze a development snapshot
to release the changed image. A node requiring writes under `/opt/ComfyUI` must be
configured for `/data` or prepared in development, because released root filesystems
are read-only. Runtime package installation belongs in development.

Gaming mode stops this GPU app. Its desired online state is retained and it resumes
after gaming ends. The GPU is shared, without a guaranteed per-app VRAM quota.
ComfyUI is owner-only initially; keep it private unless you intend others to access
the full interface. Model downloads, custom nodes and their licenses remain choices
you review in the source.

Official sources: [ComfyUI](https://github.com/Comfy-Org/ComfyUI),
[server routes/WebSockets](https://docs.comfy.org/development/comfyui-server/comms_routes),
[PyTorch installation](https://pytorch.org/get-started/locally/),
[Docker Desktop GPU support](https://docs.docker.com/desktop/features/gpu/).

Complete the GPU/ComfyUI checks in [GO-LIVE](../../docs/GO-LIVE.md#network-service-ports-gpu-and-comfyui) before sharing. The isolated web preview has no GPU/network or live models; use a reviewed test app/development session for GPU validation.
