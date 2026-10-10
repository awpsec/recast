#!/bin/sh
# Run recast-server as PUID:PGID (the arr-stack convention) so files written to the library get the same
# owner as Sonarr/Radarr's, with access to the GPU's render node for hardware encoding.
set -e
umask "${UMASK:-002}"

if [ "$(id -u)" = "0" ] && [ "${PUID:-0}" != "0" ]; then
    groups="${PGID}"
    for dev in /dev/dri/renderD* /dev/dri/card* /dev/nvidia0; do
        [ -e "$dev" ] && groups="${groups},$(stat -c %g "$dev")"
    done
    for d in /config "${RECAST_SCRATCH:-/scratch}"; do
        mkdir -p "$d"
        [ "$(stat -c %u "$d")" = "$PUID" ] || chown -R "$PUID:$PGID" "$d"
    done
    exec setpriv --reuid="$PUID" --regid="$PGID" --groups="$groups" --inh-caps=-all \
        env HOME=/config recast-server "$@"
fi
exec recast-server "$@"
