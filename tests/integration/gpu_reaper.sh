#!/usr/bin/env bash
# The GPU reaper, run the way gaming mode runs it, against a stand-in workspace:
# init + a long-running "MCP server" (the first child of init) whose child holds
# the stand-in "GPU" (/dev/zero, device 1:5), a job in its own process group,
# a job that opens the same device through a node it made under another name
# (only the device number gives it away), a thread with a file table of its own
# and a process whose main thread has exited.
#
#   tests/integration/gpu_reaper.sh [image]
set -euo pipefail
IMAGE="${1:-mcp-hosting-workspace/platform:local}"
NAME="mcp-hosting-reaper-target-$$"
cleanup() { docker rm -f "$NAME" >/dev/null 2>&1 || true; }
trap cleanup EXIT

# SYS_ADMIN only so the stand-in can call unshare(CLONE_FILES); in the real
# workspace a raw clone() without CLONE_FILES gives a thread its own table too.
docker run -d --name "$NAME" --init --network none --user 0 --cap-add SYS_ADMIN "$IMAGE" sh -c '
  python -c "import time; f = open(\"/dev/zero\", \"rb\"); time.sleep(600)" &
  setsid python -c "import time; f = open(\"/dev/zero\", \"rb\"); time.sleep(600)" &
  mknod /tmp/not-a-gpu c 1 5 && setsid python -c "import time; f = open(\"/tmp/not-a-gpu\", \"rb\"); time.sleep(600)" &
  setsid python -c "
import os, threading, time
def worker():
    os.unshare(os.CLONE_FILES)
    f = open(\"/dev/zero\", \"rb\")
    time.sleep(600)
threading.Thread(target=worker, daemon=True).start()
time.sleep(600)" &
  setsid python -c "
import ctypes, threading, time
def worker():
    f = open(\"/dev/zero\", \"rb\")
    time.sleep(600)
threading.Thread(target=worker).start()
time.sleep(0.5)
ctypes.CDLL(None).pthread_exit(None)" &
  exec sleep 600' >/dev/null
sleep 3

reaper() {
  docker run --rm --pid "container:$NAME" --network none --read-only --user 0 \
    --cap-drop ALL --cap-add KILL --cap-add SYS_PTRACE --cap-add DAC_READ_SEARCH --security-opt no-new-privileges:true \
    -e GPU_REAPER_DEVICES='^/dev/zero$' -e GPU_REAPER_TEST_DEVICES='1:5' -e GPU_REAPER_TEST_DEADLINE="${DEADLINE:-}" \
    --entrypoint python "$IMAGE" -m mcp_hosting_platform.deployer.gpu_reaper "$@"
}
field() { python3 -c "import json,sys; print(json.load(sys.stdin)['$1'])"; }

count() { python3 -c 'import json,sys; print(json.load(sys.stdin)["count"])'; }
found="$(reaper list | count)"
[ "$found" = 5 ] || { echo "FAIL expected 5 GPU processes, found $found"; exit 1; }
echo "ok   finds all five GPU processes (renamed device node, own-table thread, exited main thread)"
listing="$(reaper list)"
printf '%s' "$listing" | python3 -c 'import json,sys; r=json.load(sys.stdin); assert all(p["reason"] == "GPU device open" and p["state"] for p in r["sample"]) and "kcmp" in r and r["seconds"] >= 0, r' \
  || { echo "FAIL the listing lacks reasons, states or scan info"; exit 1; }
echo "ok   the listing says why each process counts, and how the scan went"
victim="$(printf '%s' "$listing" | python3 -c 'import json,sys; print(json.load(sys.stdin)["sample"][0]["pid"])')"
[ "$(reaper KILL-PID "$victim" | field killed)" -ge 1 ] || { echo "FAIL the kill button did not kill $victim"; exit 1; }
sleep 1
[ "$(reaper list | count)" = 4 ] || { echo "FAIL killing one process did not leave four"; exit 1; }
reaper KILL-PID 1 | grep -q protected || { echo "FAIL init could be killed with the kill button"; exit 1; }
echo "ok   the kill button stops one process and refuses init"
reaper TERM >/dev/null
sleep 1
left="$(reaper list | count)"
[ "$left" = 0 ] || { echo "FAIL $left GPU process(es) survived TERM"; exit 1; }
echo "ok   TERM stops them"
docker exec "$NAME" sh -c 'kill -0 1' && [ "$(docker inspect -f '{{.State.Running}}' "$NAME")" = true ] \
  || { echo "FAIL the stand-in MCP server or init was killed"; exit 1; }
echo "ok   init and the MCP server stand-in survive"

# Output stays small whatever the workspace does (1000 processes with long names).
docker exec -d "$NAME" python -c "
import os, time
for _ in range(400):
    if os.fork() == 0:
        fd = os.open('/dev/zero', os.O_RDONLY)
        os.set_inheritable(fd, True)  # keep holding the device after exec
        os.execvp('sleep', ['sleep' + 'é' * 300, '600'])
time.sleep(600)"
sleep 5
result="$(reaper list)"
many="$(printf '%s' "$result" | count)"
bytes="$(printf '%s' "$result" | wc -c)"
[ "$many" -ge 300 ] || { echo "FAIL expected hundreds of GPU processes, found $many"; exit 1; }
[ "$bytes" -lt 20000 ] || { echo "FAIL reaper output is $bytes bytes"; exit 1; }
echo "ok   output stays small ($bytes bytes for $many GPU processes with long names)"
reaper KILL >/dev/null
sleep 1
[ "$(reaper list | count)" = 0 ] || { echo "FAIL the flood survived KILL"; exit 1; }
echo "ok   KILL stops all of them"

# A flood of open files makes the check run out of time. A normal run says so
# ("partial"); a run on the frozen workspace kills what it could not check.
docker exec -d "$NAME" python -c "
import os, time
fd = os.open('/dev/null', os.O_RDONLY)
for _ in range(20):
    if os.fork() == 0:
        fds = [os.dup(fd) for _ in range(15000)]
        time.sleep(600)
time.sleep(600)"
sleep 4
docker exec -d "$NAME" setsid python -c "import time; f = open('/dev/zero', 'rb'); time.sleep(600)"
sleep 2
partial="$(DEADLINE=0.3 reaper list | field partial)"
[ "$partial" = True ] || { echo "FAIL a check cut short was not reported as partial"; exit 1; }
echo "ok   a check that runs out of time says so"
docker pause "$NAME" >/dev/null
frozen="$(DEADLINE=0.3 reaper KILL --frozen)"
docker unpause "$NAME" >/dev/null
[ "$(printf '%s' "$frozen" | field partial)" = False ] && [ "$(printf '%s' "$frozen" | field unchecked)" -gt 0 ] \
  || { echo "FAIL frozen run: $frozen"; exit 1; }
sleep 1
after="$(reaper list)"
[ "$(printf '%s' "$after" | field count)" = 0 ] && [ "$(printf '%s' "$after" | field partial)" = False ] \
  || { echo "FAIL GPU work or the flood survived the frozen run: $after"; exit 1; }
# --init exits when its child (the MCP server stand-in) dies, so "running" covers both.
[ "$(docker inspect -f '{{.State.Running}}' "$NAME")" = true ] || { echo "FAIL init or the MCP server stand-in was killed"; exit 1; }
echo "ok   the frozen run kills what it could not check, and spares init and the MCP server"
echo "PASS: gpu reaper"
