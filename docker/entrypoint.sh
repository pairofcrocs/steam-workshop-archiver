#!/bin/bash
# Prepare volumes, fix ownership for PUID/PGID, then drop privileges and start the app.
set -euo pipefail

PUID=${PUID:-1000}
PGID=${PGID:-1000}
META_DIR=${META_DIR:-/meta}
DOWNLOADS_DIR=${DOWNLOADS_DIR:-/downloads}
FIX_PERMISSIONS=${FIX_PERMISSIONS:-auto}
export HOME="$META_DIR/home"

mkdir -p "$META_DIR" "$DOWNLOADS_DIR" "$HOME"

seeded=0
if [ -d /opt/steamcmd ] && [ ! -x "$META_DIR/steamcmd/steamcmd.sh" ]; then
    echo "Seeding SteamCMD into $META_DIR/steamcmd"
    mkdir -p "$META_DIR/steamcmd"
    cp -a /opt/steamcmd/. "$META_DIR/steamcmd/"
    seeded=1
fi

# Started with --user / user: already unprivileged, nothing to switch.
if [ "$(id -u)" != "0" ] || [ "$PUID" = "0" ]; then
    exec "$@"
fi

groupmod -o -g "$PGID" appuser
usermod -o -u "$PUID" -g "$PGID" -d "$HOME" appuser >/dev/null

try_chown() {
    "$@" 2>/dev/null || echo "warning: couldn't change ownership ($*) — fix permissions on the volume manually if writes fail" >&2
}

# A full recursive chown only on first run or when PUID/PGID change; otherwise just
# the top-level directories, so big archives start instantly.
owner_file="$META_DIR/.owner"
if [ "$FIX_PERMISSIONS" = "off" ]; then
    :
elif [ "$FIX_PERMISSIONS" = "full" ] || [ "$(cat "$owner_file" 2>/dev/null || true)" != "$PUID:$PGID" ]; then
    echo "Setting ownership of $META_DIR and $DOWNLOADS_DIR to $PUID:$PGID (one-time; may take a while)"
    try_chown chown -R "$PUID:$PGID" "$META_DIR"
    try_chown find "$DOWNLOADS_DIR" -xdev \( ! -user "$PUID" -o ! -group "$PGID" \) -exec chown -h "$PUID:$PGID" {} +
    echo "$PUID:$PGID" > "$owner_file" || true
    try_chown chown "$PUID:$PGID" "$owner_file"
else
    try_chown chown "$PUID:$PGID" "$META_DIR" "$DOWNLOADS_DIR" "$HOME"
    if [ "$seeded" = "1" ]; then
        try_chown chown -R "$PUID:$PGID" "$META_DIR/steamcmd"
    fi
fi

exec gosu "$PUID:$PGID" "$@"
