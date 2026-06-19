#!/bin/bash
# auto-deploy.sh — called by the Claude Code Stop hook (~/.claude/settings.json).
#
# Deploys the GoGoVan dashboard to the Pi ONLY when a deployable file's content
# changed since the last successful deploy. This keeps turns that didn't touch a
# Pi file (questions, doc edits) from pointlessly restarting the van's services.
#
# deploy-to-pi.sh writes the stamp ($HOME/.gogovan-deploy-hash) on success, so
# when Claude already deployed in-turn this is a fast no-op. It is purely a
# safety net for turns where a Pi file changed but wasn't deployed.
#
# Always exits 0 — it must never block the Stop hook, even if the Pi is offline
# (deploy-to-pi.sh just exits non-zero, the stamp stays stale, next Stop retries).

REPO="/Users/stephengordon/development/gogovan"
cd "$REPO" 2>/dev/null || exit 0

STAMP="$HOME/.gogovan-deploy-hash"
LOCK="/tmp/gogovan-autodeploy.lock"
FILES="index.html can-bridge.py rope-light.py starlink-bridge.py obd-bridge.py run-speedtest.py voice-bridge.py"

# Clear a stale lock (a deploy killed mid-flight) so we don't wedge forever.
[ -d "$LOCK" ] && find "$LOCK" -maxdepth 0 -mmin +10 -exec rmdir {} \; 2>/dev/null
# Single-flight: if a deploy is already running, skip — the next Stop catches any change.
mkdir "$LOCK" 2>/dev/null || exit 0
trap 'rmdir "$LOCK" 2>/dev/null' EXIT

NEW=$(cat $FILES 2>/dev/null | shasum | cut -d' ' -f1)
OLD=$(cat "$STAMP" 2>/dev/null)
if [ -n "$NEW" ] && [ "$NEW" = "$OLD" ]; then
  exit 0   # nothing changed since the last deploy
fi

echo "=== auto-deploy: change detected, deploying $(date) ==="
./deploy-to-pi.sh         # writes $STAMP itself on success
echo "=== auto-deploy: finished (exit $?) $(date) ==="
exit 0
