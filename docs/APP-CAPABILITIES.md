# Application capabilities and development

Environment labels (`development`, `staging`, `production`), visibility, lifecycle
and AI permissions are independent. The label does not create three deployments;
each app has one live revision, a separate optional mutable development container,
and an owner-only isolated preview. Create separate apps for separate live environments.
Existing protection remains the migration default. Saving explicit permissions lets
the owner independently grant package development, platform web tests, lifecycle
restart, bounded automatic code deployment and deletion. Labels grant no access.
Credential grants remain exact-version grants. No arbitrary environment-variable map
or UI was added (audit item 46); use command arguments/config files for ordinary settings.

## Network and GPU grants

`capabilities` in `app.json` requests platform capabilities:

```json
{
  "gpu": true,
  "network": {
    "dns": true,
    "tcp": [{"destination": "public", "ports": [443]}],
    "udp": [{"destination": "public", "ports": []}],
    "lan": ["192.168.1.0/24"],
    "ingress": [{"protocol": "udp", "port": 5201, "host_port": 15201,
                 "exposure": "local", "bind_ip": "127.0.0.1", "sources": []}]
  }
}
```

An empty outbound port list means all ports, while omitting a protocol denies it.
Destinations are `public` or an IPv4 address/CIDR. Private/CGNAT destinations require
a separate LAN-range grant plus a matching TCP/UDP rule. LAN declarations alone grant
no traffic. Platform and Docker bridge networks remain blocked, including within a
LAN range. The IPv4 firewall denies nonpublic ranges before public-internet rules.
IPv6 external traffic remains blocked. DNS uses a platform relay to a public resolver;
Docker's embedded resolver is redirected/blocked so it cannot expose container names.
Public DNS access itself is a meaningful continuing capability.

The owner reviews and grants the exact request on the app page, or approves an
`app_request(..., "capabilities", ...)` request. A changed grant stops the app and
removes its production namespaces/components before saving. Bring it online after
reviewing the new bounds. Existing CONNECT `outbound` host:port rules continue to work
without granting direct network access; public-address/rebinding validation remains.
Previews get no outbound access, service ports, credentials or GPU.

Published TCP/UDP ports are bound on the Docker host namespace. `local` requires
127.0.0.1. `private` and `vpn` require an explicit host interface IP and allowed source
ranges. `public` is an explicit unauthenticated service exposure. Ports are 1024–65535.
The interface must exist on the actual Docker host; Docker Desktop host forwarding
may require additional Windows interface/port-forwarding configuration. A VPN listener
does not create a VPN. Public reachability requires suitable firewall/router rules or
a separate TCP/UDP tunnel service. Cloudflare's ordinary HTTP wildcard and Pomerium
website login do not route or authenticate these raw ports. The emergency access lock
stops apps and development sessions with raw incoming service ports and retains live
apps' desired online state;
restore access, then restart/reconcile them. Existing connections stop with the namespace.

GPU grants produce platform-controlled NVIDIA DeviceRequests; the AI never supplies
device paths or Docker HostConfig. GPU access is shared with the main workspace and
other approved apps, with no guaranteed VRAM quota. Gaming mode stops GPU app containers
and GPU development containers, blocks their restart while active, and retries CPU/GPU
policy enforcement. Online live GPU apps resume during reconciliation after gaming;
offline apps and stopped development sessions stay stopped until explicitly started.
Routes/status distinguish held GPU apps from healthy online service. Real driver/CUDA
compatibility must be checked on the installed Windows/NVIDIA host.

## Reviewed Dockerfile images

Set `"dockerfile": "Dockerfile"` instead of `image`, submit the context with `app_deploy`,
and review its source on the app page. The owner starts the image build, selecting this
job's DNS/TCP/UDP bounds. Builds cannot expose ports or access LAN ranges. Custom frontend
directives, secret/SSH mounts and privileged build entitlements are disabled. No Docker
socket, host mounts, production volumes or production credentials enter the worker.
Image-build commands belong in the Dockerfile, rather than the legacy `build` field.

Provision the pinned trusted worker image:

```sh
docker compose --profile tools pull builder-image
```

The worker uses rootless BuildKit v0.35.0 with a native snapshotter and process sandbox.
When build DNS is approved, the platform explicitly configures BuildKit's `RUN` resolver
with that job's DNS relay address. BuildKit otherwise filters Docker's loopback resolver
and falls back to public DNS servers, which the job firewall blocks. This configuration
is generated outside the submitted source context; public UDP access is not required.
RootlessKit requires host support for unprivileged user namespaces and subuid/subgid
mapping. The worker runs as UID 1000; it needs the documented container seccomp/AppArmor
and masked-proc exceptions for nested rootless namespaces, rather than privileged mode.
No `no-new-privileges` is set on this trusted worker because `newuidmap/newgidmap` need
their setuid transition. It still has no host namespace, host PID list, devices or socket.
Hosts restricting unprivileged user namespaces must be configured by their administrator;
the platform reports a failed build and does not weaken host policy automatically.
See [BuildKit rootless requirements](https://github.com/moby/buildkit/blob/master/docs/rootless.md).

CPU/memory/PID limits apply to each job. Builds have a deadline (setting, 60 minutes by
default) and a bounded image archive. One build runs at a time. Each worker container is
removed afterward; the build cache is kept per app in its own Docker volume, trimmed after
every build to the per-app limit, and the least recently used caches are removed when all
caches together pass their limit or the Docker disk runs low. A cache holds only that app's
build steps, never another app's. The owner can ignore or delete it on the app page. Logs and resolved dependency
inventory stay on the app page. The output image is identified by immutable SHA256 and
backed up alongside its reviewed source. Tags from an exported image cannot overwrite
platform image tags. Build completion is separate from release approval. COPY your app
into `/app` (or use another declared command path); Dockerfile releases use image source
directly with a read-only root, rather than overlaying an empty source volume.

Pin base digests, package constraints and Git commits for repeatable rebuilds. Dependency
inventories are observations from available package tools, not a complete attestation
for opaque installers. Failed/unavailable collection is reported; inspect lockfiles,
commands and build logs too. Building does not prove GPU runtime compatibility.

## Per-app development and package transparency

Request `development` with a current base `version` and separate development capabilities,
or grant it on the owner page. This enables the package-development permission and
provisions an isolated mutable root/source filesystem. It does not mount live app data,
app keys, workspace tokens, the main workspace, host paths or the Docker socket.
Root commands can install system packages inside this container. Development capabilities
can be broader than production without becoming production grants.
Exact HTTPS CONNECT destinations can be listed separately as `outbound` host:port
entries (for example `pypi.org:443` and `files.pythonhosted.org:443`). The platform
injects its generated proxy address; no general environment-variable editor is needed.

Use `app_development(name, action=...)`:

| Action | Result |
| --- | --- |
| `start` / `stop` | Start or stop the separate development session |
| `sync`, `folder=...` | Overlay workspace source into `/app`; delete obsolete files with a command |
| `command`, `command=[...]`, `root=true/false` | Run bounded development commands; record output and before/after package inventories |
| `snapshot` | Stop writes and freeze the filesystem/dependencies into an immutable pending image |

Owners can use **Start development**, **Stop development** and **Create release snapshot** on the app page without another AI request. Changing base/access is a separate, destructive replacement form with confirmation. See [OWNER-GUIDE](OWNER-GUIDE.md).

`app_operation` polls queued work. Every package command records system packages,
Python/npm dependencies, Git commits, collection errors and an explicit dependency diff.
Resolved transitive packages are included when their package tools expose them. Ordinary
package additions within the approved development/network scope require no new owner
prompt. Broader network/GPU access needs a new grant. Command output and inventories
are bounded. Revoking package permission stops development; replacing a development grant (even for the same base) resets
the mutable container, so freeze work first. Development base revisions remain retained.
Mutable development layers are temporary and are not included in normal backups;
snapshot work that must survive a reset or migration.

Snapshots retain actual installed image layers and source and record an inventory with
the release candidate. Development source `app.json` supplies the requested runtime
components, resources and production capabilities; development network grants are not
copied into production. Live production stays untouched until release. Owner-enabled
`deploy` may release source/snapshots within approved capabilities and the approved image
base, with no new credentials; changed base images, Dockerfiles and credential grants
still need review. A general development command inherently can run code and modify
packages; `run_tests` controls the separate platform web-test action, not shell semantics.

## Approved app-data exports

Request `data_export` with paths relative to `/data`, `max_bytes`, and
`stop_for_consistency: true`, or choose these on the owner page. Requests bind the live
revision and current permissions. The owner reviews which data leaves the app, including
the possibility of personal data or app-written secrets. Exporting data makes the copy
readable by the main workspace AI; it does not grant continuing volume access.

The platform stops the live app, then a trusted helper mounts only its data volume
read-only and the destination workspace volume. There are no credential mounts or
network access. Directory-relative file descriptors and no-follow opens reject traversal,
symlink substitution, hard links, FIFOs/devices and oversized copies. Each export has
at most 10,000 files, the approved byte limit and an owner quota. It writes to a unique
`/workspace/inbox/<app>-<operation>` folder with SHA256 provenance. A failed export can
leave a bounded partial folder; status reports failure. Cleanup/review is explicit.

If the app was online and its version/lifecycle has not changed during the copy, the
platform resumes it afterward. An interrupted export stays visibly interrupted and
the app remains offline for review. `app_operation` reports the destination and counts;
the owner page links to the inbox. Database copies are consistent because the app is
stopped; independent external writers still require application-specific coordination.

## ComfyUI

[The ComfyUI example](../examples/comfyui/README.md) supplies a reviewed Dockerfile,
GPU web manifest, persistent model/input/output/user directories, bounded pinned model
downloads and WebSocket routing. Web startup grace can be set to 10–600 seconds per
web component for slower GPU initialization. This uses the general runtime. Home
Assistant-specific integrations and the general environment-variable UI remain out of scope.

Runtime manifests may set a per-component `pids` limit from 32 through 1024; the default is 128. Docker counts both processes and threads. The limit is part of the submitted manifest reviewed for release. Older manifests continue to use 128.
