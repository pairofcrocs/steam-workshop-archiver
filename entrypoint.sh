#!/bin/bash
# Drop privileges to PUID:PGID if provided, then exec the app.
PUID=${PUID:-0}
PGID=${PGID:-0}

if [ "$PUID" != "0" ] || [ "$PGID" != "0" ]; then
    groupmod -o -g "$PGID" appuser 2>/dev/null || groupadd -o -g "$PGID" appuser
    usermod  -o -u "$PUID" appuser 2>/dev/null || useradd  -o -u "$PUID" -g "$PGID" -M -s /bin/bash appuser
    chown -R appuser /opt/steamcmd
    exec gosu appuser "$@"
else
    exec "$@"
fi
