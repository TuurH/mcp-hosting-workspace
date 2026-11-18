# Setup and upgrades

Use this guide for installation, [GO-LIVE](GO-LIVE.md) to verify it, and [OWNER-GUIDE](OWNER-GUIDE.md) for day-to-day controls. Replace `example.com` in the examples with a domain you control. Set `PLATFORM_DOMAIN` to that domain in `.env` and use the same domain in Cloudflare and Google OAuth.

## 1. Host requirements

The reference host is a Windows PC with an NVIDIA GeForce RTX 3090 (24 GB), WSL 2, and Docker Desktop in Linux-container mode. Install an NVIDIA driver compatible with the CUDA image and PyTorch wheels. Keep Docker's **Expose daemon on tcp://localhost:2375 without TLS** setting off. Setup verifies GPU passthrough before building.

Copy [wslconfig.example](wslconfig.example) to `%UserProfile%\.wslconfig` and adjust memory/processors for your actual PC. Run `wsl --shutdown`, then restart Docker Desktop. The example is a starting point, not a requirement for every machine. Leave memory and CPU for Windows, Docker's infrastructure, builds and applications. The workspace defaults are 8 CPUs and 12 GiB; memory must fit below WSL's total. Saved owner limits also need to fit after an upgrade.

Enable Docker Desktop start at sign-in. Keep the PC awake and plan Windows/Docker updates for downtime. Docker Desktop and scheduled backups normally require a signed-in Windows session.

In **Docker Desktop → Settings → Docker Engine**, merge these keys into the existing JSON (do not replace unrelated settings):

```json
{
  "log-driver": "local",
  "log-opts": { "max-size": "10m", "max-file": "3" },
  "default-address-pools": [{ "base": "10.203.0.0/16", "size": 24 }]
}
```

Choose a pool that does not overlap your LAN/VPN or platform `10.204.x.x` networks. These defaults affect newly created networks/containers. Managed app containers also receive explicit bounded log settings.

### Other hardware and host operating systems

| Target | What to adapt |
| --- | --- |
| Another NVIDIA GPU on Windows | Check the driver, GPU architecture, CUDA base image and PyTorch wheel compatibility together. The default workspace uses CUDA 12.8 wheels. If changing the CUDA generation, review `CUDA_BASE_IMAGE` and the PyTorch installation in [Dockerfile](../Dockerfile) together. Size jobs and app grants for the card's actual VRAM, and validate the optional [ComfyUI example](../examples/comfyui/README.md) separately. |
| Less RAM, CPU, or disk | Tune `%UserProfile%\.wslconfig`, `WORKSPACE_MEMORY_LIMIT`, `WORKSPACE_CPU_LIMIT`, and `WORKSPACE_SHM_SIZE`. Allow room for builds, app containers, caches, and backups. Review saved owner limits when moving an existing install. |
| Native Linux with NVIDIA | The Docker platform stack is integration-tested on Ubuntu. A full native Linux install needs a compatible Docker Engine and Compose, the [NVIDIA Container Toolkit](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/install-guide.html), and `DOCKER_SOCKET_GID` set to the group of your Docker socket (`stat -c '%g' /var/run/docker.sock` for the default socket). Validate rootless BuildKit on the host, firewall and raw-port bindings, GPU passthrough, and signed-in routing. Adapt backup and restore paths, use a Linux scheduler, and use manual gaming controls or replace the Windows game watcher. |
| CPU-only, AMD, or another GPU runtime | This needs a separate build and Compose configuration. Adapt the workspace base image, PyTorch packages, NVIDIA device reservations and host GPU setup and smoke tests. GPU job admission, app GPU grants and gaming controls also need support for the chosen runtime. |

The [Ubuntu integration test](../tests/integration/platform_stack.sh) runs without the GPU workspace and Cloudflare tunnel. Use [GO-LIVE](GO-LIVE.md) on the target host before relying on a new configuration.

## 2. First installation and normal upgrades

Run from the repository folder in PowerShell:

```powershell
Set-ExecutionPolicy -Scope Process Bypass
```

For a first installation, copy `.env.example` to `.env`. For an upgrade, keep the existing `.env` and volumes, record your current commit and make a backup first (section 4). Update the checkout to the reviewed commit/branch. Do not delete volumes or use `docker compose down -v` as an upgrade step.

Fill these installation values before starting:

| Value | Where it comes from |
| --- | --- |
| `PLATFORM_DOMAIN` | A domain you control. Compose derives the MCP, files, admin, login and app hostnames from it. |
| `MCP_SERVER_NAME` | Optional name advertised during MCP initialization. Defaults to `MCP Hosting Workspace`. |
| `CLOUDFLARE_TUNNEL_TOKEN` | Cloudflare Tunnel Docker setup; use only its token |
| `CF_ACCESS_ENABLED=true` | Required for authenticated production MCP |
| `CF_ACCESS_TEAM_DOMAIN` | `https://<team>.cloudflareaccess.com` |
| `CF_ACCESS_AUD` | Audience of the Access application for `mcp.example.com` |
| `CF_ACCESS_ALLOWED_EMAILS` | Your permitted Google email address(es) |
| `PLATFORM_OWNER_EMAIL` | The actual owner Google account; verify setup's copied first address |
| `ADMIN_CF_ACCESS_AUD` | Separate admin Access application's audience (section 5) |
| `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET` | Separate Pomerium visitor-login client (section 5) |
| `RESERVED_APP_NAMES` | Additional existing subdomains, such as `nas,router` |

`ADMIN_MAX_UPLOAD_GB` controls the owner Files per-file ceiling (50 GiB by default); recreate admin after changing it.

Do not paste app/workspace service tokens into `.env`; use their owner forms. Do not replace existing generated secrets. In particular, changing `DEPLOYER_SECRETS_KEY` makes saved app credentials and workspace tokens unreadable.

If you use a custom MCP hostname, set `PUBLIC_MCP_HOST` and `PUBLIC_MCP_URL` together. `PUBLIC_MCP_URL` is the origin without `/mcp`; clients use the full endpoint with `/mcp`. `PUBLIC_FILES_URL` overrides the download origin separately. Match both overrides in your tunnel and Access settings.

Build first while the current installation stays running:

```powershell
.\scripts\setup.ps1 -BuildOnly
```

Setup adds missing settings, generates missing platform keys, installs the pinned BuildKit worker for Dockerfile apps, verifies GPU passthrough and builds images. It does not start or stop services with `-BuildOnly`. Check `.env` and protect an updated copy, then apply:

```powershell
.\scripts\setup.ps1
.\scripts\test-local.ps1
```

A normal Compose update recreates changed services and may interrupt connections. Setup's health timeout is a warning, not successful acceptance: inspect `docker compose ps` and `docker compose logs --tail 100 <service>`, then complete GO-LIVE. `-Pull` refreshes base images for the build; it does not change pinned Compose image versions. `-UpdateWsl` restarts WSL/Docker and requires you to restart Docker Desktop.

After upgrades, refresh MCP metadata in each client (section 3). Review saved workspace limits, pending releases, grants, routing and gaming state. An offline app stays offline.

Apply an upgrade when no build, release or export is running: restarting the deployer marks running operations as interrupted (apps that were online come back by themselves; an interrupted build is started again from the app page). Background jobs in the workspace stop when the workspace container is recreated. Asks that are waiting when you upgrade to the version with app actions are refused as "the app changed" when you apply them; the AI files them again. The first build of each app after that upgrade has no cache yet and takes as long as before.

### Moving an existing installation to a new checkout

Back up the current installation first. For installations already using the `mcp-hosting-workspace` Compose project, copy `.env` privately to the new checkout and keep the existing named Docker volumes. Run `docker compose ps` from the new checkout to confirm it sees the existing containers before upgrading. Reinstall any Windows scheduled tasks you use from the new checkout so they point to its scripts.

Installations under an earlier Compose project name need a migration. Keep the old checkout and back up with its matching scripts. Bring up the new project on a separate Docker engine for rehearsal because both versions use the same default subnet ranges. Move selected workspace files and application data through file and app exports, redeploy apps, and review the new grants before switching routes. Restore whole-stack backups with the checkout that created them. Apps that inspect the AI caller header should use `x-mcp-hosting-caller`.

To preserve existing MCP connections, retain your MCP hostname, `/mcp` endpoint, and OAuth settings. Set `MCP_SERVER_NAME` to your previous display name if you want clients to continue seeing it. Recreating the workspace interrupts active connections and jobs. Returning to an earlier version requires compatible database state and a matching `.env` backup.

## 3. Connect the AI clients

Create an owner-only Cloudflare Access application for `mcp.example.com`, enable **Managed OAuth**, and use the same AUD/email values in `.env`. Google login in Cloudflare has its own OAuth client with callback `https://<team>.cloudflareaccess.com/cdn-cgi/access/callback`. This is separate from the visitor client in section 5. Test the identity provider in Cloudflare.

If Managed OAuth restricts redirect URIs, include the callback URLs required by each client. Claude currently documents `https://claude.ai/api/mcp/auth_callback` and `https://claude.com/api/mcp/auth_callback`. Claude Code requires supported localhost/loopback redirects. Keep the Access policy limited to the intended owner accounts. Follow [Cloudflare Managed OAuth](https://developers.cloudflare.com/cloudflare-one/access-controls/applications/http-apps/managed-oauth/) for current dashboard labels and client settings.

### ChatGPT

In supported ChatGPT Work accounts, open **Plugins**, select **+ → Add custom MCP server**, enter `https://mcp.example.com/mcp`, choose OAuth, create the plugin, sign in and install it. Availability depends on the account/workspace policy. See [OpenAI's connection guide](https://developers.openai.com/plugins/deploy/connect-chatgpt).

After a server upgrade, restart it, open the connection in Plugins, choose **Refresh**, confirm the changed metadata and start a new conversation. Verify an actual tool call, not just that the server is listed.

### Claude

1. Open **Customize → Connectors** and add a custom web connector with `https://mcp.example.com/mcp` using your own hostname.
2. Choose **Sign in now** for Authentication and **Register automatically** for OAuth client with the Cloudflare Managed OAuth configuration above. Leave request headers empty and advanced options at their defaults.
3. Click **Add** and sign in with a permitted Google account. Enable the connector in a fresh chat and ask Claude to call `workspace_info`.

Claude reaches remote connectors from Anthropic's network. If a connector shows as connected but no tools load, refresh its tool list and check a fresh chat. Claude connector discovery is still being tested across clients. An HTTP 200 from the workspace confirms an HTTP response started. A completed `workspace_info` call verifies tool access. [Claude's remote connector guide](https://support.claude.com/en/articles/11175166-get-started-with-custom-connectors-using-remote-mcp) · [Open connector handshake report](https://github.com/anthropics/claude-ai-mcp/issues/1241)

### Claude Code

Configure Claude Code explicitly:

```sh
claude mcp add --transport http --scope user mcp-hosting-workspace https://mcp.example.com/mcp
claude mcp list
claude mcp get mcp-hosting-workspace
```

Run `/mcp` inside Claude Code and authenticate the server. See [Claude Code MCP documentation](https://code.claude.com/docs/en/mcp). Do not assume the web connector is automatically inherited.

Use background jobs (`workspace_job_start` plus status/log tools) for long work; client tool-call deadlines vary. For files, use `workspace_write_file` for small content or the owner Files page for larger uploads. A client attachment is not automatically a local workspace path.

## 4. Backups and restore

```powershell
.\scripts\backup.ps1 -ChooseFolder
.\scripts\backup.ps1 -Install
```

The first command chooses a folder and makes a backup. The second registers a daily 03:30 task while you are logged in; set `-At "20:00"` if that suits the PC's awake hours. Docker must run. Scheduled retries/logs do not make a sleeping or signed-out PC available.

Backups include labelled platform/app/source volumes and retained immutable runtime images. They include selected main-workspace top-level folders (`BACKUP_WORKSPACE_FOLDERS=apps,projects` by default), excluding caches, `node_modules` and `.venv`. They do not include the entire large workspace, unsnapshotted mutable app-development layers, or `.env`. Freeze valuable development work using **Create release snapshot** first. Stop database apps or make application-consistent snapshots before copying their data; copying a running database is insufficient.

For NAS storage use a UNC path (`\\server\share\folder`). The script stages locally, copies under a partial name and checks hashes before publishing the complete folder. Keep room for archives and immutable images. Retention defaults to 7 daily and 4 weekly backups.

Inspect `last-backup.json` in the destination and `%LOCALAPPDATA%\McpHostingWorkspaceBackup`; scheduled logs are under its `logs` subfolder. `ok-with-warnings`, `empty`, `error` and `ok-with-errors` need review. A previous database snapshot or files changing during copying can make recovery incomplete even when archives exist.

Restore requires the original protected `.env` and encryption key. Copy a NAS backup to a local drive, preserve a backup of current state, and run:

```powershell
.\scripts\restore.ps1 -Backup "C:\Backups\mcp-hosting-YYYYMMDD-HHMMSS"
# Add -IncludeWorkspaceFolders if you also want saved apps/projects overlaid.
```

Restore checks sizes/hashes/archive validity before replacing data, stops Compose and managed app containers, replaces backed-up volumes and restarts on success. It is a platform restore, not a per-app restore. Workspace-folder restore overlays saved files and keeps files added since the backup. On failure the stack stays stopped for repair. Complete GO-LIVE again after restoring; exercise restoration on a disposable installation before relying on backups.

## 5. Application platform and public routes

### App proxy and licensing

Pomerium handles visitor login, per-app access policy, and routing. Compose pulls the official `pomerium/pomerium:v0.34.1` image. Pomerium's source code is Apache 2.0, and its official images from v0.34 onward use the separate Pomerium Community Edition License. Review the image terms if you deploy or redistribute it. MCP Hosting Workspace's Apache 2.0 license covers this project's code. [Pomerium's licensing announcement](https://www.pomerium.com/blog/we-are-changing-how-we-license-pomerium-builds)

### Google visitor login

Create a Google OAuth **Web application** client with redirect URI `https://auth.example.com/oauth2/callback`. Set `GOOGLE_CLIENT_ID` and `GOOGLE_CLIENT_SECRET` in `.env`. Configure consent/audience for the intended visitors; if in Testing, add every intended test user. The platform uses `openid`, `email`, `profile`. Publishing/verification requirements depend on Google's current account policy; follow [Google OIDC documentation](https://developers.google.com/identity/openid-connect/openid-connect).

### Owner admin Access application

Create a separate self-hosted Access application for `admin.example.com`: Google identity provider, Allow policy for `PLATFORM_OWNER_EMAIL`. Copy its AUD to `ADMIN_CF_ACCESS_AUD`. Do not put the MCP application's AUD here. Admin remains locked when required authentication settings are missing.

### Tunnel routes and DNS

In the tunnel's published application routes, add these **HTTP** services in this order; wildcard last:

| Hostname | Service URL |
| --- | --- |
| `mcp.example.com` | `workspace:8000` |
| `files.example.com` | `files:8080` |
| `admin.example.com` | `admin:8080` |
| `auth.example.com` | `pomerium:80` |
| `*.example.com` | `pomerium:80` |

`files:8081` is the internal minting/health interface and must never be the public download route. Reserve infrastructure/custom subdomains. If the tunnel does not create wildcard DNS, add a proxied `CNAME` `*` to `<tunnel-id>.cfargotunnel.com`. Explicit DNS records take precedence; check existing subdomains before changing anything.

Add a Cloudflare **Bypass cache** rule for every platform/application hostname. For the default domain (exclude other services as appropriate):

```text
ends_with(http.host, ".example.com")
```

Do not cache authenticated assets/pages. Adapt rate limits to actual app traffic and your Cloudflare plan; aggressive rules can break uploads or WebSockets. A Cloudflare challenge blocking the canary means the automated probe is incomplete, not verified.

### Dockerfile build worker, gaming and local diagnostics

Setup automatically downloads the pinned BuildKit worker, including with `-BuildOnly`. If you start Compose directly without setup, or remove the worker image, install it before building Dockerfile apps:

```powershell
docker compose --profile tools pull builder-image
```

Review [rootless BuildKit host requirements](APP-CAPABILITIES.md#reviewed-dockerfile-images). A build failure needs diagnosis; setup does not silently change host namespace restrictions.

Gaming: use the admin controls or `.\scripts\gaming.ps1 on`, `off`, or no argument for status. Configure game executable names/limits in Settings; `gaming.ps1 -Install` installs the Windows trigger (Administrator may be required). The trigger turns gaming on when a listed program starts; you turn it off. AI clients may turn it on, never off.

For temporary owner-only local diagnostics, [compose.local.yaml](../compose.local.yaml) publishes localhost ports and disables MCP authentication. Stop cloudflared first and do not run unrelated localhost services while using the local admin cookie:

```powershell
docker compose stop cloudflared
docker compose -f compose.yaml -f compose.local.yaml up -d workspace admin
# Admin: docker compose logs admin; open its one-time local-login URL.
# MCP Inspector can connect to http://127.0.0.1:8000/mcp.
```

Return to production immediately afterwards:

```powershell
docker compose up -d --force-recreate workspace admin cloudflared
```

Use the normal authenticated domain for acceptance. Production infrastructure publishes no host ports; explicitly approved app TCP/UDP listeners can publish them.
