#!/bin/sh
# Usage: backup-folders.sh <folder>...   (workspace at /data, output dir at /backup)
# Writes /backup/workspace-folders.tar.gz with the folders that exist.
# Exit 0 = ok, 1 = ok with warnings, 3 = none of the folders exist, 2 = failed.
set -u
cd /data || exit 2
present=""
for folder in "$@"; do
  case "$folder" in */*|.|..|""|-*) echo "bad folder name: $folder" >&2; exit 2 ;; esac
  [ -e "$folder" ] && present="$present $folder"
done
[ -n "$present" ] || exit 3
# shellcheck disable=SC2086
tar --warning=no-file-changed --ignore-failed-read \
    --exclude=node_modules --exclude=.venv --exclude=__pycache__ --exclude=.cache \
    -czf /backup/workspace-folders.tar.gz -- $present
rc=$?
[ -s /backup/workspace-folders.tar.gz ] || { echo "archive is empty" >&2; exit 2; }
exit "$rc"
