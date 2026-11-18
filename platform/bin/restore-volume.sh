#!/bin/sh
# Usage: restore-volume.sh <archive-name>
# Mounts: /backup (archives, read-only), /data (target volume), /staging (empty temp volume).
# Extracts into /staging first; only when that succeeds is /data replaced.
set -eu
case "$1" in */*|"") echo "bad archive name" >&2; exit 2 ;; esac
tar -xzf "/backup/$1" -C /staging
find /data -mindepth 1 -maxdepth 1 -exec rm -rf {} +
cp -a /staging/. /data/
