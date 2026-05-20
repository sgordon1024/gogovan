#!/bin/bash
# Deploy updated can-bridge.py and index.html to GoGoVan Pi
# Dashboard URLs: http://vanpi.local  (on GoGoVan) | http://100.98.52.107 (via Tailscale)

PASS="windows"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

# Prefer CWD when it has an index.html (e.g. running from a worktree).
# Fall back to the script's own directory.
if [ -f "$(pwd)/index.html" ]; then
  DIR="$(pwd)"
else
  DIR="$SCRIPT_DIR"
fi
echo "→ Source directory: $DIR"

# Auto-detect Pi — try Tailscale first, fall back to local network
echo "=== Detecting Pi connection ==="
if sshpass -p "$PASS" ssh -o ConnectTimeout=5 -o StrictHostKeyChecking=no "sgordon1024@100.98.52.107" "echo ok" &>/dev/null; then
  PI="sgordon1024@100.98.52.107"
  echo "→ Using Tailscale (100.98.52.107)"
elif sshpass -p "$PASS" ssh -o ConnectTimeout=5 -o StrictHostKeyChecking=no "sgordon1024@vanpi.local" "echo ok" &>/dev/null; then
  PI="sgordon1024@vanpi.local"
  echo "→ Using local network (vanpi.local)"
elif sshpass -p "$PASS" ssh -o ConnectTimeout=5 -o StrictHostKeyChecking=no "sgordon1024@192.168.4.1" "echo ok" &>/dev/null; then
  PI="sgordon1024@192.168.4.1"
  echo "→ Using local IP (192.168.4.1)"
else
  echo "ERROR: Cannot reach Pi via Tailscale or local network."
  echo "  - Via Tailscale: make sure Tailscale is running on this machine"
  echo "  - Via GoGoVan Wi-Fi: connect to GoGoVan network first"
  exit 1
fi

echo "=== Copying can-bridge.py ==="
sshpass -p "$PASS" scp "$DIR/can-bridge.py" "$PI:~/can-bridge.py" || { echo "FAILED: can-bridge.py copy"; exit 1; }

echo "=== Adding sudoers entry ==="
sshpass -p "$PASS" ssh "$PI" 'echo windows | sudo -S bash -c "echo \"sgordon1024 ALL=(ALL) NOPASSWD: /usr/bin/nmcli\" > /etc/sudoers.d/gogovan-nmcli && chmod 440 /etc/sudoers.d/gogovan-nmcli" && echo "sudoers ok"' || echo "WARNING: sudoers may already exist"

echo "=== Restarting can-bridge service ==="
sshpass -p "$PASS" ssh "$PI" 'echo windows | sudo -S systemctl restart can-bridge && echo "Service restarted"' || { echo "FAILED: service restart"; exit 1; }

echo "=== Copying index.html ==="
sshpass -p "$PASS" scp "$DIR/index.html" "$PI:~/index.html" || { echo "FAILED: index.html copy"; exit 1; }

echo "=== Copying rope-light.py ==="
sshpass -p "$PASS" scp "$DIR/rope-light.py" "$PI:~/rope-light.py" || { echo "FAILED: rope-light.py copy"; exit 1; }

echo "=== Restarting rope-light service ==="
sshpass -p "$PASS" ssh "$PI" 'echo windows | sudo -S systemctl restart rope-light && echo "rope-light restarted"' || echo "WARNING: rope-light restart failed"

echo "=== Verifying can-bridge is running ==="
sleep 3
sshpass -p "$PASS" ssh "$PI" 'sudo systemctl status can-bridge --no-pager -l | head -20'

echo "=== Checking subscription in logs ==="
sshpass -p "$PASS" ssh "$PI" 'sudo journalctl -u can-bridge -n 20 --no-pager'

echo "=== DONE ==="
