#!/bin/sh
# Run spoolmix as PUID:PGID (default 1000:1000) and make sure it can write /data,
# so a bind-mounted ./data folder works without fiddling with permissions.
set -e
PUID="${PUID:-1000}"
PGID="${PGID:-1000}"
if [ "$(id -u)" = "0" ]; then
    mkdir -p /data
    if [ "$(stat -c %u:%g /data)" != "$PUID:$PGID" ]; then
        chown -R "$PUID:$PGID" /data
    fi
    export HOME=/tmp
    exec setpriv --reuid="$PUID" --regid="$PGID" --clear-groups "$@"
fi
exec "$@"
