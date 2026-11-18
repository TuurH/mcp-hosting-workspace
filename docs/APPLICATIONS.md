# Application and workspace controls

For step-by-step owner actions, see [OWNER-GUIDE](OWNER-GUIDE.md); for installed-host checks, see [GO-LIVE](GO-LIVE.md).

The owner dashboard separates visibility, production protection, lifecycle, releases,
resource limits and credentials. Existing Shared and Public apps gain production
protection during migration. Making an app owner-only does not remove that protection.
Legacy protection provides the default AI policy. Explicit owner permissions now independently control development/package installation, platform tests, lifecycle changes, bounded deployment and deletion. Environment labels do not depend on protection. See [APP-CAPABILITIES.md](APP-CAPABILITIES.md) for current capability workflows. Static
apps may use an explicit owner-enabled auto-release policy outside Public visibility.
The emergency access lock overlays owner-only access without deleting sharing lists,
production protection or versions. Restoring saved visibility runs the route checks.

## Main workspace

Open **Workspace** in the admin navigation. CPU cores and memory are live Docker
limits. Memory uses GiB and the swap limit equals memory. Limits are saved and
reapplied after container recreation. Gaming mode temporarily uses its own limits;
edits made during gaming update the normal limits restored afterward. App components also use the configured gaming CPU cap, including newly started revisions; turning gaming off restores each component's normal CPU limit. Lowering a
memory limit below current use is refused. Storage usage includes workspace files,
caches, uploads and trash; a bounded scan is explicitly marked partial when needed.
Storage is a budget on a shared Docker disk, not an enforced volume quota. Docker
Desktop's disk size is managed separately in Docker Desktop settings.

Enter service tokens on **Workspace → Tokens**, never in an MCP management request.
Values are encrypted in the platform database. A separate volume delivers a mode-0600
file to the workspace read-only; the admin and files services do not mount it.
New commands and jobs load those environment variables. Token values are never
returned by a management API or included in job specifications or audit entries.
Workspace commands (and therefore the AI operating the workspace) can read and use
these credentials. Rotation affects new processes; stop existing jobs to revoke their
old copy. Reserved configuration names cannot be changed through the token form.
Encrypted credentials are included in platform database backups. Keep the original
`DEPLOYER_SECRETS_KEY` in the installation's separately protected `.env` backup.

## Container apps

Place an `app.json` in `/workspace/apps/<name>` and call `app_deploy(name)`. Static
output continues to work when no manifest is present. Example:

```json
{
  "image": "python:3.12-slim",
  "memory_mb": 256,
  "cpus": 0.5,
  "storage_gb": 1,
  "components": [
    {"name": "web", "kind": "web", "command": ["python", "app.py"], "port": 8080, "data": true},
    {"name": "worker", "kind": "worker", "command": ["python", "worker.py"], "data": true},
    {"name": "refresh", "kind": "schedule", "command": ["python", "refresh.py"], "interval_seconds": 3600}
  ],
  "secrets": ["SERVICE_TOKEN"],
  "outbound": ["api.example.com:443"]
}
```

The owner installs the base image on the Docker host first (`docker pull` or `docker
load`). Submission resolves its immutable image ID and includes it in the revision.
Images declaring implicit volumes are refused. AI input cannot supply privileged
flags, host mounts, devices, socket mounts, arbitrary Docker HostConfig, external
networks or arbitrary host ports. Network/GPU requests use validated owner grants; bounded automatic updates can be enabled explicitly. It appears on
the app's page with the complete normalized manifest and source diff.

Optional `build` is an argument list executed as UID 1000 in a disposable isolated
container. It has no network, credentials or production data and writes only its
private source volume. Put dependencies in the locally installed base image or vendor
them in source. Container apps use that immutable source volume read-only, a read-only
root filesystem, dropped capabilities, no new privileges, bounded temporary storage,
PID limits, bounded Docker logs and enforced CPU/memory limits per component.

One web component per app is supported. Workers run continuously; tasks run once per
revision; schedules run at fixed intervals of at least 60 seconds without overlap.
Schedules are reconciled once per minute and may start late. A persisted task-start
record prevents silent replay after a restore; interrupted tasks require explicit
owner action/a new revision. Container recreation and stopped worker recovery are
handled by the platform, after checking the runtime firewall. Releases serialize
workers to avoid old and new background versions running simultaneously; a short
service interruption is possible. Database migrations and data rollback are never
automatic: rolling code back does not roll stored data back.

App `/data` is private to the app, shared by its opted-in components and retained after
app deletion. Docker volume usage is measured in the background and shown with the time of
measurement. The newest ten revisions keep their source (setting `kept_revisions`); source and
preview volumes, networks and images of revisions outside live/pending/previous are removed. It has a storage budget rather than a hard Docker Desktop quota. A web
preview must be started by the owner; it gets a separate empty data volume and no
credentials, outbound access, workers, schedules or one-off task effects.

App credential names in a manifest are requests, not authorization. The owner enters
values on the app page and grants named credentials to the exact pending revision.
No grant is inherited by a new revision. Granted code can read its credentials. Live
credentials are applied when the version starts; rotation/revocation requires stopping
and starting the app. Workspace tokens are separate and never injected into apps.

Backend source diffs include Python, shell, TypeScript, SQL and other common source files.
The owner can browse the exact submitted revision, with pagination for larger files;
truncated diffs are explicitly marked. Current credential grants are shown for pending
and live revisions. Changing a live grant takes the app offline before revoking access.
Use **Apply credentials and restart** after rotating a value; restart failure leaves
it offline. The operation display updates without reloading unfinished owner forms.

Runtime releases and previews return a persisted operation ID. The app page and
`app_status` show queued, running, succeeded, failed or interrupted results. A process
restart marks in-flight operations interrupted; it does not claim they completed.
Recovery stops pending/untracked production containers before resuming the live version.
Released previews stay stopped and never enter production worker recovery. Releasing
or rolling back an offline app preserves its offline state; preparation/building does
not start its production components. Private runtime page tests use a bounded loopback
request in the trusted gateway and explicitly disclose authentication bypass. Raw AI
runtime logs are restricted to the current visibility window unless the owner explicitly
grants temporary full historical access.

## Data changes and going back

Going back to an earlier revision changes code only. Stored data is never rolled back: the old
code runs on the data the newer version left. The previous revision keeps its image and is
swapped in within seconds. An older kept revision is made pending again, rebuilt from its kept
source when it has a Dockerfile, and released; that takes minutes, and dependencies without a
lockfile can come out newer than the first time. A revision made from a development snapshot
cannot be rebuilt once its image is removed.

`app_deploy(name, data_change=...)` states what a version does to stored data: `none`,
`compatible` (the previous version still works with the data) or `irreversible`. The owner sees
it next to the pending version; it is the AI's statement, not something the platform checks.
Prefer compatible changes: add columns or files instead of renaming or dropping them, and make
an irreversible change in a version of its own. The AI can ask to go back with `app_request`
kind `rollback` (`{"version": "previous"}` or a revision id from `revisions` in `app_status`);
the owner applies it on the app page.

## Dockerfiles that rebuild quickly

Every app has its own build cache, kept between builds and trimmed to a size limit. A step is
reused only when nothing before it changed, so the order of the Dockerfile decides how long a
rebuild takes. Copy the dependency list first, install, then copy the rest:

```dockerfile
FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
```

With `COPY . .` before the install, every source change reinstalls everything; the app page
warns about that. Base image tags are checked at the registry on every build, so a cached
build still picks up a newer base image. Keep models and datasets out of the source: put them
in the app's `/data`.

## Actions the AI may call

A running app is not reachable for the AI except through the page tests. An app can open a
narrow, reviewed door by declaring actions in `app.json`:

```json
"actions": [
  {"name": "list_tasks", "class": "read", "method": "GET", "path": "/api/projects/{project}/tasks",
   "description": "List the open tasks of one project.",
   "input": {"project": {"type": "integer", "required": true}, "filter": {"type": "string"}}},
  {"name": "add_task", "class": "write", "method": "POST", "path": "/api/projects/{project}/tasks",
   "input": {"project": {"type": "integer", "required": true}, "title": {"type": "string", "required": true}}},
  {"name": "delete_task", "class": "destructive", "method": "DELETE", "path": "/api/tasks/{task}",
   "input": {"task": {"type": "integer", "required": true}}}
]
```

Each action is one fixed method and path of the app's web component. `{field}` in the path is
filled from the input; the other input fields go in the query (GET, DELETE) or a JSON body.
Input types are string, integer, number and boolean. `read` actions use GET; DELETE must be
`destructive`. Up to 50 actions per app. The owner sees the list on the app page and decides per
app which classes the AI may call (three AI permissions, all off at first). After a version
changes the declared actions, none can be called until the owner releases a version personally.

The AI calls them with `app_call_read(name, action, arguments)` and
`app_call_write(name, action, arguments)`; `app_status` lists the actions with `allowed`. The
request is made inside the app's own network namespace with the header `x-hertogs-caller: ai`,
does not follow redirects, and returns the status and at most 64 KB of the body. The audit log
records each call without its content. The app itself must check that the request does what the
action name says: the platform limits which requests can be made, not what the app does with them.

## Several chats in one workspace

Claude, ChatGPT and Codex can use the workspace at the same time and none sees the others'
conversation. `workspace_activity` shows every chat what runs now: commands, background jobs with
their label and who started them (the client name the calling program reports), jobs finished in
the last hour, platform operations and recent deploys, and the GPU. `workspace_info` reports GPU
memory, load and running apps with GPU access. The app list shows GPU access, not measured
per-app VRAM allocation; it is unavailable when the deployer cannot be reached.

`workspace_job_start(gpu=true)` puts GPU work in a queue: jobs start in the order they were asked
for, one at a time, and not during gaming mode. Two GPU jobs run together only when both state
`gpu_memory_mb` and they fit in the free GPU memory with a 512 MB margin. Admission also reserves
the declared memory of jobs already admitted, so a job that has not allocated its VRAM yet still
counts. This conservative accounting can delay a job even when it would fit. A waiting job has status
`waiting_for_gpu` with the reason. The queue is cooperative: a command that uses the GPU without
the flag is not queued and not listed.

Tools that start something accept an optional `request_id`. Repeating a call with the same value
within a day, for example after a cut connection, returns the first answer and starts nothing new.
Download links keep their original expiry; use a fresh request ID for a new link after expiry.

## Network and GPU boundaries

Runtime apps share platform-owned guarded namespaces. The default remains deny-by-default
with reviewed CONNECT destinations. Owner grants now additionally support direct IPv4
TCP/UDP, platform DNS, bounded LAN exceptions, explicit published service ports and NVIDIA
GPU devices. Changed grants stop production namespaces before revocation. Raw service ports
have explicit exposure and do not inherit HTTP login. Gaming holds GPU apps and preserves
online intent. Dockerfile builds, per-app package development and approved data exports are
documented in [APP-CAPABILITIES.md](APP-CAPABILITIES.md). Home Assistant-specific integrations
remain separate. The main workspace keeps its configured internet/LAN/GPU access.

The workspace GPU flag is a cooperative policy because the workspace has sudo. Gaming
mode additionally stops GPU work from outside the workspace and retries enforcement.
The UI distinguishes the policy flag from process checks and a frozen workspace. Hard
GPU device removal requires recreating the workspace without its GPU reservation; a
policy flag alone is never presented as device revocation.

## Routing, recovery and validation

Routing status distinguishes requested state, the applied configuration, held routes
and the last anonymous checks. A successful anonymous login redirect does not prove
that a chosen signed-in person is allowed or excluded. Complete the owner/invited/
uninvited-user browser checks in GO-LIVE before relying on sharing. Direct-upstream
`app_test` reads explicitly disclose that authentication was bypassed.

Volume backups now include app source, persistent data and immutable runtime images.
New backup manifests include SHA256 hashes, checked before restoration, while older
size-only manifests remain readable. Back up database applications only after an
application-consistent snapshot or stop them; copying a running database volume alone
is not a consistency guarantee. A restore verifies hashes before replacing volumes
and keeps the stack stopped on failure. Restore stops Compose services and removes all dynamically managed app/helper containers before replacing any volume; volumes are retained and approved apps are recreated from restored state. The encryption key and setup `.env` are still
separate owner-managed material. Runtime image backups can be large.

CI tests real Docker containers, isolated previews, owner credential grants, CPU and
memory enforcement, source mount restrictions, host/metadata-address denial, component
lifecycles, static routing, masking and administrative CSRF/authentication. Local unit
tests do not substitute for signed-in audience checks on the installed domain.
