#!/usr/bin/env bash
# Updates the Pi from the git remote (run it after commit + push): git pull there, restart the service, check it answers.
# Refuses to run while local commits are not pushed, so the Pi never lags behind what you think you deployed.
# Override with env vars: PI_HOST, PI_KEY_ALIAS, PI_DIR, PI_SERVICE, PI_PORT.
set -euo pipefail

PI_HOST="${PI_HOST:-root@192.168.68.110}"
PI_KEY_ALIAS="${PI_KEY_ALIAS:-192.168.1.33}"   # the Pi's old address, still the name in known_hosts on this PC
PI_DIR="${PI_DIR:-/mnt/media/apps/jobwatch-git}"
PI_SERVICE="${PI_SERVICE:-jobwatch}"
PI_PORT="${PI_PORT:-8090}"

cd "$(dirname "$0")/.."
if [ -n "$(git status --porcelain --untracked-files=no)" ]; then
  echo "Uncommitted changes: commit and push first." >&2; exit 1
fi
git fetch -q origin
branch="$(git branch --show-current)"
if [ -n "$(git log "origin/$branch..HEAD" --oneline)" ]; then
  echo "Local commits are not pushed yet: git push first." >&2; exit 1
fi

ssh -o BatchMode=yes -o HostKeyAlias="$PI_KEY_ALIAS" "$PI_HOST" bash -s <<EOF
set -e
cd "$PI_DIR"
git pull --ff-only
systemctl restart "$PI_SERVICE"
sleep 3
systemctl is-active "$PI_SERVICE"
code=\$(curl -s -o /dev/null -w '%{http_code}' "localhost:$PI_PORT/")
echo "HTTP \$code"
[ "\$code" = 200 ]
git log --oneline -1
EOF
echo "Pi updated."
