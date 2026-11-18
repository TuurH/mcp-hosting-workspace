"""Archive extraction, app names, log masking, gaming mode and the MCP-side helpers."""

from __future__ import annotations

import io
import json
import os
import tarfile
import time
from pathlib import Path
from typing import Any

import pytest

from mcp_hosting_platform.common.docker_client import ExecResult, demux
from mcp_hosting_platform.common.names import name_problem
from mcp_hosting_platform.common.tarsafe import UnsafeArchive, extract_untrusted
from mcp_hosting_platform.deployer import logs
from mcp_hosting_platform.deployer.db import Database
from mcp_hosting_platform.deployer.gaming import Gaming
from platform_helpers import deployer_settings, site_archive


# ---------------------------------------------------------------- tarsafe
def raw_archive(tmp_path: Path, build) -> Path:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        build(archive)
    path = tmp_path / "raw.tar.gz"
    path.write_bytes(buffer.getvalue())
    return path


def add_file(archive: tarfile.TarFile, name: str, data: bytes = b"x") -> None:
    info = tarfile.TarInfo(name)
    info.size = len(data)
    archive.addfile(info, io.BytesIO(data))


def test_extracts_files_with_normal_permissions(tmp_path):
    archive = site_archive({"index.html": "hi", "a/b/c.css": "p{}"}, tmp_path)
    result = extract_untrusted(archive, tmp_path / "out")
    assert result.files == 2
    assert (tmp_path / "out" / "a" / "b" / "c.css").read_text() == "p{}"
    assert oct((tmp_path / "out" / "index.html").stat().st_mode & 0o777) == "0o644"


def test_digest_ignores_order(tmp_path):
    one = site_archive({"a": "1", "b": "2"}, tmp_path, "one.tar.gz")
    two = site_archive({"b": "2", "a": "1"}, tmp_path, "two.tar.gz")
    assert extract_untrusted(one, tmp_path / "o1").digest == extract_untrusted(two, tmp_path / "o2").digest


@pytest.mark.parametrize("name", ["../evil", "a/../../evil", "/etc/passwd", ".git/config", "a\\b"])
def test_rejects_bad_names(tmp_path, name):
    archive = raw_archive(tmp_path, lambda a: add_file(a, name))
    with pytest.raises(UnsafeArchive):
        extract_untrusted(archive, tmp_path / "out")


@pytest.mark.parametrize("kind", [tarfile.SYMTYPE, tarfile.LNKTYPE, tarfile.FIFOTYPE, tarfile.CHRTYPE])
def test_rejects_links_and_devices(tmp_path, kind):
    def build(archive):
        info = tarfile.TarInfo("thing")
        info.type = kind
        info.linkname = "/etc/passwd"
        archive.addfile(info)
    with pytest.raises(UnsafeArchive):
        extract_untrusted(raw_archive(tmp_path, build), tmp_path / "out")


def test_rejects_duplicates_and_clashes(tmp_path):
    archive = raw_archive(tmp_path, lambda a: (add_file(a, "x"), add_file(a, "x")))
    with pytest.raises(UnsafeArchive, match="duplicate"):
        extract_untrusted(archive, tmp_path / "o1")
    archive = raw_archive(tmp_path, lambda a: (add_file(a, "x"), add_file(a, "x/y")))
    with pytest.raises(UnsafeArchive):
        extract_untrusted(archive, tmp_path / "o2")


def _extract_peak_kb(path: Path, out: Path) -> tuple[str, int]:
    """Extract in a fresh process; return ('refused'|'extracted', peak memory in KB)."""
    import subprocess
    import sys

    # VmHWM (peak memory of this process image); ru_maxrss would include the
    # memory the test process had when it forked.
    probe = (
        "import re, sys\n"
        "from pathlib import Path\n"
        "from mcp_hosting_platform.common.tarsafe import UnsafeArchive, extract_untrusted\n"
        "try:\n"
        "    extract_untrusted(Path(sys.argv[1]), Path(sys.argv[2]))\n"
        "    verdict = 'extracted'\n"
        "except UnsafeArchive:\n"
        "    verdict = 'refused'\n"
        "print(verdict, re.search(r'VmHWM:\\s+(\\d+)', open('/proc/self/status').read()).group(1))\n"
    )
    platform_dir = Path(__file__).resolve().parent.parent / "platform"
    result = subprocess.run([sys.executable, "-c", probe, str(path), str(out)],
                            capture_output=True, text=True, timeout=120, env={"PYTHONPATH": str(platform_dir)})
    verdict, peak = result.stdout.split()
    return verdict, int(peak)


def _pax_archive(tmp_path: Path, build) -> Path:
    import gzip

    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w", format=tarfile.PAX_FORMAT) as archive:
        build(archive)
    path = tmp_path / "pax.tar.gz"
    path.write_bytes(gzip.compress(buffer.getvalue(), compresslevel=1))
    return path


def test_huge_metadata_header_is_refused_without_reading_it(tmp_path):
    # A 200 MB PAX header compresses to a few hundred KB. It must be refused
    # before tarfile reads it into memory (the deployer is limited to 512 MB).
    def build(archive):
        info = tarfile.TarInfo("index.html")
        info.size = 1
        info.pax_headers = {"comment": "x" * (200 * 1024 * 1024)}
        archive.addfile(info, io.BytesIO(b"x"))
    verdict, peak = _extract_peak_kb(_pax_archive(tmp_path, build), tmp_path / "out")
    assert verdict == "refused" and peak < 100 * 1024


def test_sparse_files_are_refused_before_their_map_is_read(tmp_path):
    # PAX sparse 1.0 keeps its map in the member's data, which tarfile reads
    # into a list without a limit: 4 KB compressed was 200 MB in memory.
    count = 1_000_000
    data = f"{count}\n".encode() + b"0\n" * (2 * count)

    def build(archive):
        info = tarfile.TarInfo("a.txt")
        info.size = len(data)
        info.pax_headers = {"GNU.sparse.major": "1", "GNU.sparse.minor": "0", "GNU.sparse.realsize": "0"}
        archive.addfile(info, io.BytesIO(data))
    verdict, peak = _extract_peak_kb(_pax_archive(tmp_path, build), tmp_path / "out")
    assert verdict == "refused" and peak < 100 * 1024

    for headers in ({"GNU.sparse.map": "0,1", "GNU.sparse.realsize": "1"},  # 0.1
                    {"GNU.sparse.size": "1", "GNU.sparse.offset": "0", "GNU.sparse.numbytes": "1"}):  # 0.0
        def small(archive, headers=headers):
            info = tarfile.TarInfo("b.txt")
            info.size = 1
            info.pax_headers = headers
            archive.addfile(info, io.BytesIO(b"x"))
        path = _pax_archive(tmp_path, small)
        out = tmp_path / f"out-{len(headers)}"
        with pytest.raises(UnsafeArchive):
            extract_untrusted(path, out)


def test_global_pax_headers_are_refused(tmp_path):
    # A global header is copied into every member that follows it.
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w", format=tarfile.PAX_FORMAT,
                      pax_headers={"comment": "x" * 60_000}) as archive:
        for n in range(3):
            info = tarfile.TarInfo(f"f{n}.html")
            info.size = 1
            archive.addfile(info, io.BytesIO(b"x"))
    path = tmp_path / "global.tar"
    path.write_bytes(buffer.getvalue())
    with pytest.raises(UnsafeArchive):
        extract_untrusted(path, tmp_path / "out")


def test_many_member_headers_stay_bounded(tmp_path):
    # 2000 members with 60 KB of PAX keys each: refused once the total passes 1 MB.
    def build(archive):
        for n in range(2000):
            info = tarfile.TarInfo(f"f{n}.html")
            info.size = 1
            info.pax_headers = {f"k{i}": "v" * 50 for i in range(1000)}
            archive.addfile(info, io.BytesIO(b"x"))
    verdict, peak = _extract_peak_kb(_pax_archive(tmp_path, build), tmp_path / "out")
    assert verdict == "refused" and peak < 100 * 1024


def test_many_plain_members_do_not_accumulate(tmp_path):
    files = {f"d/f{n}.txt": "x" for n in range(5000)}
    archive = site_archive(files, tmp_path, "many.tar.gz")
    verdict, peak = _extract_peak_kb(archive, tmp_path / "out")
    assert verdict == "extracted" and peak < 100 * 1024


def test_size_limits(tmp_path):
    archive = site_archive({"a": b"x" * 2000, "b": b"x" * 2000}, tmp_path)
    with pytest.raises(UnsafeArchive, match="larger"):
        extract_untrusted(archive, tmp_path / "o1", max_total_bytes=3000)
    with pytest.raises(UnsafeArchive, match="larger"):
        extract_untrusted(archive, tmp_path / "o2", max_file_bytes=1000)
    with pytest.raises(UnsafeArchive, match="too many"):
        extract_untrusted(archive, tmp_path / "o3", max_files=1)


# ---------------------------------------------------------------- names
@pytest.mark.parametrize("name", ["blog", "weather", "pixel-tracker", "kmi", "music-bot"])
def test_good_names(name):
    assert name_problem(name) is None


@pytest.mark.parametrize("name", [
    "admin", "auth", "mcp", "files", "www", "minecraft", "canary",  # platform and DNS
    "login", "logins", "googie", "acount",                          # lookalikes
    "next-blog", "xn--bcher-kva", "a", "Blog", "-blog", "blog-", "a--b", "1blog", "x" * 31,
])
def test_bad_names(name):
    assert name_problem(name) is not None


def test_extra_reserved_names():
    assert name_problem("nas", frozenset({"nas"})) is not None


# ---------------------------------------------------------------- logs
def write_log(path: Path, entries: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as handle:
        for entry in entries:
            handle.write(json.dumps(entry) + "\n")
        handle.write("not json\n")


def caddy_entry(host: str, uri: str, ip: str = "203.0.113.77", status: int = 200) -> dict[str, Any]:
    return {
        "ts": time.time(), "status": status, "size": 10, "duration": 0.002,
        "request": {"host": host, "method": "GET", "uri": uri, "client_ip": ip,
                    "headers": {"User-Agent": ["Mozilla/5.0 bob@example.org"], "Cf-Ipcountry": ["BE"]}},
    }


def test_log_modes(tmp_path):
    path = tmp_path / "access.log"
    write_log(path, [
        caddy_entry("blog.example.com", "/?email=alice@example.org"),
        caddy_entry("other.example.com", "/secret"),
        caddy_entry("blog.example.com", "/about", ip="2001:db8:1234:5678::1"),
    ])
    full = logs.view(path, "blog.example.com", "full", 100, salt="s")
    assert [e["uri"] for e in full["entries"]] == ["/?email=alice@example.org", "/about"]
    masked = logs.view(path, "blog.example.com", "masked", 100, salt="s")
    first, second = masked["entries"]
    assert "alice@example.org" not in json.dumps(masked)
    assert "bob@example.org" not in json.dumps(masked)
    assert first["client_ip"] == "203.0.113.x"
    assert second["client_ip"] == "2001:db8:1234::/48"
    assert "untrusted" in masked["note"]
    stats = logs.view(path, "blog.example.com", "stats", 100, salt="s")
    assert "entries" not in stats and stats["stats"]["requests_24h"] == 2
    assert logs.view(path, "blog.example.com", "off", 100, salt="s") == {
        "mode": "off", "message": "Log access for this app is switched off on the admin page."}


def test_masked_views_show_only_the_apps_own_paths(tmp_path):
    path = tmp_path / "access.log"
    leak = caddy_entry("blog.example.com", "/YWxpY2VAZXhhbXBsZS5vcmc/x")
    leak["request"]["headers"]["Referer"] = ["https://blog.example.com/thanks?who=alice@example.org"]
    write_log(path, [caddy_entry("blog.example.com", "/about/"), caddy_entry("blog.example.com", "/my%20page.html"),
                     caddy_entry("blog.example.com", "/u/alice-example-org"), leak])
    known = {"/", "/index.html", "/about/", "/about", "/about/index.html", "/my page.html"}
    for mode in ("masked", "stats"):
        result = logs.view(path, "blog.example.com", mode, 100, salt="s", known=known)
        text = json.dumps(result)
        assert "alice" not in text and "YWxp" not in text and "thanks" not in text, mode
        assert dict(result["stats"]["top_paths_24h"]) == {"[other path]": 2, "/about/": 1, "/my page.html": 1}
    masked = logs.view(path, "blog.example.com", "masked", 100, salt="s", known=known)["entries"]
    assert masked[-1]["referer"] == "https://blog.example.com"
    # The owner (known=None) still sees the real paths.
    owner = logs.view(path, "blog.example.com", "full", 100, salt="s")
    assert owner["entries"][2]["uri"] == "/u/alice-example-org"


def test_nicknames_differ_between_apps(tmp_path):
    path = tmp_path / "access.log"
    write_log(path, [caddy_entry("blog.example.com", "/"), caddy_entry("shop.example.com", "/")])
    blog = logs.view(path, "blog.example.com", "masked", 10, salt="s")["entries"][0]["user_agent"]
    shop = logs.view(path, "shop.example.com", "masked", 10, salt="s")["entries"][0]["user_agent"]
    assert "user-" in blog and "user-" in shop and blog != shop


# ---------------------------------------------------------------- gaming mode
class FakeDocker:
    def __init__(self, gpu_processes: int = 1) -> None:
        self.id = "container-1"
        self.limits = {"NanoCpus": 8_000_000_000, "Memory": 12 * 1024**3, "MemorySwap": 12 * 1024**3}
        self.usage = 1 * 1024**3
        self.gpu = [{"pid": 100 + i, "pgid": 100 + i, "command": "python train.py"} for i in range(gpu_processes)]
        self.updates: list[dict[str, int]] = []
        self.signals: list[str] = []
        self.helpers: list[dict[str, Any]] = []
        self.fail_updates = False
        self.broken_reaper = 0      # number of reaper runs that return garbage
        self.protected_gpu: list[int] = []
        self.paused = False
        self.pauses: list[str] = []
        self.fail_unpause = 0       # number of unpause calls that fail
        self.fail_pause = False     # pause fails (e.g. the workspace was stopped)
        self.pause_times_out = False  # the daemon pauses, but the call reports a timeout
        self.fail_inspect = 0       # number of inspect calls that time out
        self.gone = False           # the workspace was removed
        self.stubborn = False       # GPU processes survive signals unless the workspace is frozen

    def ping(self) -> str:
        return "ok"

    def container_exists(self, name: str) -> bool:
        return not self.gone

    def inspect_container(self, name: str) -> dict[str, Any]:
        from mcp_hosting_platform.common.docker_client import DockerError
        if self.gone:
            raise DockerError(404, "No such container")
        if self.fail_inspect:
            self.fail_inspect -= 1
            raise DockerError(0, "timed out")
        return {"Id": self.id, "HostConfig": dict(self.limits), "State": {"Paused": self.paused}}

    def pause_container(self, name: str) -> None:
        from mcp_hosting_platform.common.docker_client import DockerError
        if self.gone or self.fail_pause:
            raise DockerError(404 if self.gone else 409, "cannot pause")
        if self.paused:
            raise DockerError(409, "already paused")
        self.paused = True
        self.pauses.append("pause")
        if self.pause_times_out:
            raise DockerError(0, "timed out")

    def unpause_container(self, name: str) -> None:
        if self.fail_unpause:
            from mcp_hosting_platform.common.docker_client import DockerError
            self.fail_unpause -= 1
            raise DockerError(500, "unpause failed")
        self.paused = False
        self.pauses.append("unpause")

    def memory_usage(self, name: str) -> int:
        return self.usage

    def update_container(self, name: str, **resources: int) -> None:
        if self.fail_updates:
            from mcp_hosting_platform.common.docker_client import DockerError
            raise DockerError(500, "update failed")
        self.updates.append(resources)
        self.limits.update(resources)

    def run_capture(self, config: dict[str, Any], timeout: float = 120.0, max_output: int = 0) -> tuple[int, str]:
        self.helpers.append(config)
        assert config["HostConfig"]["NetworkMode"] == "none"
        if "gpu_reaper" in " ".join(config["Entrypoint"]):
            host = config["HostConfig"]
            assert host["PidMode"] == "container:mcp-hosting-workspace"
            assert host["CapDrop"] == ["ALL"] and set(host["CapAdd"]) == {"KILL", "SYS_PTRACE", "DAC_READ_SEARCH"}
            if self.broken_reaper:
                self.broken_reaper -= 1
                return 0, "x" * 100  # e.g. output flooded by the AI
            action, frozen = config["Cmd"][0], config["Cmd"][1:] == ["--frozen"]
            if action == "KILL-PID":
                pid = int(config["Cmd"][1])
                if pid in self.protected_gpu:
                    return 0, json.dumps({"error": "that process is protected (init or the MCP server)"})
                match = [p for p in self.gpu if p["pid"] == pid]
                if not match:
                    return 0, json.dumps({"error": "no such process (it may have stopped already)"})
                self.gpu = [p for p in self.gpu if p["pid"] != pid]
                self.signals.append(f"KILL {pid}")
                return 0, json.dumps({"killed": 1, "pid": pid, "command": match[0]["command"]})
            assert self.paused or not frozen, "--frozen only while the workspace is paused"
            found = list(self.gpu)
            if action in ("TERM", "KILL"):
                self.signals.append(action + (" (frozen)" if frozen else ""))
                if frozen or not self.stubborn:
                    self.gpu = []  # processes stop when signalled
            sample = [{**p, "reason": "GPU device open", "state": "S"} for p in found[:20]]
            return 0, json.dumps({"count": len(found), "sample": sample, "signalled": len(found),
                                  "partial": False, "protected_gpu": self.protected_gpu,
                                  "kcmp": True, "seconds": 0.1})
        return 0, ""

    def run_once(self, config: dict[str, Any], timeout: float = 120.0) -> int:
        return self.run_capture(config, timeout)[0]


def make_gaming(tmp_path: Path, docker: FakeDocker) -> Gaming:
    settings = deployer_settings(tmp_path)
    db = Database(settings.db_path)
    db.set_setting("gaming", {"stop_grace_seconds": 0})
    return Gaming(settings, db, docker)  # type: ignore[arg-type]


def test_gaming_on_and_off(tmp_path):
    docker = FakeDocker()
    gaming = make_gaming(tmp_path, docker)
    state = gaming.enter("manual", "owner")
    assert state["state"] == "on" and state["ok"] is True
    assert docker.signals == ["TERM"] and docker.pauses == []
    assert docker.limits["NanoCpus"] == 2_000_000_000
    assert docker.limits["Memory"] == 6 * 1024**3 == docker.limits["MemorySwap"]
    control = json.loads((gaming.settings.control_dir / "state.json").read_text())
    assert control["gaming"] is True and control["workspace_gpu_allowed"] is False

    assert gaming.leave("owner")["ok"] is True
    assert docker.limits["NanoCpus"] == 8_000_000_000
    assert docker.limits["Memory"] == 12 * 1024**3
    control = json.loads((gaming.settings.control_dir / "state.json").read_text())
    assert control["gaming"] is False and control["workspace_gpu_allowed"] is True


def test_gaming_never_squeezes_memory_below_use(tmp_path):
    docker = FakeDocker(gpu_processes=0)
    docker.usage = 8 * 1024**3
    gaming = make_gaming(tmp_path, docker)
    gaming.enter("auto", "watcher")
    assert docker.limits["Memory"] == int(8 * 1024**3 * 1.3)
    assert any("kept at" in line["message"] for line in gaming.state()["log"])


def test_gaming_reapplies_after_recreate_and_keeps_the_normal_limits(tmp_path):
    docker = FakeDocker(gpu_processes=0)
    gaming = make_gaming(tmp_path, docker)
    gaming.enter("manual", "owner")
    # The owner lowers the gaming CPUs: the saved (normal) limits must not change.
    gaming.db.set_setting("gaming", {"workspace_cpus": 1})
    gaming.reconcile()
    assert docker.limits["NanoCpus"] == 1_000_000_000
    assert gaming.state()["saved_limits"]["limits"]["NanoCpus"] == 8_000_000_000
    # compose recreates the workspace with its normal limits
    docker.id = "container-2"
    docker.limits = {"NanoCpus": 8_000_000_000, "Memory": 12 * 1024**3, "MemorySwap": 12 * 1024**3}
    gaming.reconcile()
    assert docker.limits["NanoCpus"] == 1_000_000_000
    gaming.leave("owner")
    assert docker.limits["NanoCpus"] == 8_000_000_000


def test_gaming_kills_gpu_work_started_later(tmp_path):
    docker = FakeDocker(gpu_processes=0)
    gaming = make_gaming(tmp_path, docker)
    gaming.enter("manual", "owner")
    docker.gpu = [{"pid": 300, "pgid": 300, "command": "python sneaky.py"}]
    gaming.reconcile()
    assert docker.signals[-2:] == ["TERM", "KILL"]  # TERM, a short wait, then KILL in the same round


def test_unreadable_gpu_check_freezes_the_workspace_and_kills(tmp_path):
    docker = FakeDocker(gpu_processes=2)
    docker.broken_reaper = 1  # the AI floods the helper's output
    gaming = make_gaming(tmp_path, docker)
    state = gaming.enter("manual", "owner")
    assert docker.pauses == ["pause", "unpause"]
    assert docker.signals == ["KILL (frozen)"]
    assert state["ok"] is True and docker.limits["NanoCpus"] == 2_000_000_000  # limits applied anyway


def test_reconcile_never_raises_the_memory_cap(tmp_path):
    docker = FakeDocker(gpu_processes=0)
    gaming = make_gaming(tmp_path, docker)
    gaming.enter("manual", "owner")
    cap = docker.limits["Memory"]
    for _ in range(5):
        docker.usage = int(docker.limits["Memory"] * 0.95)
        gaming.reconcile()
    assert docker.limits["Memory"] == cap


def test_limits_saved_by_the_first_build_are_restored(tmp_path):
    docker = FakeDocker(gpu_processes=0)
    gaming = make_gaming(tmp_path, docker)
    docker.limits = {"NanoCpus": 2_000_000_000, "Memory": 6 * 1024**3, "MemorySwap": 6 * 1024**3}
    gaming.db.set_setting("gaming_state", {"state": "on", "log": [], "saved_limits": {
        "NanoCpus": 8_000_000_000, "Memory": 12 * 1024**3, "MemorySwap": 12 * 1024**3}})
    gaming.reconcile()
    gaming.leave("owner")
    assert docker.limits["NanoCpus"] == 8_000_000_000 and docker.limits["Memory"] == 12 * 1024**3


def test_gpu_held_by_the_mcp_server_is_reported(tmp_path):
    docker = FakeDocker(gpu_processes=0)
    docker.protected_gpu = [7]
    gaming = make_gaming(tmp_path, docker)
    state = gaming.enter("manual", "owner")
    assert state["ok"] is False and "MCP server" in state["error"]


def test_frozen_workspace_is_resumed(tmp_path):
    docker = FakeDocker(gpu_processes=1)
    docker.broken_reaper, docker.fail_unpause = 1, 1
    gaming = make_gaming(tmp_path, docker)
    assert gaming.enter("manual", "owner")["ok"] is False  # the unpause failed
    assert docker.paused is True
    gaming.reconcile()
    assert docker.paused is False and "frozen" not in gaming.state()


def test_a_pause_made_by_hand_is_left_alone(tmp_path):
    docker = FakeDocker(gpu_processes=0)
    gaming = make_gaming(tmp_path, docker)
    gaming.enter("manual", "owner")
    docker.paused = True
    gaming.reconcile()
    assert docker.paused is True


def test_turning_off_resumes_a_frozen_workspace(tmp_path):
    docker = FakeDocker(gpu_processes=1)
    docker.broken_reaper, docker.fail_unpause = 1, 1
    gaming = make_gaming(tmp_path, docker)
    gaming.enter("manual", "owner")
    assert gaming.leave("owner")["ok"] is True
    assert docker.paused is False and gaming.state()["state"] == "off"


def test_a_pause_made_by_hand_survives_the_freeze_fallback(tmp_path):
    docker = FakeDocker(gpu_processes=0)
    gaming = make_gaming(tmp_path, docker)
    gaming.enter("manual", "owner")
    docker.paused = True  # the owner paused the workspace
    docker.gpu = [{"pid": 300, "pgid": 300, "command": "python sneaky.py"}]
    docker.broken_reaper = 1  # the normal check fails
    gaming.reconcile()
    gaming.reconcile()
    assert docker.paused is True and docker.pauses == []
    assert "KILL (frozen)" in docker.signals and docker.gpu == []


def test_a_failed_pause_leaves_nothing_to_resume(tmp_path):
    docker = FakeDocker(gpu_processes=1)
    docker.broken_reaper, docker.fail_pause = 1, True  # e.g. the workspace was just stopped
    gaming = make_gaming(tmp_path, docker)
    assert gaming.enter("manual", "owner")["ok"] is False
    assert "frozen" not in gaming.state()
    assert gaming.leave("owner")["ok"] is True


def test_turning_off_works_when_the_frozen_workspace_was_removed(tmp_path):
    docker = FakeDocker(gpu_processes=1)
    docker.broken_reaper, docker.fail_unpause = 1, 1
    gaming = make_gaming(tmp_path, docker)
    gaming.enter("manual", "owner")
    assert gaming.state().get("frozen")
    docker.gone = True  # docker compose rm workspace
    assert gaming.leave("owner")["ok"] is True
    assert gaming.state()["state"] == "off" and "frozen" not in gaming.state()


def test_minute_check_clears_its_error_once_things_work(tmp_path):
    docker = FakeDocker(gpu_processes=0)
    gaming = make_gaming(tmp_path, docker)
    gaming.enter("manual", "owner")
    docker.id = "container-2"
    docker.limits = {"NanoCpus": 8_000_000_000, "Memory": 12 * 1024**3, "MemorySwap": 12 * 1024**3}
    docker.fail_updates = True
    gaming.reconcile()
    assert gaming.state()["error"]
    docker.fail_updates = False
    gaming.reconcile()
    assert gaming.state()["error"] is None
    assert gaming.enter("ai", "ai")["ok"] is True and docker.signals == ["TERM", "TERM", "TERM"]  # no extra run


def test_a_full_disk_does_not_stop_gaming_mode_from_working(tmp_path, monkeypatch):
    docker = FakeDocker(gpu_processes=1)
    gaming = make_gaming(tmp_path, docker)

    def full_disk():
        raise OSError(28, "No space left on device")
    monkeypatch.setattr(gaming, "write_control", full_disk)
    state = gaming.enter("ai", "ai")
    assert state["state"] == "on" and "control file" in state["error"]
    assert docker.limits["NanoCpus"] == 2_000_000_000 and docker.gpu == []  # limits and GPU stop still ran
    docker.gpu = [{"pid": 300, "pgid": 300, "command": "python later.py"}]
    gaming.reconcile()
    assert docker.gpu == []


def test_workspace_stays_frozen_while_gpu_work_cannot_be_checked(tmp_path):
    docker = FakeDocker(gpu_processes=1)
    docker.stubborn = True       # survives the normal stop...
    gaming = make_gaming(tmp_path, docker)
    gaming.enter("manual", "owner")  # (stopped by the frozen pass)
    docker.gpu = [{"pid": 300, "pgid": 300, "command": "python later.py"}]
    docker.broken_reaper = 1  # the normal check fails...
    # ...and the helper cannot run at all while frozen (full disk):
    original = docker.run_capture

    def helper_fails_when_frozen(config, timeout=120.0, max_output=0):
        if config["Cmd"][1:] == ["--frozen"]:
            from mcp_hosting_platform.common.docker_client import DockerError
            raise DockerError(500, "no space left on device")
        return original(config, timeout, max_output)
    docker.run_capture = helper_fails_when_frozen  # type: ignore[method-assign]
    gaming.reconcile()
    assert docker.paused is True and gaming.state()["frozen"] == "held"
    assert "stays paused" in gaming.state()["error"]
    gaming.reconcile()  # still cannot check: still frozen, not resumed in between
    assert docker.paused is True and docker.pauses.count("unpause") == 1  # only the earlier, successful pass
    docker.run_capture = original  # type: ignore[method-assign]
    gaming.reconcile()
    assert docker.paused is False and docker.gpu == [] and "frozen" not in gaming.state()
    assert gaming.state()["error"] is None


def test_turning_off_resumes_a_held_freeze(tmp_path):
    docker = FakeDocker(gpu_processes=1)
    docker.broken_reaper = 1
    gaming = make_gaming(tmp_path, docker)
    original = docker.run_capture

    def helper_fails_when_frozen(config, timeout=120.0, max_output=0):
        if config["Cmd"][1:] == ["--frozen"]:
            from mcp_hosting_platform.common.docker_client import DockerError
            raise DockerError(500, "no space left on device")
        return original(config, timeout, max_output)
    docker.run_capture = helper_fails_when_frozen  # type: ignore[method-assign]
    gaming.enter("manual", "owner")
    assert docker.paused is True
    assert gaming.leave("owner")["ok"] is True and docker.paused is False


def test_a_pause_that_times_out_keeps_the_marker(tmp_path):
    docker = FakeDocker(gpu_processes=1)
    docker.broken_reaper, docker.pause_times_out = 1, True
    gaming = make_gaming(tmp_path, docker)
    original_pause = docker.pause_container

    def pause_then_inspect_fails(name):
        docker.fail_inspect = 1  # the follow-up inspect times out too
        original_pause(name)
    docker.pause_container = pause_then_inspect_fails  # type: ignore[method-assign]
    gaming.enter("manual", "owner")
    assert docker.paused is True and gaming.state().get("frozen")
    gaming.reconcile()
    assert docker.paused is False


def test_the_mcp_server_message_appears_once(tmp_path):
    docker = FakeDocker(gpu_processes=1)
    docker.stubborn, docker.protected_gpu = True, [42]
    gaming = make_gaming(tmp_path, docker)
    error = gaming.enter("manual", "owner")["error"]
    assert error.count("the MCP server process holds the GPU") == 1


def test_never_freezes_when_the_freeze_cannot_be_recorded(tmp_path, monkeypatch):
    import sqlite3
    docker = FakeDocker(gpu_processes=0)
    gaming = make_gaming(tmp_path, docker)
    gaming.enter("manual", "owner")
    docker.gpu = [{"pid": 300, "pgid": 300, "command": "python later.py"}]
    docker.broken_reaper = 2  # the GPU check cannot run (full disk)...

    def full(*args, **kwargs):
        raise sqlite3.OperationalError("database or disk is full")
    monkeypatch.setattr(gaming.db, "set_setting", full)  # ...and nothing can be saved
    gaming.reconcile()
    assert docker.paused is False and docker.pauses == []
    monkeypatch.undo()
    assert gaming.leave("owner")["ok"] is True and docker.paused is False


def test_a_saved_held_freeze_stays_when_a_later_save_fails(tmp_path, monkeypatch):
    import sqlite3
    docker = FakeDocker(gpu_processes=0)
    gaming = make_gaming(tmp_path, docker)
    gaming.enter("manual", "owner")
    docker.gpu = [{"pid": 300, "pgid": 300, "command": "python later.py"}]
    docker.broken_reaper = 1
    original = docker.run_capture

    def helper_fails_when_frozen(config, timeout=120.0, max_output=0):
        if config["Cmd"][1:] == ["--frozen"]:
            from mcp_hosting_platform.common.docker_client import DockerError
            raise DockerError(500, "no space left on device")
        return original(config, timeout, max_output)
    docker.run_capture = helper_fails_when_frozen  # type: ignore[method-assign]
    gaming.reconcile()
    assert docker.paused is True and gaming.state()["frozen"] == "held"

    def full(*args, **kwargs):
        raise sqlite3.OperationalError("database or disk is full")
    monkeypatch.setattr(gaming.db, "set_setting", full)
    gaming.reconcile()
    assert docker.paused is True and docker.pauses == ["pause"]


def test_turning_off_says_so_when_it_cannot_be_saved(tmp_path, monkeypatch):
    import sqlite3
    docker = FakeDocker(gpu_processes=0)
    gaming = make_gaming(tmp_path, docker)
    gaming.enter("manual", "owner")

    def full(*args, **kwargs):
        raise sqlite3.OperationalError("database or disk is full")
    monkeypatch.setattr(gaming.db, "set_setting", full)
    result = gaming.leave("owner")
    assert result["ok"] is False and "comes back on" in result["error"]
    assert gaming.state()["state"] == "off"  # acted on in memory meanwhile


def test_gaming_on_with_a_full_disk_keeps_working_and_never_loses_the_limits(tmp_path, monkeypatch):
    import sqlite3
    docker = FakeDocker(gpu_processes=1)
    gaming = make_gaming(tmp_path, docker)
    normal = dict(docker.limits)

    def full(*args, **kwargs):
        raise sqlite3.OperationalError("database or disk is full")
    monkeypatch.setattr(gaming.db, "set_setting", full)  # the deployer cannot write, Docker still works
    result = gaming.enter("manual", "owner")
    assert result["ok"] is False and "normal limits cannot be saved" in result["error"]
    assert docker.limits == normal  # not lowered: nothing would know how to restore them
    assert docker.gpu == []  # GPU work still stopped
    assert gaming.state()["state"] == "on"  # enforced from memory
    docker.gpu = [{"pid": 300, "pgid": 300, "command": "python again.py"}]
    gaming.reconcile()
    assert docker.gpu == []  # the minute check keeps stopping it
    gaming.leave("owner")
    assert docker.limits == normal


def test_an_old_error_clears_once_gaming_mode_is_off(tmp_path, monkeypatch):
    docker = FakeDocker(gpu_processes=0)
    gaming = make_gaming(tmp_path, docker)
    gaming.enter("manual", "owner")
    real = gaming.write_control
    calls = []

    def fails_once():
        calls.append(1)
        if len(calls) == 1:
            raise OSError(28, "No space left on device")
        real()
    monkeypatch.setattr(gaming, "write_control", fails_once)
    assert gaming.leave("owner")["ok"] is False
    gaming.reconcile()
    assert gaming.state()["state"] == "off" and gaming.state()["error"] is None


def test_an_interrupted_turn_on_is_finished(tmp_path):
    # The deployer restarted while the workspace was frozen during "gaming on".
    docker = FakeDocker(gpu_processes=1)
    gaming = make_gaming(tmp_path, docker)
    gaming._save_state({"state": "entering", "source": "ai", "frozen": True, "log": []})
    docker.paused = True
    gaming.reconcile()
    state = gaming.state()
    assert docker.paused is False and state["state"] == "on" and not state.get("error")
    assert docker.limits["NanoCpus"] == 2_000_000_000 and docker.gpu == []


def test_minute_check_freezes_when_gpu_work_survives_kill(tmp_path):
    docker = FakeDocker(gpu_processes=0)
    gaming = make_gaming(tmp_path, docker)
    gaming.enter("manual", "owner")
    docker.gpu = [{"pid": 300, "pgid": 300, "command": "python stubborn.py"}]
    docker.stubborn = True
    gaming.reconcile()
    assert docker.signals[-3:] == ["TERM", "KILL", "KILL (frozen)"]
    assert docker.gpu == [] and docker.pauses == ["pause", "unpause"]
    assert not gaming.state().get("error")


def test_turning_gaming_on_again_never_raises_the_memory_cap(tmp_path):
    docker = FakeDocker(gpu_processes=1)
    docker.stubborn = True
    docker.fail_unpause = 1  # leaves gaming mode on with an error, so "on" runs again
    gaming = make_gaming(tmp_path, docker)
    gaming.enter("manual", "owner")
    cap = docker.limits["Memory"]
    for _ in range(3):
        docker.usage = int(docker.limits["Memory"] * 0.98)
        gaming.enter("ai", "ai")
    assert docker.limits["Memory"] == cap


def test_recreated_workspace_is_limited_after_a_failed_update(tmp_path):
    docker = FakeDocker(gpu_processes=0)
    gaming = make_gaming(tmp_path, docker)
    gaming.enter("manual", "owner")
    docker.id = "container-2"  # recreated with its normal limits
    docker.limits = {"NanoCpus": 8_000_000_000, "Memory": 12 * 1024**3, "MemorySwap": 12 * 1024**3}
    docker.fail_updates = True
    gaming.reconcile()
    assert "lowering the workspace limits failed" in gaming.state()["error"]
    docker.fail_updates = False
    gaming.reconcile()
    assert docker.limits["NanoCpus"] == 2_000_000_000


def test_gaming_reports_failures_and_retries_restore(tmp_path):
    docker = FakeDocker(gpu_processes=0)
    docker.fail_updates = True
    gaming = make_gaming(tmp_path, docker)
    state = gaming.enter("manual", "owner")
    assert state["ok"] is False and "update failed" in state["error"]
    docker.fail_updates = False
    gaming.enter("manual", "owner")  # retry works
    docker.fail_updates = True
    assert gaming.leave("owner")["ok"] is False
    assert gaming.state()["state"] == "leaving" and gaming.state()["saved_limits"]
    docker.fail_updates = False
    gaming.reconcile()
    assert gaming.state()["state"] == "off"
    assert docker.limits["NanoCpus"] == 8_000_000_000


def test_gaming_api_status_codes(tmp_path):
    from starlette.testclient import TestClient

    from mcp_hosting_platform.deployer.api import build_full_app
    from platform_helpers import make_platform

    platform, _ = make_platform(tmp_path)
    docker = FakeDocker(gpu_processes=0)
    docker.fail_updates = True
    gaming = Gaming(platform.settings, platform.db, docker)  # type: ignore[arg-type]
    client = TestClient(build_full_app(platform, gaming), base_url="http://deployer")
    response = client.post("/v1/gaming/on", json={})
    assert response.status_code == 502 and "update failed" in response.json()["error"]
    assert client.get("/v1/status").json()["docker"] == "ok"


def test_owner_sees_and_kills_gpu_processes(tmp_path):
    from starlette.testclient import TestClient

    from mcp_hosting_platform.deployer.api import build_full_app
    from platform_helpers import make_platform

    platform, _ = make_platform(tmp_path)
    docker = FakeDocker(gpu_processes=3)
    gaming = Gaming(platform.settings, platform.db, docker)  # type: ignore[arg-type]
    client = TestClient(build_full_app(platform, gaming), base_url="http://deployer")
    report = client.get("/v1/gpu").json()
    assert report["count"] == 3 and report["paused"] is False and report["kcmp"] is True
    assert report["sample"][0]["reason"] == "GPU device open"
    killed = client.post("/v1/gpu/kill", json={"pid": 100}, headers={"X-Actor": "owner@mcp-hosting.test"})
    assert killed.status_code == 200 and killed.json()["pid"] == 100
    assert [p["pid"] for p in docker.gpu] == [101, 102]
    assert client.post("/v1/gpu/kill", json={"pid": 999}).status_code == 400  # gone already
    assert client.post("/v1/gpu/kill", json={"pid": 1}).status_code == 400
    assert client.post("/v1/gpu/kill", json={"all": True}).json()["count"] == 2 and docker.gpu == []
    actions = [entry["action"] for entry in platform.db.audit_log()]
    assert "gpu.kill" in actions and "gpu.kill_all" in actions
    docker.paused = True
    gaming._save_state({**gaming.state(), "state": "on", "frozen": "held"})
    report = client.get("/v1/gpu").json()
    assert report["paused"] and report["held"] and client.get("/v1/status").json()["workspace_frozen"]


def test_the_ai_cannot_reach_the_gpu_page(tmp_path):
    from starlette.testclient import TestClient

    from mcp_hosting_platform.deployer.api import build_narrow_app
    from platform_helpers import make_platform

    platform, _ = make_platform(tmp_path)
    gaming = Gaming(platform.settings, platform.db, FakeDocker())  # type: ignore[arg-type]
    client = TestClient(build_narrow_app(platform, gaming), base_url="http://deployer")
    token = {"Authorization": f"Bearer {platform.settings.api_token}"}
    assert client.get("/v1/gpu", headers=token).status_code in (404, 405)
    assert client.post("/v1/gpu/kill", json={"all": True}, headers=token).status_code in (404, 405)


def test_demux():
    frame = bytes([1, 0, 0, 0, 0, 0, 0, 3]) + b"out" + bytes([2, 0, 0, 0, 0, 0, 0, 3]) + b"err"
    assert demux(frame) == (b"out", b"err")


# ---------------------------------------------------------------- MCP side
def test_pack_and_site_root(workspace_root, monkeypatch):
    from app import platform_tools

    app_dir = workspace_root / "apps" / "packtest"
    (app_dir / "dist").mkdir(parents=True)
    (app_dir / "dist" / "index.html").write_text("hi")
    (app_dir / "dist" / "node_modules").mkdir()
    (app_dir / "dist" / "node_modules" / "x.js").write_text("x")
    os.symlink("/etc/passwd", app_dir / "dist" / "passwd")
    root = platform_tools.site_root("packtest")
    assert root == app_dir / "dist"
    data, files, total = platform_tools.pack(root, 10_000)
    with tarfile.open(fileobj=io.BytesIO(data)) as archive:
        assert archive.getnames() == ["index.html"]
    assert (files, total) == (1, 2)
    with pytest.raises(ValueError):
        platform_tools.pack(root, 1)


def test_gpu_blocked_reads_control_file(tmp_path):
    from app import platform_tools

    control = tmp_path / "state.json"
    assert platform_tools.gpu_blocked(control) is None  # no deployer yet: GPU allowed
    control.write_text(json.dumps({"gaming": True, "workspace_gpu_allowed": False, "message": "gaming"}))
    assert platform_tools.gpu_blocked(control) == "gaming"
    control.write_text(json.dumps({"gaming": False, "workspace_gpu_allowed": True}))
    assert platform_tools.gpu_blocked(control) is None


def test_mcp_tools_talk_to_the_deployer(workspace_root, tmp_path, monkeypatch):
    """app_* tools -> narrow API -> deployer, with the real token handling."""
    from starlette.testclient import TestClient

    from app import platform_tools, server
    from mcp_hosting_platform.deployer.api import build_narrow_app
    from platform_helpers import make_platform

    platform, gaming = make_platform(tmp_path)
    client = TestClient(build_narrow_app(platform, gaming), base_url="http://deployer.test:8090")
    monkeypatch.setattr(platform_tools.httpx, "request",
                        lambda method, url, **kw: client.request(method, url, **{k: v for k, v in kw.items() if k not in ("timeout", "trust_env")}))

    site = workspace_root / "apps" / "e2e" / "site"
    site.mkdir(parents=True)
    (site / "index.html").write_text("<h1>e2e</h1>")
    result = server.app_deploy("e2e", note="first version")
    assert result["outcome"] == "live" and result["stage"] == "dev"
    assert result["source_folder"] == "apps/e2e/site"
    assert server.app_source("e2e", path="index.html")["content"] == "<h1>e2e</h1>"
    assert [a["name"] for a in server.app_list()["apps"]] == ["e2e"]

    request = server.app_request("e2e", "stage", {"stage": "public"}, note="please")
    assert request["status"] == "pending"
    assert platform.db.app("e2e")["stage"] == "dev"
    with pytest.raises(RuntimeError, match="request kind"):
        server.app_request("e2e", "docker", {})
    status = server.platform_status()
    assert status["pending_requests"] == 1 and status["people_and_groups"] == {"people": [], "groups": []}
