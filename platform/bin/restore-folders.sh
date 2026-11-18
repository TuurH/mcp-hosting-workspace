#!/bin/sh
# Usage: restore-folders.sh   (archive /backup/workspace-folders.tar.gz into /data)
set -eu
gzip -t /backup/workspace-folders.tar.gz
tar -xzf /backup/workspace-folders.tar.gz -C /data
