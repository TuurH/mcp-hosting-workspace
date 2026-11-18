"""Gaming mode and the workspace's GPU access.

Turning gaming mode on stops GPU work in the workspace, lowers the workspace's
CPU and memory limits with ``docker update`` and drops the Linux page cache so
Windows gets memory back. Turning it off restores the saved limits. The AI may
turn it on; only the owner (full API or the Windows CLI) turns it off.

Blocking new GPU work inside the workspace is cooperative (the AI has sudo
there): the deployer publishes /control/state.json, which the workspace tools
read. Finding and stopping GPU processes is done from outside, by a helper
container that shares only the workspace's process list (gpu_reaper.py), and
is repeated by the reconcile loop while gaming mode is on.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

from ..common.docker_client import DockerClient, DockerError
from .config import DeployerSettings
from .db import Database

logger = logging.getLogger("mcp-hosting.deployer.gaming")

DROP_CACHES = "sync; echo 1 > /proc/sys/vm/drop_caches"


class Gaming:
    def __init__(self, settings: DeployerSettings, db: Database, docker: DockerClient | None) -> None:
        from .resources import WorkspaceResources
        self.settings = settings
        self.db = db
        self.docker = docker
        self.lock = threading.RLock()
        # The newest state while it cannot be saved (a full disk): gaming mode
        # keeps acting on it until a save works again or the deployer restarts.
        self._unsaved: dict[str, Any] | None = None
        self.resources = WorkspaceResources(self)
        self.runtime = None

    def _apply_app_limits(self, state: dict[str, Any]) -> bool:
        if self.runtime:
            try:
                self.runtime.apply_gpu_policy()
                self.runtime.apply_cpu_limits()
            except Exception as exc:
                self._add_error(state, f"app CPU limits could not be applied: {exc}")
                self._log(state, state['error'])
                return False
        return True

    # ------------------------------------------------------------------ state
    def state(self) -> dict[str, Any]:
        if self._unsaved is not None:
            value: Any = json.loads(json.dumps(self._unsaved))
        else:
            value = self.db.get_setting("gaming_state")
        if not isinstance(value, dict):
            value = {}
        value.setdefault("state", "off")
        value.setdefault("log", [])
        saved = value.get("saved_limits")
        if isinstance(saved, dict) and "limits" not in saved:
            # Stored by the first Phase 1 build: no container id. It belongs to
            # the workspace that is running now.
            value["saved_limits"] = {"id": None, "limits": saved}
        return value

    def _save_state(self, state: dict[str, Any]) -> bool:
        """Save the state; False when that failed (a full disk, which the AI can
        cause). Gaming mode carries on stopping GPU work, but never pauses the
        workspace unless it could record that it did."""
        state["log"] = state.get("log", [])[-30:]
        try:
            self.db.set_setting("gaming_state", state)
            self._unsaved = None
            return True
        except (sqlite3.Error, OSError) as exc:
            logger.error("gaming: saving the state failed: %s", exc)
            self._unsaved = json.loads(json.dumps(state))
            return False

    def docker_health(self) -> str:
        if self.docker is None:
            return "no Docker socket mounted"
        return self.docker.ping()

    def _log(self, state: dict[str, Any], message: str) -> bool:
        logger.info("gaming: %s", message)
        state.setdefault("log", []).append({"time": time.time(), "message": message})
        return self._save_state(state)

    def workspace_gpu_allowed(self) -> bool:
        return self.state()["state"] == "off" and bool(self.db.get_setting("workspace_gpu_allowed"))

    def _publish(self, state: dict[str, Any]) -> None:
        """write_control, reporting a failure (for example a full disk) instead of
        stopping the Docker steps that follow."""
        try:
            self.write_control()
        except OSError as exc:
            self._add_error(state, f"writing the control file failed: {exc}")
            logger.error("gaming: writing the control file failed: %s", exc)

    def write_control(self) -> None:
        state = self.state()
        payload = {
            "gaming": state["state"] != "off",
            "workspace_gpu_allowed": self.workspace_gpu_allowed(),
            "updated_at": time.time(),
            "message": (
                "Gaming mode is on: the GPU is reserved for the owner. Do not start GPU work."
                if state["state"] != "off"
                else ("" if self.workspace_gpu_allowed() else "The owner has switched off GPU access for the workspace.")
            ),
        }
        directory = self.settings.control_dir
        directory.mkdir(parents=True, exist_ok=True)
        tmp = directory / ".state.json.tmp"
        tmp.write_text(json.dumps(payload, indent=2))
        os.chmod(tmp, 0o644)
        os.replace(tmp, directory / "state.json")

    # ------------------------------------------------------------------ workspace helpers
    def _reaper(self, action: str, frozen: bool = False, args: tuple[str, ...] = (),
                strict: bool = True) -> dict[str, Any]:
        """Run gpu_reaper in a helper container. Raises DockerError when the
        result cannot be trusted (failed, unreadable, or, with ``strict``, cut
        short)."""
        assert self.docker is not None
        code, out = self.docker.run_capture(
            {
                "Image": self.settings.helper_image,
                "User": "0",
                "Entrypoint": ["python", "-m", "mcp_hosting_platform.deployer.gpu_reaper"],
                "Cmd": [action, "--frozen"] if frozen else [action, *args],
                "Labels": {"mcp-hosting-workspace.platform": "helper"},
                "HostConfig": {
                    "PidMode": f"container:{self.settings.workspace_container}",
                    "NetworkMode": "none",
                    "ReadonlyRootfs": True,
                    "CapDrop": ["ALL"],
                    "CapAdd": ["KILL", "SYS_PTRACE", "DAC_READ_SEARCH"],  # read other users' /proc/<pid>/fd
                    "SecurityOpt": ["no-new-privileges:true"],
                    "Memory": 128 * 1024 * 1024,
                    "PidsLimit": 32,
                },
            },
            timeout=120 if frozen else 60,
            max_output=64 * 1024,
        )
        try:
            result = json.loads(out.strip().splitlines()[-1]) if out.strip() else {}
        except ValueError:
            result = {}
        if code != 0 or ("count" not in result and "killed" not in result and "error" not in result):
            raise DockerError(0, f"GPU process check failed (exit {code})")
        if strict and "count" not in result:
            raise DockerError(0, str(result.get("error") or "GPU process check failed"))
        if strict and result.get("partial"):
            raise DockerError(0, "GPU process check did not finish in time")
        return result

    # ------------------------------------------------------------------ the owner's GPU page
    def gpu_report(self) -> dict[str, Any]:
        """What holds the GPU in the workspace right now, for the admin page."""
        state = self.state()
        report: dict[str, Any] = {"gaming": state["state"], "frozen_by_platform": bool(state.get("frozen")),
                                  "held": state.get("frozen") == "held", "error": state.get("error")}
        if self.docker is None:
            return {**report, "check_error": "no Docker socket mounted"}
        try:
            if not self.docker.container_exists(self.settings.workspace_container):
                return {**report, "check_error": "the workspace container does not exist"}
            info = self.docker.inspect_container(self.settings.workspace_container)
            report["paused"] = bool(info.get("State", {}).get("Paused"))
            report.update(self._reaper("list", strict=False))
        except DockerError as exc:
            report["check_error"] = str(exc)
        return report

    def kill_gpu_process(self, pid: int, actor: str) -> dict[str, Any]:
        """The owner kills one process in the workspace (and its job)."""
        if self.docker is None:
            raise DockerError(0, "no Docker socket mounted")
        result = self._reaper("KILL-PID", args=(str(int(pid)),), strict=False)
        self.db.audit(actor, "gpu.kill", {"pid": pid, "result": result})
        if result.get("error"):
            raise DockerError(0, str(result["error"]))
        return result

    def kill_all_gpu(self, actor: str) -> dict[str, Any]:
        """The owner kills every process that holds the GPU."""
        if self.docker is None:
            raise DockerError(0, "no Docker socket mounted")
        result = self._reaper("KILL", strict=False)
        self.db.audit(actor, "gpu.kill_all", {"count": result.get("count"), "signalled": result.get("signalled")})
        return result

    @staticmethod
    def _add_error(state: dict[str, Any], message: str) -> None:
        current = state.get("error") or ""
        if message not in current:  # the same problem found twice in one pass
            state["error"] = f"{current}; {message}" if current else message

    def _note_protected(self, state: dict[str, Any], result: dict[str, Any]) -> None:
        if result.get("protected_gpu"):
            message = ("the MCP server process holds the GPU; restart the workspace to free it "
                       "(docker compose restart workspace)")
            self._add_error(state, message)
            self._log(state, message)

    def _frozen_kill(self, state: dict[str, Any], reason: str) -> None:
        """Fallback when the normal check fails: freeze the workspace so nothing in
        it can react, kill the GPU processes (and whatever could not be checked
        in time), then let it run again. When even that check cannot run (for
        example a full disk), the workspace stays frozen ("held") until a later
        check succeeds or gaming mode is turned off."""
        assert self.docker is not None
        workspace = self.settings.workspace_container
        paused = bool(self.docker.inspect_container(workspace).get("State", {}).get("Paused"))
        # A pause made by hand is already a freeze, and stays: only a pause this
        # code made (marked before pausing, so a restart still resumes it) is undone.
        ours = not paused or bool(state.get("frozen"))
        already_held = paused and state.get("frozen") == "held"  # its marker is saved already
        if ours:
            state["frozen"] = state.get("frozen") or True
        recorded = self._log(state, f"{reason}; " + ("freezing the workspace to stop GPU work" if ours
                                                     else "the workspace is paused by hand; stopping GPU work in it"))
        if not paused and not recorded:
            # Without the saved marker nothing would know to resume it later.
            state.pop("frozen", None)
            raise DockerError(0, "the platform cannot save its state (is the disk full?), so it did not "
                                 "freeze the workspace; GPU work may still be running")
        if not paused:
            try:
                self.docker.pause_container(workspace)
            except DockerError:
                try:
                    paused = bool(self.docker.inspect_container(workspace).get("State", {}).get("Paused"))
                except DockerError as again:
                    paused = again.status != 404  # unknown: keep the marker; _resume checks later
                if not paused:  # e.g. the workspace was stopped or removed: nothing to resume later
                    state.pop("frozen", None)
                    self._save_state(state)
                raise
        try:
            result = self._reaper("KILL", frozen=True)
        except DockerError as exc:
            if ours:
                state["frozen"] = "held"
                if self._save_state(state) or already_held:
                    raise DockerError(0, f"GPU check failed on the frozen workspace ({exc}); it stays paused "
                                         "until a check succeeds or gaming mode is turned off") from exc
                self._resume(state)  # "held" could not be recorded: do not leave it frozen
                raise DockerError(0, f"GPU check failed on the frozen workspace ({exc}) and the state cannot be "
                                     "saved; it was resumed, GPU work may still be running") from exc
            raise DockerError(0, f"GPU check failed on the paused workspace ({exc}); "
                                 "GPU work may still be running") from exc
        if ours:
            self._resume(state)
        self._note_protected(state, result)
        if result.get("unchecked"):
            self._log(state, f"{result['unchecked']} process(es) could not be checked in time and were killed")
        time.sleep(1)
        left = self._reaper("list")["count"]
        self._log(state, f"GPU processes killed ({result['signalled']} signalled)" if not left
                  else f"{left} GPU process(es) still running")
        if left:
            raise DockerError(0, f"{left} GPU process(es) could not be stopped")

    def _resume(self, state: dict[str, Any]) -> None:
        """Unpause a workspace this code froze. Only this code pauses it; a pause
        made by hand is left alone."""
        assert self.docker is not None
        if not state.get("frozen"):
            return
        try:
            info = self.docker.inspect_container(self.settings.workspace_container)
        except DockerError as exc:
            if exc.status != 404:
                raise
            info = {}  # the workspace is gone: nothing to resume
        if info.get("State", {}).get("Paused"):
            self.docker.unpause_container(self.settings.workspace_container)
        state.pop("frozen", None)
        self._save_state(state)

    def _stop_gpu_work(self, state: dict[str, Any]) -> None:
        if state.get("frozen") == "held":
            self._frozen_kill(state, "the workspace is still frozen from an unfinished GPU stop")
            return
        try:
            if self._stop_normally(state):
                return
            reason = "GPU processes survived KILL"
        except DockerError as exc:
            reason = str(exc)
        self._frozen_kill(state, reason)

    def _stop_normally(self, state: dict[str, Any]) -> bool:
        """TERM, grace period, KILL. False when something is still running."""
        first = self._reaper("TERM")  # finds and signals in one run
        self._note_protected(state, first)
        if not first["count"]:
            self._log(state, "no GPU processes in the workspace")
            return True
        self._log(state, f"stopping {first['count']} GPU process(es): "
                  + "; ".join(str(p["command"])[:60] for p in first["sample"][:5]))
        grace = float(self.db.get_setting("gaming").get("stop_grace_seconds", 15))
        deadline = time.monotonic() + grace
        while True:
            if not self._reaper("list")["count"]:
                self._log(state, "GPU processes stopped")
                return True
            if time.monotonic() >= deadline:
                break
            time.sleep(1)
        self._reaper("KILL")
        time.sleep(1)
        if not self._reaper("list")["count"]:
            self._log(state, "GPU processes killed")
            return True
        return False

    def _inspect(self) -> tuple[str, dict[str, int]]:
        assert self.docker is not None
        info = self.docker.inspect_container(self.settings.workspace_container)
        host = info["HostConfig"]
        return info["Id"], {
            "NanoCpus": int(host.get("NanoCpus") or 0),
            "Memory": int(host.get("Memory") or 0),
            "MemorySwap": int(host.get("MemorySwap") or 0),
        }

    def _targets(self) -> tuple[int, int]:
        config = self.db.get_setting("gaming")
        return int(float(config["workspace_cpus"]) * 1_000_000_000), int(float(config["workspace_memory_gb"]) * 1024**3)

    def _apply_gaming_limits(self, state: dict[str, Any]) -> None:
        """Set the gaming limits once: when gaming mode starts, when the workspace
        was recreated, or when the owner changed the gaming settings."""
        assert self.docker is not None
        saved = state["saved_limits"]["limits"]
        target_cpus, target_memory = self._targets()
        usage = self.docker.memory_usage(self.settings.workspace_container)
        # Lowering the limit below what is in use makes the kernel kill processes.
        safe_memory = max(target_memory, int(usage * 1.3))
        if saved["Memory"]:
            safe_memory = min(safe_memory, saved["Memory"])
        cpus = target_cpus if not saved["NanoCpus"] else min(target_cpus, saved["NanoCpus"])
        self.docker.update_container(self.settings.workspace_container,
                                     Memory=safe_memory, MemorySwap=safe_memory, NanoCpus=cpus)
        state["applied_targets"] = [target_cpus, target_memory]
        note = "" if safe_memory == target_memory else f" (memory kept at {safe_memory / 1024**3:.1f} GB because of current use)"
        self._log(state, f"workspace limited to {cpus / 1e9:g} CPUs and {safe_memory / 1024**3:.1f} GB{note}")

    def _drop_caches(self, state: dict[str, Any]) -> None:
        assert self.docker is not None
        try:
            code = self.docker.run_once(
                {
                    "Image": self.settings.helper_image,
                    "User": "0",
                    "Entrypoint": ["sh", "-c"],
                    "Cmd": [DROP_CACHES],
                    "HostConfig": {"Privileged": True, "NetworkMode": "none"},
                    "Labels": {"mcp-hosting-workspace.platform": "helper"},
                },
                timeout=60,
            )
            self._log(state, "page cache dropped" if code == 0 else f"dropping the page cache failed ({code})")
        except DockerError as exc:
            self._log(state, f"dropping the page cache failed: {exc}")

    # ------------------------------------------------------------------ transitions
    def _remember_limits(self, state: dict[str, Any]) -> bool:
        """Make sure saved_limits belongs to the running workspace. Returns True
        when it was (re)taken now, i.e. the gaming limits must be applied."""
        container_id, limits = self._inspect()
        saved = state.get("saved_limits")
        if saved and saved.get("id") is None:
            saved["id"] = container_id  # the first build's format: same container
            return False
        if not saved or saved.get("id") != container_id:
            state["saved_limits"] = {"id": container_id, "limits": limits}
            state.pop("applied_targets", None)  # this container has no gaming limits yet
            return True
        return False

    def _limit(self, state: dict[str, Any]) -> bool:
        """Apply the gaming limits when this workspace does not have them yet or
        the owner changed them. Never again otherwise: the AI can turn gaming mode
        on repeatedly, and a new run would raise memory to its use plus 30%.
        Returns True when the workspace was new to gaming mode (recreated)."""
        retaken = self._remember_limits(state)
        if retaken and not self._save_state(state):
            # Without the saved normal limits, turning gaming mode off could not
            # give them back.
            raise DockerError(0, "the workspace's normal limits cannot be saved (is the disk full?), "
                                 "so they were not lowered")
        if retaken or state.get("applied_targets") != list(self._targets()):
            try:
                self._apply_gaming_limits(state)
            except DockerError as exc:
                raise DockerError(0, f"lowering the workspace limits failed: {exc}") from exc
        return retaken

    def enter(self, source: str, actor: str) -> dict[str, Any]:
        """Turn gaming mode on. The result has ok=False (and an error) when the
        workspace could not be stopped or limited."""
        with self.lock:
            state = self.state()
            if state["state"] == "on" and not state.get("error"):
                return {**state, "ok": True}
            state.update({"state": "entering", "since": time.time(), "source": source, "error": None})
            self._log(state, f"turning on ({source}, by {actor})")
            self._publish(state)  # tell the workspace tools first
            if self.docker is None:
                state["error"] = "no Docker access: only the workspace GPU flag was changed"
            else:
                try:
                    if state.get("frozen") is True:
                        self._resume(state)  # left frozen by an earlier failed unpause ("held" is retried below)
                    if self.docker.container_exists(self.settings.workspace_container):
                        # Limits first: they apply even if stopping GPU work fails.
                        try:
                            self._limit(state)
                        except DockerError as exc:
                            state["error"] = str(exc)
                        self._stop_gpu_work(state)
                    self._drop_caches(state)
                except DockerError as exc:
                    self._add_error(state, str(exc))
            if state.get("error"):
                self._log(state, f"error: {state['error']}")
            state["state"] = "on"  # the GPU flag stays on even when limiting failed
            self._log(state, "gaming mode on" + (" (with errors)" if state.get("error") else ""))
            self._apply_app_limits(state)
            self._publish(state)
            if not self._save_state(state):
                self._add_error(state, "gaming mode is on, but that could not be saved (is the disk full?); "
                                       "it stays on until the deployer restarts")
            self.db.audit(actor, "gaming.on", {"source": source, "error": state.get("error")})
            return {**state, "ok": not state.get("error")}  # also right when saving failed (full disk)

    def leave(self, actor: str) -> dict[str, Any]:
        with self.lock:
            state = self.state()
            if state["state"] == "off":
                return {**state, "ok": True}
            state.update({"state": "leaving", "error": None})
            self._log(state, f"turning off (by {actor})")
            saved = state.get("saved_limits")
            if self.docker is not None and state.get("frozen"):
                try:
                    self._resume(state)
                except DockerError as exc:
                    state["error"] = f"resuming the workspace failed: {exc}"
                    self._log(state, state["error"])
                    self._publish(state)
                    return {**state, "ok": False}  # still "leaving": reconcile retries
            if self.docker is not None and saved:
                try:
                    if self.docker.container_exists(self.settings.workspace_container):
                        container_id, _ = self._inspect()
                        if saved.get("id") in (None, container_id):
                            limits = saved["limits"]
                            restore = {k: v for k, v in limits.items() if v}
                            if restore:
                                self.docker.update_container(self.settings.workspace_container, **restore)
                            if len(restore) < len(limits):
                                self._log(state, "the workspace had no limit before; recreate it to remove "
                                                 "the gaming limit: docker compose up -d --force-recreate workspace")
                            self._log(state, "workspace limits restored")
                        else:
                            self._log(state, "the workspace was recreated meanwhile; it already has its normal limits")
                except DockerError as exc:
                    # Keep the saved limits and the "leaving" state: reconcile retries.
                    state["error"] = f"restoring the workspace limits failed: {exc}"
                    self._log(state, state["error"])
                    self.db.audit(actor, "gaming.off", {"error": state["error"]})
                    self._publish(state)
                    return {**state, "ok": False}
            for key in ("saved_limits", "applied_targets"):
                state.pop(key, None)
            state.update({"state": "off", "since": time.time(), "source": None, "error": None})
            self._log(state, "gaming mode off")
            self._apply_app_limits(state)
            self.db.audit(actor, "gaming.off", {})
            self._publish(state)
            if not self._save_state(state):
                # Off from now on, but the database still says on: a deployer
                # restart before a save works would turn it on again.
                self._add_error(state, "gaming mode is off, but that could not be saved (is the disk full?); "
                                       "if the deployer restarts before it can be saved, it comes back on")
            return {**state, "ok": not state.get("error")}

    def reconcile(self) -> None:
        """Every minute: keep gaming mode in force (recreated workspace, changed
        settings, new GPU work) and finish a turn-off whose restore failed."""
        if not self.lock.acquire(timeout=5):
            return  # a transition is running
        try:
            state = self.state()
            control_error = None
            try:
                self.write_control()
            except OSError as exc:  # e.g. a full disk: the Docker steps below still run
                control_error = f"writing the control file failed: {exc}"
                logger.error("gaming reconcile: %s", control_error)
            if self.docker is None:
                return
            apps_ok = self._apply_app_limits(state)
            held = state.get("frozen") == "held" and state["state"] in ("on", "entering")
            if state.get("frozen") and not held:
                # Left frozen by a failed unpause or a restart during a GPU stop,
                # whatever the gaming state is now. ("held": still frozen because
                # GPU work could not be checked; retried below.)
                try:
                    self._resume(state)
                    self._log(state, "workspace was still frozen; resumed it")
                except DockerError as exc:
                    logger.warning("gaming reconcile: resuming the workspace failed: %s", exc)
                    state["error"] = f"resuming the workspace failed: {exc}"
                    self._save_state(state)
                    return  # try again next minute; nothing else makes sense while it is frozen
            if state["state"] == "leaving":
                self.leave("reconcile")  # the lock is re-entrant
                return
            if state["state"] == "entering":
                # The deployer stopped while turning gaming mode on: finish it.
                self.enter(str(state.get("source") or "reconcile"), "reconcile")
                return
            if state["state"] != "on":
                if state["state"] == "off" and state.get("error") and not control_error and not state.get("frozen") and apps_ok:
                    state["error"] = None  # e.g. a control file that could not be written then: it is now
                    self._save_state(state)
                return
            try:
                if not self.docker.container_exists(self.settings.workspace_container):
                    return
                info = self.docker.inspect_container(self.settings.workspace_container)
                if not info.get("State", {}).get("Running", True):
                    return  # a stopped workspace runs nothing; checked again once it runs
                if apps_ok:
                    state["error"] = None  # this pass reports its own problems
                if control_error:
                    self._add_error(state, control_error)
                try:
                    if self._limit(state):  # no-op unless recreated or the owner changed the settings
                        self._log(state, "workspace was recreated during gaming mode; limits applied again")
                except DockerError as exc:
                    self._add_error(state, str(exc))
                    self._log(state, f"error: {exc}")
            except DockerError as exc:
                logger.warning("gaming reconcile: %s", exc)
                return
            stopped, reason = 0, None
            try:
                if held:
                    raise DockerError(0, "the workspace is still frozen from an unfinished GPU stop")
                found = self._reaper("TERM")
                self._note_protected(state, found)
                stopped = found["count"]
                if stopped:
                    time.sleep(3)
                    self._reaper("KILL")  # gaming is on: work was already asked to stop
                    time.sleep(1)
                    if self._reaper("list")["count"]:
                        reason = "GPU processes survived KILL"
            except DockerError as exc:
                logger.warning("gaming reconcile: %s", exc)
                reason = f"GPU check failed ({exc})"
            try:
                if reason:
                    self._frozen_kill(state, reason)  # raises when something is still running
                if stopped:
                    self._log(state, f"gaming mode: stopped {stopped} new GPU process(es)")
                self._save_state(state)
            except DockerError as again:
                self._add_error(state, str(again))
                self._log(state, f"error: {again}")
        finally:
            self.lock.release()
