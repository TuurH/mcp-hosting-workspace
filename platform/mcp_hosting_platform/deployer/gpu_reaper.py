"""Find and stop processes in the workspace that hold the GPU.

Runs in a short-lived helper container from the platform image that shares the
workspace's process namespace (``PidMode=container:<workspace>``) with only the
KILL, SYS_PTRACE and DAC_READ_SEARCH capabilities. Nothing from the workspace's
filesystem is used. GPU devices are recognised by their device numbers (from
this container's own /proc/devices and /proc/misc), so opening a device node
under another name does not hide a process. Threads with a file table of their
own are checked too, and a file table with more than MAX_FDS entries counts as
holding the GPU (checking it would take too long).

    python -m mcp_hosting_platform.deployer.gpu_reaper list
    python -m mcp_hosting_platform.deployer.gpu_reaper TERM
    python -m mcp_hosting_platform.deployer.gpu_reaper KILL
    python -m mcp_hosting_platform.deployer.gpu_reaper KILL --frozen
    python -m mcp_hosting_platform.deployer.gpu_reaper KILL-PID <pid>   (the owner's kill button)

A normal run stops checking after DEADLINE_SECONDS and reports "partial". With
--frozen (the workspace is paused, so nothing in it can change) every process
that was not checked by then is killed as well, so the run always finishes.

Prints one small JSON object (whatever the workspace contains):
{"count": n, "sample": [{"pid", "command", "reason", "state"}...], "signalled": n, "partial": bool,
 "unchecked": n, "protected_gpu": [...], "kcmp": bool, "seconds": float}
"""

from __future__ import annotations

import ctypes
import json
import os
import re
import signal
import stat
import sys
import time
from pathlib import Path

# GPU_REAPER_DEVICES (path pattern), GPU_REAPER_TEST_DEVICES ("major:minor,...")
# and GPU_REAPER_TEST_DEADLINE (seconds) only exist for the integration test (a
# stand-in device, a short time limit); the deployer never sets them.
_TEST_DEADLINE = float(os.environ.get("GPU_REAPER_TEST_DEADLINE") or 0)
DEADLINE_SECONDS = _TEST_DEADLINE or 20.0
FROZEN_DEADLINE_SECONDS = _TEST_DEADLINE or 40.0
MAX_FDS = 65536
SAMPLE = 20
_KCMP_SYSCALL = {"x86_64": 312, "aarch64": 272}.get(os.uname().machine)
_KCMP_FILES = 2
_libc = ctypes.CDLL(None, use_errno=True)
_TEST_PATHS = os.environ.get("GPU_REAPER_DEVICES")
_TEST_NUMBERS = os.environ.get("GPU_REAPER_TEST_DEVICES")
GPU_PATH = re.compile(_TEST_PATHS or r"^/dev/(dxg|nvidia[0-9]*|nvidiactl|nvidia-uvm|nvidia-uvm-tools|nvidia-modeset)$")


def gpu_device_numbers() -> set[tuple[int, int | None]]:
    """(major, minor) of /dev/dxg (a misc device) and (major, None) for every
    nvidia character-device major."""
    numbers: set[tuple[int, int | None]] = set()
    if _TEST_NUMBERS:
        for item in _TEST_NUMBERS.split(","):
            major, _, minor = item.partition(":")
            numbers.add((int(major), int(minor)))
        return numbers
    try:
        for line in Path("/proc/misc").read_text().splitlines():
            minor, _, name = line.strip().partition(" ")
            if name.strip() == "dxg" and minor.isdigit():
                numbers.add((10, int(minor)))
    except OSError:
        pass
    try:
        section = ""
        for line in Path("/proc/devices").read_text().splitlines():
            if line.endswith(":"):
                section = line
                continue
            major, _, name = line.strip().partition(" ")
            if section.startswith("Character") and major.isdigit() and name.strip().startswith("nvidia"):
                numbers.add((int(major), None))
    except OSError:
        pass
    return numbers


def _stat(pid: int) -> tuple[int, int, int] | None:
    """(ppid, pgid, start time) from /proc/<pid>/stat."""
    try:
        raw = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return None
    fields = raw[raw.rindex(")") + 2:].split()  # the command name may contain spaces
    return int(fields[1]), int(fields[2]), int(fields[19])


def _pids() -> list[int]:
    return [int(name) for name in os.listdir("/proc") if name.isdigit()]


def protected_pids(own: int) -> set[int]:
    """PID 1 (the workspace's init), the MCP server, and this helper."""
    children_of_init = []
    for pid in _pids():
        info = _stat(pid)
        if info and info[0] == 1:
            children_of_init.append((info[2], pid))
    protected = {1, own}
    if children_of_init:
        # The MCP server is the first process init started; a process can fake
        # its name, not its start time.
        protected.add(min(children_of_init)[1])
    return protected


def _same_files(first: int, second: int) -> bool:
    """True when two tasks share one file table (kcmp). False when unsure, so
    the task is checked on its own (kernels without CONFIG_CHECKPOINT_RESTORE
    have no kcmp: every thread's table is then read, which is slower)."""
    if _KCMP_SYSCALL is None:
        return False
    return _libc.syscall(_KCMP_SYSCALL, first, second, _KCMP_FILES, 0, 0) == 0


def _fd_dirs(pid: int) -> list[str]:
    """The fd folder of each distinct file table in the process. A thread can
    have its own table (unshare), and a process whose main thread has exited
    shows an empty /proc/<pid>/fd."""
    dirs, owners = [f"/proc/{pid}/fd"], [pid]
    try:
        tids = [int(name) for name in os.listdir(f"/proc/{pid}/task") if name.isdigit() and int(name) != pid]
    except OSError:
        return dirs
    for tid in tids:
        if not any(_same_files(owner, tid) for owner in owners):
            owners.append(tid)
            dirs.append(f"/proc/{pid}/task/{tid}/fd")
    return dirs


REASON_DEVICE = "GPU device open"
REASON_TOO_MANY = "too many open files to check"


def gpu_reason(pid: int, devices: set[tuple[int, int | None]], deadline: float = float("inf")) -> str | None:
    """Why the process counts as holding the GPU ("" when it does not), or None
    when the deadline passed before it was fully checked."""
    for fd_dir in _fd_dirs(pid):
        try:
            entries = os.scandir(fd_dir)
        except OSError:
            continue
        with entries:
            for seen, entry in enumerate(entries, 1):
                if seen > MAX_FDS:
                    return REASON_TOO_MANY  # checking it would take too long
                if seen % 256 == 0 and time.monotonic() > deadline:
                    return None
                try:
                    if GPU_PATH.match(os.readlink(entry.path)):
                        return REASON_DEVICE
                    info = os.stat(entry.path)
                    if stat.S_ISCHR(info.st_mode):
                        number = (os.major(info.st_rdev), os.minor(info.st_rdev))
                        if number in devices or (number[0], None) in devices:
                            return REASON_DEVICE
                except OSError:
                    continue
    return ""


def holds_gpu(pid: int, devices: set[tuple[int, int | None]], deadline: float = float("inf")) -> bool | None:
    """True when the process counts as holding the GPU, None when the deadline
    passed before it was fully checked."""
    reason = gpu_reason(pid, devices, deadline)
    return None if reason is None else bool(reason)


def kcmp_works() -> bool:
    return _same_files(os.getpid(), os.getpid())


def _state(pid: int) -> str:
    """The process state letter from /proc (R running, S sleeping, D waiting,
    T stopped, Z exited), or "?"."""
    try:
        raw = Path(f"/proc/{pid}/stat").read_text()
        return raw[raw.rindex(")") + 2]
    except (OSError, ValueError, IndexError):
        return "?"


def _command(pid: int) -> str:
    try:
        raw = Path(f"/proc/{pid}/cmdline").read_bytes()[:400]
    except OSError:
        return "?"
    return raw.replace(b"\0", b" ").decode("ascii", "replace").strip()[:80]


def _parents() -> dict[int, int]:
    parents: dict[int, int] = {}
    for candidate in _pids():
        info = _stat(candidate)
        if info:
            parents[candidate] = info[0]
    return parents


def _descendants(pid: int, parents: dict[int, int]) -> set[int]:
    result, frontier = set(), {pid}
    while frontier:
        children = {child for child, parent in parents.items() if parent in frontier} - result
        result |= children
        frontier = children
    return result


def scan(own: int, deadline: float, reasons: dict[int, str] | None = None) -> tuple[list[int], list[int], list[int]]:
    """(GPU pids, protected pids holding the GPU, unprotected pids not checked
    before the deadline). ``reasons`` is filled with why each pid counts."""
    devices = gpu_device_numbers()
    protected = protected_pids(own)
    found, protected_gpu = [], []
    pids = _pids()
    for index, pid in enumerate(pids):
        if pid == own:
            continue
        reason = gpu_reason(pid, devices, deadline) if time.monotonic() <= deadline else None
        if reason is None:
            return found, protected_gpu, [p for p in pids[index:] if p not in protected and p != own]
        if reason:
            (protected_gpu if pid in protected else found).append(pid)
            if reasons is not None:
                reasons[pid] = reason
    return found, protected_gpu, []


def stop(pids: list[int], sig: signal.Signals, own: int) -> int:
    protected = protected_pids(own)
    protected_groups = {(_stat(pid) or (0, -1, 0))[1] for pid in protected}
    parents: dict[int, int] | None = None
    signalled = 0
    for pid in pids:
        info = _stat(pid)
        pgid = info[1] if info else pid
        try:
            if pgid > 1 and pgid not in protected_groups:
                os.killpg(pgid, sig)  # the whole job, e.g. a shell pipeline
                signalled += 1
            else:
                # Shares a group with the MCP server: only this process and its children.
                parents = parents if parents is not None else _parents()
                for target in {pid} | _descendants(pid, parents):
                    if target not in protected:
                        os.kill(target, sig)
                        signalled += 1
        except ProcessLookupError:
            continue
    return signalled


def kill_one(pid: int, own: int) -> dict[str, object]:
    """The owner's kill button: SIGKILL one process of the workspace (with its
    job), never init, the MCP server or this helper."""
    if pid in protected_pids(own):
        return {"error": "that process is protected (init or the MCP server); restart the workspace instead"}
    if not Path(f"/proc/{pid}").is_dir():
        return {"error": "no such process (it may have stopped already)"}
    command = _command(pid)
    return {"killed": stop([pid], signal.SIGKILL, own), "pid": pid, "command": command}


def main(argv: list[str]) -> int:
    action = argv[0] if argv else "list"
    own = os.getpid()
    if action == "KILL-PID":
        if len(argv) != 2 or not argv[1].isdigit():
            print(json.dumps({"error": "usage: gpu_reaper KILL-PID <pid>"}))
            return 2
        print(json.dumps(kill_one(int(argv[1]), own)))
        return 0
    frozen = argv[1:] == ["--frozen"]
    if action not in ("list", "TERM", "KILL") or (argv[1:] and not (frozen and action == "KILL")):
        print(json.dumps({"error": "usage: gpu_reaper list|TERM|KILL [--frozen with KILL]|KILL-PID <pid>"}))
        return 2
    started = time.monotonic()
    reasons: dict[int, str] = {}
    found, protected_gpu, unchecked = scan(
        own, started + (FROZEN_DEADLINE_SECONDS if frozen else DEADLINE_SECONDS), reasons)
    seconds = round(time.monotonic() - started, 2)
    sample = [{"pid": pid, "command": _command(pid), "reason": reasons.get(pid, ""), "state": _state(pid)}
              for pid in found[:SAMPLE]]
    signalled = 0
    if action != "list":
        # Frozen: what could not be checked in time is killed too.
        targets = found + unchecked if frozen else found
        signalled = stop(targets, signal.SIGTERM if action == "TERM" else signal.SIGKILL, own)
    print(json.dumps({
        "count": len(found),
        "sample": sample,
        "signalled": signalled,
        "partial": bool(unchecked) and not frozen,
        "unchecked": len(unchecked),
        "protected_gpu": protected_gpu[:SAMPLE],
        "protected_sample": [{"pid": pid, "command": _command(pid), "reason": reasons.get(pid, "")}
                             for pid in protected_gpu[:SAMPLE]],
        "kcmp": kcmp_works(),
        "seconds": seconds,
    }))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
