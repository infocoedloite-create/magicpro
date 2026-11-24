#!/usr/bin/env bash
# Install Magic Pro as a supervisor service on a Vast.ai base-image instance and put it
# behind the instance's Caddy auth edge. Run from the project folder on the instance.
set -euo pipefail
APP_DIR="$(cd "$(dirname "$0")/../.." && pwd)"
INTERNAL_PORT="${MAGIC_INTERNAL_PORT:-17860}"
EXTERNAL_PORT="${MAGIC_EXTERNAL_PORT:-10100}"   # must be a free open port on the instance

# MAGIC_IMAGE_BUILD=1 (docker build): write the files only; supervisor and the portal are not running yet
BUILD="${MAGIC_IMAGE_BUILD:-}"
bash "$APP_DIR/setup.sh"

cat > /opt/supervisor-scripts/magicpro.sh <<EOF
#!/bin/bash
utils=/opt/supervisor-scripts/utils
. "\${utils}/logging.sh"
. "\${utils}/environment.sh"
. "\${utils}/exit_portal.sh" "Magic Pro"
cd "$APP_DIR"
export MAGIC_HOST=127.0.0.1 MAGIC_PORT=$INTERNAL_PORT MAGIC_OPEN_BROWSER=0
pty "$APP_DIR/.venv/bin/python" run.py 2>&1
EOF
chmod +x /opt/supervisor-scripts/magicpro.sh

cat > /etc/supervisor/conf.d/magicpro.conf <<EOF
[program:magicpro]
environment=PROC_NAME="%(program_name)s"
command=/opt/supervisor-scripts/magicpro.sh
autostart=true
autorestart=true
startsecs=5
stopasgroup=true
killasgroup=true
stopsignal=TERM
stopwaitsecs=10
stdout_logfile=/dev/stdout
redirect_stderr=true
stdout_logfile_maxbytes=0
EOF

if [ -n "$BUILD" ]; then echo "Magic Pro service files written (image build)."; exit 0; fi

python3 - "$EXTERNAL_PORT" "$INTERNAL_PORT" <<'PY'
import sys, yaml
ext, internal = int(sys.argv[1]), int(sys.argv[2])
d = yaml.safe_load(open("/etc/portal.yaml")) or {}
d.setdefault("applications", {})["Magic Pro"] = {
    "hostname": "localhost", "external_port": ext, "internal_port": internal,
    "open_path": "/", "name": "Magic Pro"}
yaml.safe_dump(d, open("/etc/portal.yaml", "w"), sort_keys=False)
PY

supervisorctl reread
supervisorctl update
supervisorctl restart magicpro || true
supervisorctl restart caddy
echo "Magic Pro service installed (127.0.0.1:$INTERNAL_PORT, external port $EXTERNAL_PORT behind token auth)."
