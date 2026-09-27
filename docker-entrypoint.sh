#!/bin/sh
# Start as root only long enough to make the data directory writable, then run as the "app" user.
# Render mounts persistent disks owned by root, which an unprivileged process could not write to.
set -e
DATA_DIR="$(dirname "${DATABASE_PATH:-/app/data/triage.db}")"
if [ "$(id -u)" = "0" ]; then
  mkdir -p "$DATA_DIR"
  chown -R app:app "$DATA_DIR" 2>/dev/null || echo "warning: could not change the owner of $DATA_DIR" >&2
  exec python -c 'import os, pwd, sys
u = pwd.getpwnam("app")
os.setgroups([])
os.setgid(u.pw_gid)
os.setuid(u.pw_uid)
os.environ["HOME"] = u.pw_dir
os.execvp(sys.argv[1], sys.argv[1:])' "$@"
fi
exec "$@"
