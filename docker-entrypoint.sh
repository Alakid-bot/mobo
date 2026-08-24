#!/bin/sh
set -eu

# Zeabur mounts the persistent volume at container start. Normalising its
# ownership here lets the application itself stay non-root regardless of the
# initial volume owner.
mkdir -p /data
chown -R app:app /data

exec gosu app "$@"
