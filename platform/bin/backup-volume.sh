#!/bin/sh
# Usage: backup-volume.sh <archive-name>   (volume at /data, output dir at /backup)
# Exit 0 = ok, 1 = ok with warnings (files changed while reading), 2+ = failed.
set -u
name="$1"
case "$name" in */*|"") echo "bad archive name" >&2; exit 2 ;; esac
tar --warning=no-file-changed --ignore-failed-read -czf "/backup/$name" -C /data .
rc=$?
[ -s "/backup/$name" ] || { echo "archive is empty" >&2; exit 2; }
exit "$rc"
