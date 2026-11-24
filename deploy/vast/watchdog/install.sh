#!/usr/bin/env bash
# Install the GPU idle watchdog as a supervisor service on the Vast.ai instance.
#   bash deploy/vast/watchdog/install.sh            # dry run: only logs "would stop" (default)
#   MAGIC_WATCHDOG_DRY_RUN=0 bash deploy/vast/watchdog/install.sh   # really stop the instance
#   pool node: also pass MAGIC_LAUNCHER_URL and MAGIC_NODE_KEY (from the launcher's adopt step)
# Settings are written to /etc/magicpro-watchdog.env; edit it and `supervisorctl restart
# magicpro-watchdog` to change them later. Logs: /var/log/portal/magicpro-watchdog.log
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
ENV_FILE=/etc/magicpro-watchdog.env

# Values given to this script are written as-is; anything not given falls back at run time to the
# container's own environment (set when the pool creates an instance), then to the default.
w() { if [ -n "${!1:-}" ]; then echo "$1=${!1}"; else echo "$1=\${$1:-$2}"; fi; }
{
  w MAGIC_WATCHDOG_DRY_RUN 1
  w MAGIC_IDLE_GRACE 600
  w MAGIC_DEADMAN 900
  w MAGIC_APP_URL http://127.0.0.1:17860
  w MAGIC_LAUNCHER_URL ""
  w MAGIC_NODE_KEY ""
  w MAGIC_TUNNEL_DIRECT 0
} > "$ENV_FILE"
chmod 600 "$ENV_FILE"

cat > /opt/supervisor-scripts/magicpro-watchdog.sh <<EOF
#!/bin/bash
utils=/opt/supervisor-scripts/utils
. "\${utils}/logging.sh"
. "\${utils}/environment.sh"      # CONTAINER_ID / CONTAINER_API_KEY come from /etc/environment
set -a; . $ENV_FILE; set +a
pty /usr/bin/python3 -u "$HERE/watchdog.py" 2>&1
EOF
chmod +x /opt/supervisor-scripts/magicpro-watchdog.sh

cat > /etc/supervisor/conf.d/magicpro-watchdog.conf <<EOF
[program:magicpro-watchdog]
environment=PROC_NAME="%(program_name)s"
command=/opt/supervisor-scripts/magicpro-watchdog.sh
autostart=true
autorestart=true
startsecs=3
stopasgroup=true
killasgroup=true
stdout_logfile=/dev/stdout
redirect_stderr=true
stdout_logfile_maxbytes=0
EOF

if [ -n "${MAGIC_IMAGE_BUILD:-}" ]; then echo "Watchdog service files written (image build)."; exit 0; fi
supervisorctl reread
supervisorctl update
supervisorctl restart magicpro-watchdog || true
echo "Watchdog installed ($(grep DRY_RUN $ENV_FILE)). Log: /var/log/portal/magicpro-watchdog.log"
