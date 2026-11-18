# Owner guide

Open `https://admin.example.com` using the Google account in `PLATFORM_OWNER_EMAIL`. The navigation groups everyday work into Apps, People, Workspace, Files, GPU, Disk, Settings and Audit. Forms save separately; save one before switching pages or changing another policy. Every page shows a small popup (bottom right) when a build or release finishes, when the AI uploads a version or asks for something, and when an app passes its storage budget or is stopped for memory; **Settings → Popups** can also show them as Windows notifications. The dashboard card **Waiting on you** lists the apps with a version to release or an ask to decide. A navigation warning helps prevent losing typed form values; credentials are not saved in browser storage.

The app, overview, workspace, people, settings, GPU, audit and log pages update about every five seconds while visible, and immediately when you return to the tab. Newly pending releases, requests and operations appear without reloading. Updates preserve edited forms, focus and open details. A connection warning appears after repeated failures; the page retries automatically. File browsing, source/diff review and the Disk page remain on-demand views.

**Environment and AI permissions** and **Protection and log access** are separate cards with separate save buttons. Edited forms show an unsaved-changes marker. Submitting another form warns before discarding those edits. If an approval's review token changes or its form disappears on the server, the old edited form stays visible but cannot be submitted: copy your edits and reload to review the new state. Updates never silently apply edited permissions to a different revision.

## Workspace: resources and tokens

On **Workspace**, compare desired limits with effective CPU/memory and storage measurements. Save CPU cores, memory in GiB and a storage budget. CPU/memory are applied to the running container; gaming can temporarily lower effective limits. A budget does not reserve disk or impose a hard volume quota. Partial storage scans are labelled. Free Docker disk separately if capacity is low; trash, uploads, caches, app images and builds also use disk.

In **Tokens**, choose an uppercase name such as `SERVICE_API_TOKEN`, enter its value and save. Ask the AI to use the name in a new command; do not paste the value into a chat/MCP request. Only names/update times return to the management API. Commands can read and use their delivered tokens. Reserved platform/runtime names, including `HF_`, `PIP_`, `PYTHON` and `NVIDIA_` prefixes, are refused; use a service-specific token name and map it inside the intended command if a library requires a reserved name.

Rotation affects new commands/jobs. Stop existing jobs to discard their old token. Deletion removes delivery to new processes but cannot retract a value already read or copied. App credentials are separate; workspace tokens never automatically enter an app.

## Apps: access, releases and lifecycle

Ask the AI to create source under `/workspace/apps/<name>` and submit it using `app_deploy`. No `app.json` means static output; a manifest means a managed runtime. For an `image` manifest, the owner first installs the base image on the Docker host. Dockerfiles instead need an approved build. See [APPLICATIONS](APPLICATIONS.md).

Open an app and use its section links. The heading shows saved website visibility, environment and current lifecycle. **Only you** is an audience, not a development environment. **Shared** grants access to chosen people/groups; **Public** requires typing the app hostname. Add people first on **People**. Selecting a group grants access to its current members. Removing a person affects subsequent HTTP requests; take a runtime offline to close existing WebSockets immediately. Confirm routing has applied the change.

At **Pending release: next steps**, review changes and exact source. Complete any missing image build, network/GPU grant, stored credential values and exact-revision credential grants. The release button stays unavailable until these known prerequisites are ready. The deployer still checks current state and can reject a stale form; reload and review the current revision.

For runtimes, **Start isolated web preview** starts only the web component. Open the preview after its operation succeeds. It has empty separate data and no keys/network/GPU/background workers; some apps, including GPU apps, cannot be meaningfully tested there. Test those with a separate reviewed test app or approved development session, then check the live service as owner before sharing. Static previews need no separate container startup.

Choose **Release pending version**. While a release, preview, build or export runs, the top of the app page shows its phase, how long it has taken and its log, updated every two seconds; **Open the full log** shows the whole text. When it finishes the page reloads by itself, unless you are editing a form: then a popup offers **Reload**. An offline app remains offline: use **Bring online** separately. **Take offline** stops runtime components. **Discard** removes the pending candidate. A failed operation says in which phase it failed; read its log before retrying.

**Going back.** **Roll back** swaps in the previous revision within seconds: its image is kept. **Kept revisions** lists the newest ten revisions (the number is a setting); **Restore** on an older one makes it pending again, rebuilds its image from the kept source when needed, and releases it, which takes minutes. Going back changes code only. The app's data is never rolled back: the old code runs on the data the newer version left. When the AI uploads a version it can state what the version does to stored data (none, compatible, irreversible); the pending-release box shows that statement. Before releasing a version marked irreversible, export or back up the data if you may want to go back.

## App runtime limits and AI permissions

**Container runtime** shows component state, enforced limits, storage and manifests. Saved CPU/memory limits apply to each component; their total can exceed one component's limit. Each component shows the memory it uses against its limit, in amber above 85%. When the system stops a component for using more memory than its limit, the page says so, with the time. Component output is linked separately from static visitor logs.

**Storage** shows what the app uses against its budget, split into app data, source volumes, kept revision sources and development files. Sizes are measured in the background about every ten minutes, so the page never waits for Docker; **Measure now** updates the numbers in place without reloading edited forms. "Not measured yet" means unknown, not zero. The budget is a warning level (amber from 80%, red from 100%, with a popup), not a hard limit. The build cache is shown but not counted.

At **Environment and AI permissions**, choose the label and separately grant the actions you want delegated: package development, platform web tests, live lifecycle, bounded deployment, deletion, and calling the app's declared actions. An app can declare actions in its `app.json` (see [APPLICATIONS](APPLICATIONS.md#actions-the-ai-may-call)): the page lists each with its kind (read, write, destructive) and the exact request it makes. Three checkboxes decide which kinds the AI may call. When a new version changes the declared actions, the AI can call none of them until you release a version from this page yourself. A production label alone does not permit or forbid an action. **Production protection** is the default approval policy; explicit permissions may delegate actions within existing bounds. General development commands can run tests/install packages regardless of the separate platform web-test permission.

New Dockerfiles/images, broader network/GPU access and fresh credential grants still require review. Deleting an app removes its containers/versions but retains persistent data in Docker and backups. There is no owner UI for arbitrary environment variables; use source configuration/command arguments for ordinary settings.

## App credentials and log access

Under **App credentials**, store each manifest-required value and select the names in the **pending revision** grant form. Review that exact source before granting. New revisions do not inherit credential grants. All declared credentials must be present/granted before that version starts. Static client pages cannot consume server-side app keys.

Changing a live grant takes the app offline. After rotating a stored value, use **Apply credentials and restart**; restart failure leaves it offline. Granted code can read values. Deleting/rotating the stored value alone does not remove an existing process's copy.

Owner log views show retained bounded output. AI access is configurable: static `stats`/`masked` modes filter visitor information; runtime component stdout/stderr is available only with `full` access. Temporary full grants expire after 24 hours. Visibility changes affect the readable historical window. Logs/package commands can contain sensitive content emitted by an app; review what you permit.

## Networking, GPU and service listeners

At **Network and GPU access**, expand the pending revision's requested policy. Review protocol, destination, ports, DNS, LAN ranges, incoming interface/source rules and GPU use, then grant those exact capabilities. Changing grants stops production before saving; bring it online after reviewing the new bounds. Revoke removes standing network/GPU grants.

Default runtime access is denied apart from reviewed HTTPS CONNECT destinations. Direct TCP, UDP and platform DNS can be approved. An empty protocol port list means all ports; an omitted protocol means blocked. Private destinations need both a LAN grant and a matching protocol rule. Platform/Docker networks stay blocked. Incoming raw ports bypass Google website login. A VPN exposure does not install a VPN. Verify Windows/Docker forwarding, interfaces and firewall/router reachability on your host. Use [APP-CAPABILITIES](APP-CAPABILITIES.md) for the precise schema and limitations.

GPU is shared, with no guaranteed VRAM quota. Gaming holds GPU apps. A successful image build cannot verify driver/CUDA compatibility. The **GPU** page lists GPU holders in the main workspace, not all host/application processes.

## Dockerfile builds and package development

Provision the rootless worker once from PowerShell: `docker compose --profile tools pull builder-image`. On the app page review Dockerfile/source, choose this build's public TCP/UDP/DNS bounds and **Approve and build this revision**. The build log is shown live at the top of the page. Each app keeps its own build cache, so a rebuild skips the steps that did not change; the page shows its size and warns when the Dockerfile copies the whole source before installing dependencies (then the cache cannot help). Tick **Ignore cached steps** to rebuild every step, **Refresh base images** to force the registry check for newer base tags, or use **Delete build cache** to discard the cache. These are independent controls. The AI can read build output, including the current Dockerfile step, only when the app's log permission allows it; the same historical visibility window applies as for runtime output. Inspect operation status, logs and dependency inventory. Build success produces an immutable candidate image; release remains separate. Build workers have no production keys/data and cannot expose ports or access LAN. Rootless host prerequisites can require administrator configuration.

At **App development workspace**, select an existing base and grant separate development network/GPU access. For source-limited package fetching, use exact HTTPS sources such as `pypi.org:443` and `files.pythonhosted.org:443`, leaving public TCP/UDP and DNS blocked. Exact sources use the generated proxy. For IP/LAN/listener rules, have the AI file an exact development ask and review it in this section of the app page.

The development grant creates/resets the mutable session and permits package commands. The AI uses `app_development` to sync source, install packages, run commands and poll results. Inspect dependency changes, before/after inventories, collection errors and command output. Normal package additions within the granted bounds do not require repeated owner prompts.

**Start development** and **Stop development** retain mutable files. **Create release snapshot** freezes installed layers/source into a pending revision and stops development; live production is unchanged. Complete the new candidate's release prerequisites. **Revoke package permission** stops development. **Change development base or access** replaces the mutable container even for the same base: snapshot valuable work first. Unsnapshotted layers are not normal backup data. Sync overlays files; remove obsolete source explicitly.

## Data exports and Files

At **Export app data**, enter relative `/data` paths, choose a maximum MiB size and approve stopping the app. Review personal information/app-written credentials before exporting: the workspace AI can read the resulting copy. The operation writes a unique inbox directory with hashes/provenance. Successful results link to **Open inbox**. The app resumes only when its prior online/version state still matches. Failures may leave a bounded partial copy; review/clean it in Files. Interrupted exports leave the app offline for review.

On **Files**, browse workspace folders, view content, download, rename, create folders, upload and delete. HTML/source is shown as text or downloaded; image previews are constrained. Uploads use 8 MiB chunks with a per-file limit (50 GiB by default) and free-space checks. Pause, then select the same original file again to resume; cancelling discards the partial upload. Resume depends on that browser's saved upload ID and the server retaining the partial file. Overwriting requires its checkbox. Deletion moves items to `.trash`; **Empty .trash** permanently frees them. Trash and partial uploads still consume disk. Browser file operations do not browse private live app volumes; use approved app-data export.

Workspace MCP exports create expiring downloadable links; anyone with a valid link can use it until expiry. Root `files.example.com/` should return Not found. That is expected.

## Asks from the AI, Disk, gaming, Settings and recovery

There is no separate requests page. What the AI asks for appears on the page of the app, inside the section where you would do it yourself: a visibility change under **Who can open it**, a credential under **App credentials**, going back to an earlier revision under **Versions**, and so on. A line at the top of the page links to each ask. Every ask separates the platform's computed action/bounds from the AI's unverified note. **Apply** carries it out, **Dismiss** removes it; public exposure needs typed confirmation. The AI can ask for: visibility, bringing an app online, log access, a credential, network/GPU access, a development workspace, a data export and going back to a kept revision. A new version is not an ask: it waits as the pending version. Taking an app offline or deleting it is either permitted to the AI under AI permissions or done by you. **Audit** records actions without credential values.

**Disk** shows the free space inside Docker's disk and the images nothing uses. The platform removes the images of app revisions it no longer keeps by itself. **Images without a name** are leftovers of earlier builds: remove them all with one button. Named unused images are listed with an estimate of the space each frees (images share layers, so it is an estimate); removing a base image only means it is downloaded again when needed. The free space shown is that of Docker's own virtual disk; the Windows drive it lives on can be fuller.

Use Apps gaming controls to turn gaming on/off; configure limits and executable names in **Settings**. The automatic Windows trigger only turns it on. The AI cannot turn it off. Check actual effective limits, held app states and GPU process verification. A frozen workspace can retain VRAM; **Kill** stops a selected holder and its job immediately. Partial/error checks need review. Normal online GPU apps resume when gaming ends; explicitly stopped development stays stopped.

**Settings** configures application defaults/request limits, gaming and log defaults. Defaults do not replace live owner resource overrides. Higher per-component limits require host capacity. ComfyUI's example needs at least 8192 MiB allowed memory; models are not supplied. Follow [its host checklist](../examples/comfyui/README.md).

The Apps **Emergency access lock** temporarily overrides websites to owner-only and stops raw-listener apps/development. It preserves saved audiences/protection. **Restore saved visibility** restores audiences, then verify routing; restart stopped development explicitly. It does not retract downloaded data/previously read credentials.

After a host/container restart, inspect routing, app lifecycle, operations and background jobs. Interrupted workspace jobs are not automatically resumed. See [SETUP](SETUP.md#4-backups-and-restore) for backup/restore and [GO-LIVE](GO-LIVE.md) for acceptance and troubleshooting.
