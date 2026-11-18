"""Who is doing what in the workspace: the GPU queue, repeated requests, limits in tool texts."""

from __future__ import annotations

import json
import asyncio
import os
import time
import uuid

import pytest

from app import activity, job_runner, jobs, platform_tools, server
from app.config import settings
from app.limits import LIMITS, TRANSFER_GUIDE, size


class FakeContext:
    def __init__(self, agent: str) -> None:
        self.headers = {"user-agent": agent}


def wait_for(job_id: str, states: set[str], seconds: float = 30) -> dict:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        status = jobs.job_status(job_id)
        if status["status"] in states:
            return status
        time.sleep(0.2)
    raise AssertionError(f"job stayed {jobs.job_status(job_id)['status']}")


def fake_job(status: str, *, gpu: bool = True, created_at: float | None = None, memory: int | None = None,
             label: str = "", started_at: float | None = None) -> str:
    job_id = uuid.uuid4().hex
    directory = settings.jobs_dir / job_id
    directory.mkdir(parents=True)
    (directory / "status.json").write_text(json.dumps({
        "job_id": job_id, "status": status, "gpu": gpu, "gpu_memory_mb": memory, "label": label,
        "runner_pid": os.getpid(), "created_at": created_at or time.time(), "started_at": started_at,
        "command": "true"}))
    return job_id


@pytest.fixture
def clean_jobs(monkeypatch):
    """Only the jobs of this test count, and their made-up runners are 'alive'."""
    made: list[str] = []
    monkeypatch.setattr(job_runner, "_other_gpu_jobs", lambda job_id: [
        json.loads((settings.jobs_dir / other / "status.json").read_text()) for other in made if other != job_id])
    monkeypatch.setattr(platform_tools, "gpu_blocked", lambda path=None: None)
    monkeypatch.setattr(activity, 'gpu_state', lambda **kwargs: {
        'available':True, 'gpus':[{'memory_total_mb':24000}]})
    return made


def test_sizes_and_limits_are_stated_as_numbers():
    assert size(10 * 1024**2) == "10 MB" and size(2 * 1024**3) == "2 GB" and size(1536) == "1.5 KB" and size(12) == "12 bytes"
    assert LIMITS["workspace_write_file_max_bytes"] == settings.max_write_file_bytes
    assert size(settings.max_import_bytes) in TRANSFER_GUIDE and "admin page (Files)" in TRANSFER_GUIDE
    info = server.workspace_info()
    assert info["transfer_limits"]["workspace_import_file_max_bytes"] == settings.max_import_bytes
    assert info["transfer_guide"] == TRANSFER_GUIDE
    assert info["gpu"]["available"] is False and "reason" in info["gpu"]      # no GPU in the test container
    assert info["gpu"]["gpu_jobs_running"] == [] or isinstance(info["gpu"]["gpu_jobs_running"], list)


def test_a_file_that_is_too_large_says_what_to_use_instead():
    with pytest.raises(ValueError) as refused:
        server.workspace_write_file("too-big.bin", "x" * (settings.max_write_file_bytes + 1))
    message = str(refused.value)
    assert size(settings.max_write_file_bytes) in message and "workspace_import_file" in message and "admin page" in message


def test_caller_is_read_from_the_request():
    assert activity.caller(FakeContext("Claude-User/1.0 (+https://claude.ai)"))["client"] == "Claude"
    assert activity.caller(FakeContext("openai-mcp/1.0.0"))["client"] == "ChatGPT"
    assert activity.caller(FakeContext("codex_cli_rs/0.50 openai"))["client"] == "Codex"
    unknown = activity.caller(FakeContext("curl/8"))
    assert unknown == {"client": "unknown", "user_agent": "curl/8"}
    assert activity.caller(None)["client"] == "unknown"          # a call without request information


def test_repeated_request_does_not_run_twice():
    runs = []
    def work():
        runs.append(1)
        return {"job_id": "abc", "n": len(runs)}
    request = "request-" + uuid.uuid4().hex
    first = activity.once("tool", request, work)
    again = activity.once("tool", request, work)
    assert first == {"job_id": "abc", "n": 1} and again["n"] == 1 and again["repeated"] is True and len(runs) == 1
    assert activity.once("other-tool", request, work)["n"] == 2          # the same value for another tool is another request
    assert activity.once("tool", None, work)["n"] == 3                   # no request_id: always runs
    with pytest.raises(ValueError, match="8 to 128"):
        activity.once("tool", "short", work)
    def broken():
        raise RuntimeError("deployer is down")
    failing = "request-" + uuid.uuid4().hex
    with pytest.raises(RuntimeError):
        activity.once("tool", failing, broken)
    assert activity.once("tool", failing, work)["n"] == 4                # a failure is not remembered: the retry runs


def test_gpu_jobs_wait_for_their_turn(clean_jobs):
    now = time.time()
    me = {"created_at": now, "gpu": True}
    assert job_runner.gpu_wait_reason("f" * 32, me) is None                               # nothing else: start
    running = fake_job("running", created_at=now - 50, label="training")
    clean_jobs.append(running)
    reason = job_runner.gpu_wait_reason("f" * 32, me)
    assert running[:8] in reason and "training" in reason and "one at a time" in reason
    # A job asked for earlier goes first, also when it is still waiting itself.
    clean_jobs.clear()
    earlier = fake_job("waiting_for_gpu", created_at=now - 10)
    later = fake_job("waiting_for_gpu", created_at=now + 10)
    clean_jobs.extend([earlier, later])
    assert "ahead in the queue" in job_runner.gpu_wait_reason("f" * 32, me)
    clean_jobs.remove(earlier)
    assert job_runner.gpu_wait_reason("f" * 32, me) is None                               # only a later job waits


def test_gpu_jobs_share_the_gpu_only_when_both_state_their_memory(clean_jobs, monkeypatch):
    now = time.time()
    clean_jobs.append(fake_job("running", created_at=now - 50, memory=6000))
    monkeypatch.setattr(activity, "gpu_free_mb", lambda: 10_000)
    assert job_runner.gpu_wait_reason("f" * 32, {"created_at": now, "gpu_memory_mb": 3000}) is None
    reason = job_runner.gpu_wait_reason("f" * 32, {"created_at": now, "gpu_memory_mb": 9800})
    assert "10000 MB of GPU memory is free" in reason and "9800" in reason                 # the margin does not fit
    assert "one at a time" in job_runner.gpu_wait_reason("f" * 32, {"created_at": now})    # this job did not say
    monkeypatch.setattr(activity, "gpu_free_mb", lambda: None)
    assert "cannot be read" in job_runner.gpu_wait_reason("f" * 32, {"created_at": now, "gpu_memory_mb": 100})


def test_gpu_memory_is_checked_without_other_workspace_jobs(clean_jobs, monkeypatch):
    monkeypatch.setattr(activity, 'gpu_free_mb', lambda: 2000)
    assert '2000 MB' in job_runner.gpu_wait_reason('f' * 32, {'gpu_memory_mb': 4000})
    assert job_runner.gpu_wait_reason('f' * 32, {'gpu_memory_mb': 1000}) is None
    monkeypatch.setattr(activity, 'gpu_free_mb', lambda: None)
    assert 'cannot be read' in job_runner.gpu_wait_reason('f' * 32, {'gpu_memory_mb': 1000})


def test_gpu_starting_job_reserves_memory_before_allocating(clean_jobs, monkeypatch):
    clean_jobs.append(fake_job('starting', memory=12000))
    monkeypatch.setattr(activity, 'gpu_free_mb', lambda: 24000)
    assert '12000 MB is reserved' in job_runner.gpu_wait_reason('f' * 32, {'gpu_memory_mb': 16000})


def test_running_job_does_not_reserve_its_whole_estimate_forever(clean_jobs, monkeypatch):
    now = time.time()
    clean_jobs.append(fake_job('running', memory=10000, started_at=now-121))
    monkeypatch.setattr(activity, 'gpu_free_mb', lambda: 14000)
    # The running job's 10 GB already appears in measured usage. A 4 GB job fits.
    assert job_runner.gpu_wait_reason('f'*32, {'gpu_memory_mb':4000}) is None
    monkeypatch.setattr(activity, 'gpu_free_mb', lambda: 3000)
    assert '3000 MB' in job_runner.gpu_wait_reason('f'*32, {'gpu_memory_mb':4000})


def test_startup_reservation_uses_admission_time_not_time_spent_queued(clean_jobs, monkeypatch):
    now = time.time()
    clean_jobs.append(fake_job('running', memory=10000, created_at=now-3600, started_at=now-30))
    monkeypatch.setattr(activity, 'gpu_free_mb', lambda: 14000)
    assert '10000 MB is reserved' in job_runner.gpu_wait_reason('f'*32, {'gpu_memory_mb':4000})


def test_legacy_jobs_without_admission_time_do_not_reserve_forever(clean_jobs, monkeypatch):
    clean_jobs.append(fake_job('running', memory=10000, created_at=time.time()-3600))
    monkeypatch.setattr(activity, 'gpu_free_mb', lambda: 14000)
    assert job_runner.gpu_wait_reason('f'*32, {'gpu_memory_mb':4000}) is None


@pytest.mark.parametrize('need', [24000, 25000])
def test_impossible_gpu_requests_are_rejected_before_creating_a_job(clean_jobs, need):
    before = set(settings.jobs_dir.iterdir())
    with pytest.raises(job_runner.GPUCapacityError, match='largest detected GPU has 24000 MB'):
        jobs.start_job(command='echo must not run', gpu_memory_mb=need)
    assert set(settings.jobs_dir.iterdir()) == before


def test_capacity_uses_one_card_and_allows_the_margin_boundary(clean_jobs, monkeypatch):
    monkeypatch.setattr(activity, 'gpu_free_mb', lambda: 24000)
    assert job_runner.gpu_wait_reason('f'*32, {'gpu_memory_mb':24000-512}) is None
    monkeypatch.setattr(activity, 'gpu_state', lambda **kwargs: {
        'gpus':[{'memory_total_mb':16000},{'memory_total_mb':16000}]})
    with pytest.raises(job_runner.GPUCapacityError):
        job_runner.gpu_wait_reason('f'*32, {'gpu_memory_mb':20000})


def test_existing_oversized_job_fails_and_releases_the_queue(monkeypatch, tmp_path):
    monkeypatch.setattr(activity, 'gpu_state', lambda **kwargs: {
        'gpus':[{'memory_total_mb':24000}]})
    oversized = fake_job('queued', memory=25000)
    directory = settings.jobs_dir/oversized
    marker = tmp_path/'must-not-exist'
    (directory/'spec.json').write_text(json.dumps({
        'job_id':oversized, 'command':f'touch {marker}', 'cwd':str(tmp_path),
        'created_at':time.time(), 'gpu':True, 'gpu_memory_mb':25000}))
    # This is an already-queued spec, so the runner must revalidate it too.
    monkeypatch.setattr(platform_tools, 'gpu_blocked', lambda: 'Gaming mode is on.')
    assert asyncio.run(job_runner.run(oversized)) == 2
    result = jobs.job_status(oversized)
    assert result['status'] == 'failed' and 'cannot fit' in result['error']
    assert not marker.exists()
    # The real scanner excludes failed jobs from the queue.
    assert oversized not in [job['job_id'] for job in job_runner._other_gpu_jobs('f'*32)]


def test_export_retry_reuses_the_original_link(monkeypatch):
    calls = []
    def mint(path, ttl):
        calls.append((path, ttl))
        return {'url': 'https://files.test/original', 'expires_at': 123}
    monkeypatch.setattr(server, 'create_download', mint)
    request = uuid.uuid4().hex
    original = server.workspace_export_file('report.pdf', request_id=request)
    repeated = server.workspace_export_file('report.pdf', request_id=request)
    assert repeated['url'] == original['url'] and repeated['expires_at'] == original['expires_at']
    assert repeated['repeated'] and calls == [('report.pdf', 3600)]


def test_gaming_mode_holds_the_gpu_queue(clean_jobs, monkeypatch):
    monkeypatch.setattr(platform_tools, "gpu_blocked", lambda path=None: "Gaming mode is on.")
    assert job_runner.gpu_wait_reason("f" * 32, {"created_at": time.time()}) == "Gaming mode is on."


def test_a_reused_process_number_is_not_mistaken_for_a_running_job():
    # After a restart of the container, the old runner's number can belong to any program.
    lost = fake_job("running", gpu=True)
    assert jobs.job_status(lost)["status"] == "stopped_or_lost"
    assert lost not in [job["job_id"] for job in job_runner._other_gpu_jobs("f" * 32)]


def test_gpu_queue_end_to_end_with_real_jobs(monkeypatch):
    """Two real GPU jobs: the second waits, starts when the first ends, and a waiting job can be stopped."""
    monkeypatch.setattr(job_runner, "GPU_QUEUE_POLL_SECONDS", 0.3)
    for job in jobs.list_jobs(200):                       # leftovers of other tests must not hold the queue
        jobs.stop_job(job["job_id"], force_after_seconds=1)
    first = server.workspace_job_start("sleep 3; echo first", label="first gpu job", gpu=True, ctx=FakeContext("Claude-User"))
    assert "gpu_queue" in first
    wait_for(first["job_id"], {"running"})
    second = server.workspace_job_start("echo second", label="second", gpu_memory_mb=None, gpu=True, ctx=FakeContext("openai-mcp"))
    third = server.workspace_job_start("echo third", gpu=True)
    waiting = wait_for(second["job_id"], {"waiting_for_gpu"})
    assert "first gpu job" in waiting["waiting_for"] and waiting["started_by"]["client"] == "ChatGPT"

    seen = server.workspace_activity()
    by_id = {job["job_id"]: job for job in seen["jobs_active"]}
    assert by_id[first["job_id"]]["started_by"]["client"] == "Claude" and by_id[first["job_id"]]["label"] == "first gpu job"
    assert [job["job_id"] for job in seen["gpu"]["gpu_jobs_running"]] == [first["job_id"]]
    assert [job["job_id"] for job in seen["gpu"]["gpu_jobs_waiting"]] == [second["job_id"], third["job_id"]]

    stopped = jobs.stop_job(third["job_id"], force_after_seconds=5)                       # stop one that still waits
    assert stopped["stopped"] and wait_for(third["job_id"], {"stopped"})["note"].startswith("Stopped while waiting")
    done = wait_for(second["job_id"], {"finished"}, seconds=40)
    assert done["exit_code"] == 0 and done["waited_for_gpu_seconds"] >= 1
    assert done["started_at"] >= jobs.job_status(first["job_id"])["finished_at"] - 0.5    # never both at once
    assert jobs.job_output(second["job_id"])["stdout"].strip() == "second"


def test_a_job_without_the_gpu_flag_starts_at_once_and_a_request_id_starts_one_job():
    request = "job-" + uuid.uuid4().hex
    first = server.workspace_job_start("echo plain", label="plain", request_id=request)
    again = server.workspace_job_start("echo plain", label="plain", request_id=request)
    assert again["job_id"] == first["job_id"] and again["repeated"] is True
    done = wait_for(first["job_id"], {"finished"})
    assert done["gpu"] is False and done["started_by"] == {"client": "unknown", "user_agent": ""}
    assert sum(1 for job in jobs.list_jobs(200) if job.get("label") == "plain" and job["command"] == "echo plain"
               and job["created_at"] >= done["created_at"]) == 1


def test_running_commands_are_listed_with_who_started_them():
    with activity.track("python train.py --epochs 3", ".", {"client": "Claude"}) as entry:
        running = activity.commands()["running"]
        assert [item["id"] for item in running] == [entry["id"]] and running[0]["started_by"] == {"client": "Claude"}
    after = activity.commands()
    assert after["running"] == [] and after["finished_last_15_minutes"][0]["command"] == "python train.py --epochs 3"


def test_app_tools_pass_the_new_fields_to_the_deployer(monkeypatch, workspace_root):
    calls = []
    def fake(method, path, **kwargs):
        calls.append((method, path, kwargs))
        return {"ok": True}
    monkeypatch.setattr(platform_tools, "call", fake)
    assert server.app_call_read("notes", "list_notes", {"project": 3}) == {"ok": True}
    assert calls[-1] == ("POST", "/v1/apps/notes/call", {"json": {"action": "list_notes", "arguments": {"project": 3}, "write": False}})
    server.app_call_write("notes", "add_note", {"text": "hi"})
    assert calls[-1][2]["json"]["write"] is True

    site = workspace_root / "apps" / "limits-demo"
    site.mkdir(parents=True, exist_ok=True)
    (site / "index.html").write_text("<h1>hi</h1>")
    request = "deploy-" + uuid.uuid4().hex
    server.app_deploy("limits-demo", data_change="irreversible", request_id=request)
    assert calls[-1][2]["params"] == {"note": "", "kind": "static", "data_change": "irreversible"}
    count = len(calls)
    assert server.app_deploy("limits-demo", data_change="irreversible", request_id=request)["repeated"] is True
    assert len(calls) == count                                                             # not uploaded twice
    server.app_deploy("limits-demo")
    assert "data_change" not in calls[-1][2]["params"]

    ask = "ask-" + uuid.uuid4().hex
    server.app_request("limits-demo", "rollback", {"version": "previous"}, request_id=ask)
    server.app_request("limits-demo", "rollback", {"version": "previous"}, request_id=ask)
    assert [call[1] for call in calls].count("/v1/apps/limits-demo/requests") == 1
