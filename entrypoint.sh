#!/bin/sh
set -eu

# Railway mounts volumes as root. The platform starts this entrypoint as root
# (RAILWAY_RUN_UID=0); initialize the data directory, then run the app unprivileged.
if [ "$(id -u)" -eq 0 ]; then
    install -d -o appuser -g appuser /data /data/uploads
    for db_file in /data/agribank_recon.db*; do
        if [ -e "$db_file" ]; then
            chown appuser:appuser "$db_file"
        fi
    done
    exec gosu appuser "$@"
fi

exec "$@"
