#!/usr/bin/env bash
# Start this machine as a Lucent remote (Agent Control Protocol).
#
# Credentials live in ~/.lucent-remote.env (chmod 600), never in the repo:
#
#   LUCENT_CONTROLLER_URL=http://localhost:8766
#   LUCENT_REMOTE_ID=<remote uuid from /sandboxes?tab=remotes>
#   LUCENT_REMOTE_TOKEN=lr_...
#   LUCENT_REMOTE_EXECUTOR=shell      # or noop / command
#   LUCENT_REMOTE_SHELL=/bin/zsh      # shell executor only
#
# Usage:
#   scripts/start_remote_agent.sh            # foreground
#   scripts/start_remote_agent.sh --detach   # background, logs to /tmp
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="${LUCENT_REMOTE_ENV:-$HOME/.lucent-remote.env}"
LOG_FILE="${LUCENT_REMOTE_LOG:-/tmp/lucent-remote-agent.log}"
PYTHON="${LUCENT_REMOTE_PYTHON:-$REPO_ROOT/.venv/bin/python}"

if [[ ! -f "$ENV_FILE" ]]; then
  echo "Missing remote credentials file: $ENV_FILE" >&2
  echo "Create it with LUCENT_CONTROLLER_URL, LUCENT_REMOTE_ID, LUCENT_REMOTE_TOKEN." >&2
  exit 2
fi
if [[ ! -x "$PYTHON" ]]; then
  echo "Python not found at $PYTHON (set LUCENT_REMOTE_PYTHON)." >&2
  exit 2
fi

set -a
# shellcheck disable=SC1090
source "$ENV_FILE"
set +a

# One agent per remote on this host. Two pollers race for claims and the loser
# spins, which looks like flaky dispatch rather than a configuration mistake.
if pgrep -f "lucent_remote_agent.py" >/dev/null 2>&1; then
  echo "A remote agent is already running (pid $(pgrep -f lucent_remote_agent.py | tr '\n' ' '))." >&2
  exit 0
fi

AGENT="$REPO_ROOT/scripts/lucent_remote_agent.py"

if [[ "${1:-}" == "--detach" ]]; then
  nohup "$PYTHON" "$AGENT" --poll-seconds "${LUCENT_REMOTE_POLL_SECONDS:-5}" \
    >"$LOG_FILE" 2>&1 &
  echo "remote agent started (pid $!), logging to $LOG_FILE"
else
  exec "$PYTHON" "$AGENT" --poll-seconds "${LUCENT_REMOTE_POLL_SECONDS:-5}"
fi
