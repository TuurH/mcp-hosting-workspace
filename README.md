# MCP Hosting Workspace

A self-hosted platform for running AI jobs and hosting the apps they build. Claude and ChatGPT connect to a persistent GPU workspace over authenticated MCP. You review code, grant capabilities, and manage app releases from the admin website.

Built and tested on Windows with WSL 2, Docker Desktop, and a GeForce RTX 3090 (24 GB). See [host requirements and adaptations](docs/SETUP.md#1-host-requirements) for other GPUs, native Linux, and CPU-only hosts.

## Start here

| Goal | Guide |
| --- | --- |
| Install or upgrade | [Setup](docs/SETUP.md) |
| Verify the actual installation | [Go-live checklist](docs/GO-LIVE.md) |
| Use every owner control | [Owner guide](docs/OWNER-GUIDE.md) |
| Write an application manifest | [Applications](docs/APPLICATIONS.md) |
| Build images, develop packages, grant networking/GPU, export data | [Capabilities](docs/APP-CAPABILITIES.md) |
| Understand boundaries and recovery | [Design](docs/DESIGN.md) |
| Host ComfyUI | [Optional ComfyUI example](examples/comfyui/README.md) |
| Connect Claude, Claude Code, or ChatGPT | [AI client setup](docs/SETUP.md#3-connect-the-ai-clients) |

Run PowerShell commands from the repository folder. Complete the Cloudflare/Google prerequisites in Setup before expecting authenticated pages to work:

```powershell
Set-ExecutionPolicy -Scope Process Bypass
.\scripts\setup.ps1
.\scripts\test-local.ps1
```

An automated check returning success is only one part of acceptance. The go-live checklist also tests signed-in audiences, hardware, backups and complete owner workflows.

## What is available

- Persistent `/workspace`, command execution, background jobs, bounded output, file import/write/export, expiring download links and a browser file manager with resumable uploads.
- Main-workspace CPU, memory and storage-budget controls; owner-entered tokens delivered to new workspace commands.
- Static websites and managed container apps: one web component, workers, one-off tasks and fixed-interval schedules, with persistent private app data.
- Only you / Shared / Public visibility; people and groups; independent environment labels, production protection and explicit AI permissions.
- Exact source review, changes, isolated web previews, release prerequisites, persisted operation results, rollback and online/offline controls.
- Exact-revision app credential grants, reviewed rootless Dockerfile builds, isolated mutable package development, dependency-change records and immutable development snapshots.
- Owner-approved IPv4 TCP/UDP/DNS, LAN exceptions, service listeners and shared NVIDIA GPU access; gaming controls and app-data exports into the workspace inbox.
- Routing checks, bounded logs, temporary AI log grants, audit records, an emergency access lock, selective backups and verified restore tooling.

ComfyUI is available as an optional source template.

## Access and limits

Replace `example.com` with a domain you control. [Setup](docs/SETUP.md) covers the Cloudflare and Google configuration.

| Endpoint | Purpose | Access |
| --- | --- | --- |
| `https://mcp.example.com/mcp` | AI tools | Cloudflare Access + Managed OAuth; server verifies owner identity |
| `https://admin.example.com` | Owner controls and files | Separate owner-only Access application |
| `https://auth.example.com` | Application login | Google through Pomerium |
| `https://<app>.example.com` | Application | Selected website visibility |
| `https://next-<app>.example.com` | Started preview | Owner only; runtime preview has restricted capabilities |
| `https://files.example.com/<link>` | Exported download | Possession of expiring link |

The PC must be awake, online, signed in and running Docker Desktop. Main-workspace Python packages persist in `/workspace/.venv`; system packages installed into its mutable container need rebuilding into the image to survive recreation. Use the matching CUDA devel base (`CUDA_BASE_IMAGE`) when you need `nvcc`; see `.env.example`. The main workspace has sudo and configured internet/LAN access. It cannot directly control Docker; only the trusted deployer mounts the Docker socket. Owner-granted raw TCP/UDP listeners bypass website login and require their own network exposure and client authentication.

CPU and memory limits are enforced per container. Storage settings are budgets on a shared Docker disk, and GPU grants do not allocate a private GPU or VRAM quota. Code granted a credential can read it. Keep `.env`, especially `DEPLOYER_SECRETS_KEY`, separately protected: encrypted backup data cannot be recovered without the original key.

## Development checks

```sh
python -m pip install -r tests/requirements-test.txt -r platform/requirements.txt
python scripts/check-docs.py
python -m pytest -q
```

[CI](.github/workflows/ci.yml) additionally parses PowerShell and runs actual Docker, firewall, lifecycle and browser-layout checks. See the workflow for its prerequisites and commands. GPU/Windows/Cloudflare audience acceptance still runs on the installed host; CPU-only CI cannot certify it.

## License

This project is licensed under [Apache 2.0](LICENSE). The stack pulls separately licensed upstream images, including [Pomerium](docs/SETUP.md#app-proxy-and-licensing).
