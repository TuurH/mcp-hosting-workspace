# Installation and feature acceptance checklist

Complete [SETUP](SETUP.md) first. Run commands from the repository folder in PowerShell with `Set-ExecutionPolicy -Scope Process Bypass`. Record the tested commit, date, host/driver versions and any failures. Check only features you actually enabled; mark others Not tested rather than Passed. Use disposable test apps/data for destructive checks.

## Automated installation checks

```powershell
docker compose ps
.\scripts\test-local.ps1
$LASTEXITCODE
```

| Exit | Meaning | Next step |
| --- | --- | --- |
| 0 | Automated checks passed | Continue signed-in and feature acceptance below |
| 1 | At least one check failed | Correct failures and rerun |
| 2 | Checks were skipped/incomplete | Resolve reachability/challenges/configuration and rerun |

The script checks infrastructure health, deployer Docker access, workspace GPU, routing and anonymous HTTP boundaries. Run the GPU check with gaming off and workspace GPU allowed. It does not install optional application images or certify signed-in people, raw listener reachability, ComfyUI generation or restoration. Infrastructure should publish no host ports; explicitly approved app listeners are listed separately and must match owner grants.

- [ ] `.env` matches the real owner/MCP/admin audiences; keep its original encryption key separately protected.
- [ ] All expected long-running Compose services are healthy/running; successful `volume-init` completion is expected. No unexpected localhost diagnostics override remains.
- [ ] Tunnel routes are correct, wildcard last; downloads use `files:8080`, never private `8081`.
- [ ] Protected responses have `CF-Cache-Status: DYNAMIC` or `BYPASS`; they are not served from cache. A WAF challenge is not proof of application authentication.
- [ ] Real calls work in every client you use after OAuth and metadata refresh.

## Signed-in access and routing

Use separate browser profiles for owner, invited Google user, uninvited Google user and anonymous visitor. Automated redirects alone cannot verify this table.

| Test website state | Owner | Invited | Uninvited | Anonymous |
| --- | --- | --- | --- | --- |
| Only you | Opens | Denied | Denied | Requires login |
| Shared | Opens | Opens | Denied | Requires login |
| Public | Opens | Opens | Opens | Opens |
| Started preview | Opens | Denied | Denied | Requires login |

- [ ] Owner can open admin; other accounts/anonymous cannot. Missing admin/MCP configuration stays locked.
- [ ] Create a static test app, check every audience state, including a group membership change. Public confirmation requires the exact hostname.
- [ ] Remove an invited person and verify the next HTTP request is denied. For runtime WebSockets, take offline to close existing connections when immediate revocation is required.
- [ ] Check applied routing status after each change. An unknown app hostname returns 404; anonymous `canary` requires login.
- [ ] Activate Emergency access lock: apps become owner-only; raw listeners stop; saved audiences/protection remain. Restore saved visibility, verify audiences again and explicitly restart stopped development.

## Workspace, files and credentials

- [ ] Run a command, write/read a small file and start a background job. Poll status/logs. Verify failed/timeout/cancelled results and bounded/truncated output are understandable.
- [ ] Export a small workspace file, open its download link and verify expiry. Files root returns Not found; private mint endpoints are unreachable publicly.
- [ ] Browse Files, create/rename a folder, view text/image, download, delete to trash and permanently empty the disposable trash item.
- [ ] Upload a file larger than one chunk. Pause, select that same file and resume. Check cancellation, duplicate-name refusal and explicit overwrite. Oversize files should explain the configured limit.
- [ ] Enter a disposable Workspace token through the owner form. A new command can use it; the token value is absent from management responses. Rotate/delete it, verify new processes and stop old jobs to remove their old copy.
- [ ] Edit Workspace CPU/memory/storage budget; compare desired/effective limits and storage measurements. Explain partial scans. A storage budget does not reserve/limit the Docker volume physically.
- [ ] Navigation warns about unfinished edits. Operation polling leaves edited fields intact. Check keyboard focus and usable mobile controls.

## Runtime lifecycle, limits and release

- [ ] Submit a disposable container app with web, worker, task and schedule components using an owner-installed image. Review exact source and manifest; verify a required base image failure is actionable.
- [ ] Exercise Pending release checklist: missing build/capability/credential values/grants point to the right sections and prevent premature release.
- [ ] Start a preview: only web runs, with empty separate data and no credentials/network/GPU/background effects. Verify its restricted behavior; do not use it to certify a GPU/data-dependent production app.
- [ ] Release and verify live component readiness, worker operation, once-per-revision task and non-overlapping interval schedule. A failed readiness check leaves an actionable failed operation.
- [ ] Edit per-component CPU/memory limits, verify actual Docker enforcement and compare multi-component total use. Observe private data/storage budgets.
- [ ] Take offline; release/rollback while offline must preserve offline state. Bring online, then restart and check recovery after a deployer restart. Code rollback must retain stored app data.
- [ ] Store app credentials, grant only the exact revision and verify runtime access. New revision grants are empty. Preview gets none. Rotate with Apply credentials and restart; revoke a live grant and confirm shutdown before revocation.
- [ ] Set environment label independently of visibility/protection. Test each intended AI permission, then revoke it and verify the operation is refused. Broader capabilities/new images/credentials still require review.
- [ ] Inspect static masked/stats logs and runtime output separately; test temporary full log access/expiry and visibility-history boundaries. Partial logs/inventories must not claim completeness.
- [ ] Delete only the disposable app, confirming source/containers removal and retained app data.

## Builds and development packages

- [ ] Pull the pinned worker, submit a small Dockerfile, review/build it with narrowly selected network bounds and inspect logs/dependencies. Try a failing build; it remains visibly failed and does not release code.
- [ ] Confirm a successful image is immutable, then release separately. Host rootless restrictions should explain failure without automatically weakening host policy.
- [ ] Grant development from the reviewed base. Sync source, install a small package through approved HTTPS sources, run a command and inspect before/after inventories and dependency changes/errors.
- [ ] Stop/start development without losing files. Snapshot into a pending candidate; check installed package/source preservation and unchanged live production. Review production capabilities/keys separately before release.
- [ ] Confirm changing development base/access warns that unsnapshotted changes are lost, including the same-base replacement. Revoke package permission and verify development commands are refused.
- [ ] Snapshot valuable mutable work before backup/migration; it must not be assumed present in ordinary backups.

## Network, service ports, GPU and ComfyUI

- [ ] Default app cannot reach unapproved public/LAN/platform/metadata addresses. Test reviewed CONNECT access separately from direct networking.
- [ ] Grant a selected TCP/UDP destination/port and DNS, test allowed traffic and denied ports/destinations. LAN needs both range and protocol grant; platform/Docker/metadata routes stay blocked. IPv6 external traffic stays denied.
- [ ] For every enabled incoming listener, test its real Docker/Windows interface, protocol and allowed/disallowed sources. Local must be loopback; private/VPN require actual interfaces and source ranges. Public raw ports bypass website Google login and need service authentication/network exposure. Ordinary HTTP tunnel does not make UDP reachable.
- [ ] Revoke capabilities and verify running namespaces/listeners stop before saving; bring online only after reviewing the remaining bounds.
- [ ] Test an approved GPU app on the actual NVIDIA host. Check driver/CUDA compatibility and shared VRAM behavior; CPU CI/build success is insufficient.
- [ ] For ComfyUI, follow the optional example: build image, grant GPU/network, release/bring online, populate verified models, render a small image and check authenticated WebSocket progress, persistent outputs and restart. An empty model list cannot generate an image.

## Gaming, exports and recovery

- [ ] Start a real workspace GPU job and approved GPU app/development session. Turn gaming on; check CPU/memory limits, stopped/held GPU apps and workspace GPU verification/VRAM release. Frozen processes can retain memory; investigate partial/errors and kill holders if needed.
- [ ] AI cannot turn gaming off. Owner turns it off: online live GPU apps resume, intentionally offline apps/stopped development stay stopped.
- [ ] If using the Windows game trigger, verify a listed executable enables gaming. Closing the game does not automatically disable it.
- [ ] Export selected disposable `/data` paths with a bounded size. Confirm stop/copy/resume, unique inbox destination, SHA256 provenance and link. Oversize/symlink exports fail clearly; review bounded partial output.
- [ ] Restart during disposable queued work: operations/jobs report interrupted, never falsely completed. Review application state before retrying; interrupted exports remain offline for review.

## Backup and restore drill

- [ ] Make an application-consistent backup: stop database apps or coordinate their own snapshots; snapshot development first. Verify `last-backup.json`, manifest hashes, retained images and chosen workspace folders. Investigate all warnings.
- [ ] Verify scheduled task while Windows is signed in and Docker running; simulate an unreachable backup destination and check local error status/logging.
- [ ] On a disposable installation with matching `.env`, restore a complete local backup. Verify files/app data/images/credential decryptability and rerun audience/runtime checks. Confirm workspace overlay semantics and old jobs' interruption.
- [ ] A deliberately corrupted archive is refused before replacement. If a restore fails later, the stack remains stopped. Do not test destructive restoration against the only production copy.

## Troubleshooting

| Symptom | Check and next action |
| --- | --- |
| Admin/MCP 503 | Required `.env` authentication values/AUD/owner; recreate changed service after correcting |
| 403 or wrong Google user | Actual signed-in identity, Access/Pomerium audience policy and separate browser profile |
| Private app suddenly anonymous | Cloudflare cache bypass, wildcard ordering, explicit DNS and applied routing; take offline while correcting |
| Release unavailable | Follow pending checklist, then reload after build/grant changes; inspect failed/interrupted Operations |
| Runtime route unavailable | Component declared port/readiness, desired offline state, held gaming state and component output |
| Dockerfile build fails | Worker provisioned, rootless namespace prerequisite, DNS/ports, build logs and resource/disk capacity |
| Package download fails | Approved exact HTTPS destinations (including download hosts), proxy use or explicit DNS/direct-network grant |
| Raw port unreachable | Docker host interface, Windows forwarding/firewall, source ranges/router and protocol; HTTP tunnel is separate |
| GPU not released | Real GPU report, partial checks, frozen holders, protected server process and application held states |
| Upload cannot resume | Same browser/file, remaining partial upload and free space; cancel/restart if original metadata is gone |
| Backup warning | Snapshot consistency, changing files, missing folders/images and NAS capacity/reachability; run restore drill |

For service logs: `docker compose logs --tail 100 <service>`. Use `docker compose ps` first to identify the failing component. Keep credentials out of copied diagnostic output.
