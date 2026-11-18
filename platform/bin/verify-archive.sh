#!/bin/sh
# Usage: verify-archive.sh <archive-name>   (archives at /backup)
set -eu
case "$1" in */*|"") echo "bad archive name" >&2; exit 2 ;; esac
gzip -t "/backup/$1"
tar -tzf "/backup/$1" > /dev/null
