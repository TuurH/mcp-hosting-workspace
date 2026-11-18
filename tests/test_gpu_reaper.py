"""The GPU reaper's process checks, run against real child processes.

/dev/zero (character device 1:5) stands in for the GPU; the reaper finds it by
its device number, as it finds /dev/dxg or /dev/nvidia*.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time

import pytest

from mcp_hosting_platform.deployer import gpu_reaper

STAND_IN = {(1, 5)}

OWN_TABLE_THREAD = """
import ctypes, threading, time
def worker():
    assert ctypes.CDLL(None).unshare(0x400) == 0  # CLONE_FILES: this thread gets a file table of its own
    f = open("/dev/zero", "rb")
    print("ready", flush=True)
    time.sleep(600)
threading.Thread(target=worker, daemon=True).start()
time.sleep(600)
"""

MAIN_THREAD_GONE = """
import ctypes, threading, time
def worker():
    f = open("/dev/zero", "rb")
    print("ready", flush=True)
    time.sleep(600)
threading.Thread(target=worker).start()
time.sleep(0.5)
ctypes.CDLL(None).pthread_exit(None)  # the main thread exits, the process lives on
"""

MANY_FILES = """
import os, time
fd = os.open("/dev/null", os.O_RDONLY)
fds = [os.dup(fd) for _ in range(600)]
print("ready", flush=True)
time.sleep(600)
"""


def _state(pid: int) -> str:
    raw = open(f"/proc/{pid}/stat").read()
    return raw[raw.rindex(")") + 2]


@pytest.fixture
def child():
    started: list[subprocess.Popen] = []

    def start(code: str) -> int:
        process = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE, text=True)
        started.append(process)
        assert process.stdout is not None and process.stdout.readline().strip() == "ready"
        time.sleep(0.6)
        return process.pid

    yield start
    for process in started:
        process.kill()
        process.wait()


def test_thread_with_its_own_file_table_is_found(child):
    pid = child(OWN_TABLE_THREAD)
    leader_only = [os.readlink(f"/proc/{pid}/fd/{fd}") for fd in os.listdir(f"/proc/{pid}/fd")]
    assert "/dev/zero" not in leader_only  # invisible through the main thread
    assert gpu_reaper.holds_gpu(pid, STAND_IN) is True


def test_process_whose_main_thread_exited_is_found(child):
    pid = child(MAIN_THREAD_GONE)
    deadline = time.monotonic() + 10
    while _state(pid) != "Z":  # wait until the main thread has really exited
        assert time.monotonic() < deadline, "the main thread did not exit"
        time.sleep(0.05)
    try:
        leader = os.listdir(f"/proc/{pid}/fd")
    except PermissionError:
        leader = []  # without root, an exited main thread cannot be read at all
    assert leader == []  # nothing to see through the leader
    assert gpu_reaper.holds_gpu(pid, STAND_IN) is True


def test_too_many_open_files_counts_as_holding_the_gpu(child, monkeypatch):
    pid = child(MANY_FILES)
    assert gpu_reaper.holds_gpu(pid, STAND_IN) is False
    monkeypatch.setattr(gpu_reaper, "MAX_FDS", 500)
    assert gpu_reaper.holds_gpu(pid, STAND_IN) is True


def test_the_deadline_is_checked_inside_one_process(child):
    pid = child(MANY_FILES)
    assert gpu_reaper.holds_gpu(pid, STAND_IN, deadline=time.monotonic() - 1) is None
