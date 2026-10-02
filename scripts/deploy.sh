#!/usr/bin/env bash
# Deploy the latest Sherlocks code from this machine to the server.
#
#   scripts/deploy.sh                 copy code, rebuild ON the server (server needs internet for pip)
#   scripts/deploy.sh --image         build HERE, ship the image (server needs no internet)
#   scripts/deploy.sh --with-env      also copy this machine's .env (overwrites the server's!)
#
# Settings (override on the command line, e.g. SERVER=user@host scripts/deploy.sh):
SERVER="${SERVER:-cdranalysis@192.168.200.239}"
REMOTE_DIR="${REMOTE_DIR:-/var/www/brother-eye}"

set -euo pipefail
cd "$(dirname "$0")/.."

# One SSH connection for the whole deploy: the password is asked once, not per step.
SOCKET="${HOME}/.ssh/sherlocks-deploy-%r@%h:%p"
mkdir -p "${HOME}/.ssh"
SSH_OPTS=(-o ControlMaster=auto -o "ControlPath=$SOCKET" -o ControlPersist=15m)
ssh() { command ssh "${SSH_OPTS[@]}" "$@"; }
RSYNC_SSH="ssh ${SSH_OPTS[*]}"

MODE=build-on-server
WITH_ENV=0
for arg in "$@"; do
  case "$arg" in
    --image) MODE=ship-image ;;
    --with-env) WITH_ENV=1 ;;
    *) echo "unknown option: $arg" >&2; exit 2 ;;
  esac
done

echo "==> Deploying to $SERVER:$REMOTE_DIR ($MODE)"

ssh "$SERVER" "mkdir -p '$REMOTE_DIR'"

# 1. Code. The server keeps its own .env, database, photos (output/) and logs.
EXCLUDES=(--exclude .venv/ --exclude .git/ --exclude __pycache__/ --exclude '*.pyc' --exclude '*.egg-info/'
          --exclude .pytest_cache/ --exclude .ruff_cache/ --exclude output/ --exclude logs/ --exclude .claude/)
[ "$WITH_ENV" = 1 ] || EXCLUDES+=(--exclude .env)
rsync -az --delete -e "$RSYNC_SSH" "${EXCLUDES[@]}" ./ "$SERVER:$REMOTE_DIR/"
echo "    code copied"


# 2. Image + restart. -t so sudo can ask for the server's password.
if [ "$MODE" = ship-image ]; then
  echo "==> Building the image here"
  sudo docker compose build app
  echo "==> Shipping it (a few hundred MB)"
  sudo docker save sherlocks-app:latest | gzip | ssh "$SERVER" "cat > /tmp/sherlocks-app.tar.gz"
  ssh -t "$SERVER" "cd '$REMOTE_DIR' && gunzip -c /tmp/sherlocks-app.tar.gz | sudo docker load && rm -f /tmp/sherlocks-app.tar.gz && sudo docker compose up -d"
else
  ssh -t "$SERVER" "cd '$REMOTE_DIR' && sudo docker compose up --build -d"
fi

# 3. Check it came up.
echo "==> Waiting for the API"
ssh -t "$SERVER" "for i in \$(seq 1 30); do curl -fsS http://127.0.0.1:7401/health && echo && exit 0; sleep 3; done; \
  echo 'API did not come up - last log lines:'; sudo docker logs --tail 40 sherlocks_app; exit 1"
echo "==> Done: http://${SERVER#*@}:7401/"
