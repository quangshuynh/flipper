#!/bin/sh
# Container entrypoint: refuse ephemeral storage, own the persistent disk, drop root, exec CMD.
set -eu

# New databases, attachments, credential files, and backups are readable by the owner only.
umask 077

data_dir="${FLIPPER_DATA_DIR:-}"
case "$data_dir" in
    /*) ;;
    *)
        echo "flipper: FLIPPER_DATA_DIR must be an absolute path on the persistent disk" >&2
        exit 64
        ;;
esac
disk="$(dirname "$data_dir")"

# The authoritative database must never live on the container's ephemeral filesystem. Require
# the data directory, or its parent, to be a mounted volume other than the container root.
if [ "$disk" = "/" ] || ! { mountpoint -q "$disk" || mountpoint -q "$data_dir"; }; then
    echo "flipper: $disk is not a mounted persistent volume; refusing to start" >&2
    exit 65
fi

if [ "$(id -u)" = "0" ]; then
    mkdir -p "$data_dir"
    # Hosting providers mount disks owned by root. Hand the disk to the unprivileged app user so
    # it can write the database, attachments, credential file, and backups beside it.
    chown -R flipper:flipper "$disk"
    exec setpriv --reuid=flipper --regid=flipper --init-groups --inh-caps=-all -- "$@"
fi

exec "$@"
