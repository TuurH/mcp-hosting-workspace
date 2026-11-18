"""Minimal Docker Engine API client over the unix socket.

Only what the deployer needs. Written against the Engine API directly (instead
of docker-py) because docker-py cannot update NanoCpus, which Compose's
``cpus:`` setting uses.
"""

from __future__ import annotations

import json
import re
import struct
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

import httpx

API_VERSION = "v1.44"


class DockerError(RuntimeError):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(f"Docker API error {status}: {message}")
        self.status = status


@dataclass
class ExecResult:
    exit_code: int
    stdout: str
    stderr: str


def demux(raw: bytes) -> tuple[bytes, bytes]:
    """Split Docker's multiplexed stdout/stderr stream (non-TTY exec/logs)."""
    stdout, stderr = bytearray(), bytearray()
    offset = 0
    while offset + 8 <= len(raw):
        stream_type = raw[offset]
        (size,) = struct.unpack(">I", raw[offset + 4 : offset + 8])
        payload = raw[offset + 8 : offset + 8 + size]
        (stderr if stream_type == 2 else stdout).extend(payload)
        offset += 8 + size
    return bytes(stdout), bytes(stderr)


class DockerClient:
    def __init__(self, socket_path: str = "/var/run/docker.sock", timeout: float = 60.0) -> None:
        transport = httpx.HTTPTransport(uds=socket_path)
        self._http = httpx.Client(
            transport=transport, base_url=f"http://docker/{API_VERSION}", timeout=timeout
        )

    def close(self) -> None:
        self._http.close()

    # -- low level ---------------------------------------------------------
    def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        try:
            response = self._http.request(method, path, **kwargs)
        except httpx.HTTPError as exc:  # socket missing, permission denied, timeout
            raise DockerError(0, f"cannot reach Docker ({exc})") from exc
        if response.status_code >= 400:
            try:
                message = response.json().get("message", response.text)
            except ValueError:
                message = response.text
            raise DockerError(response.status_code, message)
        return response

    # -- containers --------------------------------------------------------
    def list_containers(self, *, all: bool = True, labels: list[str] | None = None) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"all": "1" if all else "0"}
        if labels:
            params["filters"] = json.dumps({"label": labels})
        return self._request("GET", "/containers/json", params=params).json()

    def inspect_container(self, name: str) -> dict[str, Any]:
        return self._request("GET", f"/containers/{quote(name)}/json").json()

    def writable_size(self, name: str) -> int | None:
        """Slow: measure only from the background storage scanner."""
        size = self._request('GET', f'/containers/{quote(name)}/json', params={'size':'true'}, timeout=120).json().get('SizeRw')
        return int(size) if size is not None and int(size) >= 0 else None

    def container_exists(self, name: str) -> bool:
        try:
            self.inspect_container(name)
            return True
        except DockerError as exc:
            if exc.status == 404:
                return False
            raise

    def memory_usage(self, name: str) -> int:
        """Current memory use in bytes, excluding reclaimable file cache (like `docker stats`)."""
        stats = self._request(
            "GET", f"/containers/{quote(name)}/stats", params={"stream": "false", "one-shot": "true"}
        ).json()
        memory = stats.get("memory_stats") or {}
        usage = int(memory.get("usage") or 0)
        inactive = int((memory.get("stats") or {}).get("inactive_file") or 0)
        return max(0, usage - inactive)

    def update_container(self, name: str, **resources: Any) -> None:
        """Update live limits, e.g. NanoCpus=2_000_000_000, Memory=..., MemorySwap=..."""
        self._request("POST", f"/containers/{quote(name)}/update", json=resources)

    def start_container(self, name: str) -> None:
        self._request("POST", f"/containers/{quote(name)}/start")

    def stop_container(self, name: str, timeout: int = 10) -> None:
        self._request("POST", f"/containers/{quote(name)}/stop", params={"t": str(timeout)},
                      timeout=timeout + 30)

    def pause_container(self, name: str) -> None:
        self._request("POST", f"/containers/{quote(name)}/pause")

    def unpause_container(self, name: str) -> None:
        self._request("POST", f"/containers/{quote(name)}/unpause")

    def remove_container(self, name: str, force: bool = True) -> None:
        self._request("DELETE", f"/containers/{quote(name)}", params={"force": "1" if force else "0", 'v':'1'})

    def create_container(self, name: str | None, config: dict[str, Any], api_version: str | None = None) -> str:
        """``api_version`` selects a newer Engine API for one call (volume subpath mounts need v1.45)."""
        params = {"name": name} if name else {}
        path = f"http://docker/{api_version}/containers/create" if api_version else "/containers/create"
        return self._request("POST", path, params=params, json=config).json()["Id"]

    def wait_container(self, name: str, timeout: float = 120.0) -> int:
        response = self._request("POST", f"/containers/{quote(name)}/wait", timeout=timeout)
        return int(response.json().get("StatusCode", -1))

    def container_logs(self, name: str, tail: int = 200, since: float | None = None) -> tuple[str, str]:
        params = {"stdout": "1", "stderr": "1", "tail": str(tail), "timestamps": "1"}
        if since:
            # The Engine endpoint takes Unix seconds with optional nanoseconds;
            # the Docker CLI converts date strings before calling this API.
            params['since'] = f'{since:.9f}'
        response = self._request(
            "GET",
            f"/containers/{quote(name)}/logs",
            params=params,
        )
        out, err = demux(response.content)
        return out.decode("utf-8", "replace"), err.decode("utf-8", "replace")

    def follow_logs(self, name: str, sink, read_timeout: float | None = None) -> None:
        """Pass a container's output to ``sink(bytes)`` as it is produced, until it stops.

        Blocks; run it in its own thread. Docker keeps storing the output whether
        or not anyone follows it, so a slow sink never slows the container down.
        """
        buffer = b""
        with self._http.stream("GET", f"/containers/{quote(name)}/logs",
                               params={"stdout": "1", "stderr": "1", "follow": "1", "tail": "all"},
                               timeout=httpx.Timeout(10.0, read=read_timeout)) as response:
            if response.status_code >= 400:
                raise DockerError(response.status_code, "Container output is unavailable.")
            for chunk in response.iter_raw():
                buffer += chunk
                while len(buffer) >= 8:
                    (size,) = struct.unpack(">I", buffer[4:8])
                    if len(buffer) < 8 + size:
                        break
                    sink(bytes(buffer[8:8 + size]))
                    buffer = buffer[8 + size:]

    def run_once(self, config: dict[str, Any], timeout: float = 120.0) -> int:
        """Create, start, wait for and remove a short-lived container. Returns its exit code."""
        return self.run_capture(config, timeout=timeout)[0]

    def run_capture(self, config: dict[str, Any], timeout: float = 120.0,
                    max_output: int = 1024 * 1024) -> tuple[int, str]:
        """Like run_once, also returning (at most max_output bytes of) its stdout."""
        container_id = self.create_container(None, config)
        try:
            self.start_container(container_id)
            code = self.wait_container(container_id, timeout=timeout)
            response = self._request("GET", f"/containers/{container_id}/logs",
                                     params={"stdout": "1", "stderr": "0"})
            out, _err = demux(response.content[: max_output + 64 * 1024])
            return code, out[:max_output].decode("utf-8", "replace")
        finally:
            try:
                self.remove_container(container_id, force=True)
            except DockerError:
                pass

    def ping(self) -> str:
        """"ok", or why the Docker socket cannot be used."""
        try:
            self._request("GET", "/_ping", timeout=5.0)
            return "ok"
        except DockerError as exc:
            return str(exc)

    # -- exec ----------------------------------------------------------------
    def exec(self, name: str, cmd: list[str], user: str = "", timeout: float = 120.0,
             max_output_bytes: int = 4*1024*1024) -> ExecResult:
        exec_id = self._request(
            "POST",
            f"/containers/{quote(name)}/exec",
            json={"Cmd": cmd, "User": user, "AttachStdout": True, "AttachStderr": True, "Tty": False},
        ).json()["Id"]
        raw = bytearray()
        with self._http.stream('POST', f'/exec/{exec_id}/start', json={'Detach':False,'Tty':False},timeout=timeout) as response:
            if response.status_code >= 400:
                raise DockerError(response.status_code,'Could not execute the container command.')
            for chunk in response.iter_bytes(64*1024):
                raw.extend(chunk)
                if len(raw)>max_output_bytes:
                    raise DockerError(0,'Command output exceeded the limit.')
        out, err = demux(bytes(raw))
        info = self._request("GET", f"/exec/{exec_id}/json").json()
        return ExecResult(
            exit_code=int(info.get("ExitCode") or 0),
            stdout=out.decode("utf-8", "replace"),
            stderr=err.decode("utf-8", "replace"),
        )

    # -- images / networks ---------------------------------------------------
    def image_exists(self, name: str) -> bool:
        try:
            self._request("GET", f"/images/{quote(name, safe='')}/json")
            return True
        except DockerError as exc:
            if exc.status == 404:
                return False
            raise

    def connect_network(self, network: str, container: str) -> None:
        try:
            self._request("POST", f"/networks/{quote(network)}/connect", json={"Container": container})
        except DockerError as exc:
            if exc.status == 403 and "already exists" in str(exc):
                return
            raise

    def inspect_image(self, name: str) -> dict[str, Any]:
        return self._request("GET", f"/images/{quote(name, safe='')}/json").json()

    def network_subnets(self) -> list[str]:
        networks = self._request('GET', '/networks').json()
        return [entry['Subnet'] for network in networks for entry in (network.get('IPAM', {}).get('Config') or [])
                if entry.get('Subnet') and ':' not in entry['Subnet']]

    def commit_container(self, container: str) -> str:
        # No runtime credentials or production volumes may be present in callers.
        return self._request('POST', '/commit', params={'container': container, 'pause': 'true'}, timeout=300).json()['Id']

    def download_archive(self, container: str, path: str, target, max_bytes: int) -> None:
        received = 0
        with self._http.stream('GET', f'/containers/{quote(container, safe="")}/archive',
                                params={'path': path}, timeout=300) as response:
            if response.status_code >= 400:
                raise DockerError(response.status_code, 'Container archive is unavailable.')
            for chunk in response.iter_bytes(64 * 1024):
                received += len(chunk)
                if received > max_bytes:
                    raise ValueError('Archive exceeds the approved size limit.')
                target.write(chunk)

    def load_image(self, archive, max_bytes: int) -> str:
        import json
        import tarfile
        # Do not import builder-supplied tags (could overwrite a platform image).
        from .tarsafe import _BoundedTarInfo
        archive.seek(0,2)
        if archive.tell()>max_bytes: raise ValueError('Image archive exceeds its approved budget.')
        archive.seek(0)
        with tarfile.open(fileobj=archive, mode='r:',tarinfo=_BoundedTarInfo) as tar:
            member = tar.getmember('manifest.json')
            if member.size > 65536:
                raise ValueError('Image manifest is too large.')
            manifest = json.load(tar.extractfile(member))
            if len(manifest) != 1 or manifest[0].get('RepoTags'):
                raise ValueError('Builder must export one untagged Docker image.')
            config = tar.getmember(manifest[0]['Config'])
            if config.size > 1024 * 1024:
                raise ValueError('Image configuration is too large.')
        archive.seek(0)
        response = self._request('POST', '/images/load', params={'quiet': 'true'},
                                 content=iter(lambda: archive.read(64 * 1024), b''),
                                 headers={'Content-Type': 'application/x-tar'}, timeout=600)
        # Docker's image store can return an ID different from the exported
        # configuration digest. Use its confirmation, and check streamed errors
        # even when the HTTP response itself succeeded.
        loaded_ids = set()
        for line in response.iter_lines():
            if not line.strip():
                continue
            event = json.loads(line)
            error = event.get('error') or (event.get('errorDetail') or {}).get('message')
            if error:
                raise DockerError(0, 'Image import failed: ' + str(error))
            loaded_ids.update(re.findall(r'^Loaded image ID: (sha256:[0-9a-f]{64})\s*$',
                                         event.get('stream', ''), re.M))
        if len(loaded_ids) != 1:
            raise DockerError(0, 'Docker did not report exactly one loaded image ID.')
        return self.inspect_image(loaded_ids.pop())['Id']

    def ensure_network(self, name: str, internal: bool = True) -> str:
        try:
            return self._request("GET", f"/networks/{quote(name)}").json()["Id"]
        except DockerError as exc:
            if exc.status != 404:
                raise
        return self._request("POST", "/networks/create", json={"Name": name, "Internal": internal,
            "Labels": {"mcp-hosting-workspace.platform": "runtime"}}).json()["Id"]

    def ensure_volume(self, name: str, backup: bool = True, labels: dict[str, str] | None = None) -> bool:
        """Create a volume if it is missing. Volumes are included in backups unless
        ``backup`` is False (build caches and scratch space are not worth keeping)."""
        existed = True
        try:
            self._request("GET", f"/volumes/{quote(name)}")
        except DockerError as exc:
            if exc.status != 404:
                raise
            existed = False
        self._request("POST", "/volumes/create", json={"Name": name,
            "Labels": {"mcp-hosting-workspace.backup": "true" if backup else "false", "mcp-hosting-workspace.platform": "runtime",
                       **(labels or {})}})
        return existed

    def inspect_volume(self, name: str) -> dict[str, Any] | None:
        try:
            return self._request("GET", f"/volumes/{quote(name)}").json()
        except DockerError as exc:
            if exc.status == 404:
                return None
            raise

    def list_volumes(self, labels: list[str] | None = None) -> list[dict[str, Any]]:
        params = {"filters": json.dumps({"label": labels})} if labels else {}
        return self._request("GET", "/volumes", params=params).json().get("Volumes") or []

    def upload_archive(self, container: str, path: str, archive: bytes) -> None:
        self._request("PUT", f"/containers/{quote(container)}/archive", params={"path": path},
                      content=archive, headers={"Content-Type": "application/x-tar"}, timeout=120)

    def remove_network(self, name: str) -> None:
        self._request("DELETE", f"/networks/{quote(name)}")

    def disconnect_network(self, network: str, container: str) -> None:
        self._request("POST", f"/networks/{quote(network)}/disconnect", json={"Container":container,"Force":True})

    def remove_volume(self, name: str) -> None:
        self._request("DELETE", f"/volumes/{quote(name)}")

    def disk_usage(self) -> dict[str, Any]:
        return self._request("GET", "/system/df", timeout=30).json()

    def volume_usage(self) -> list[dict[str, Any]]:
        """Size of every volume. Slow on a large disk: call it from a background thread."""
        return self._request("GET", "/system/df", params={"type": "volume"}, timeout=180).json().get("Volumes") or []

    def image_usage(self) -> list[dict[str, Any]]:
        """Every image with its size and the number of containers that use it."""
        return self._request("GET", "/system/df", params={"type": "image"}, timeout=120).json().get("Images") or []

    def remove_image(self, image: str) -> None:
        """Remove one image by ID. Docker refuses while a container still uses it."""
        self._request("DELETE", f"/images/{quote(image, safe='')}", params={"noprune": "false"}, timeout=120)

    def prune_build_cache(self) -> int:
        """Clear the host's own build cache (from building the platform images). Returns bytes freed."""
        return int(self._request("POST", "/build/prune", params={"all": "true"}, timeout=600).json().get("SpaceReclaimed") or 0)
