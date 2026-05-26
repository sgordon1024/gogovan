#!/bin/bash
# Deploy updated files to GoGoVan Pi
# Dashboard URLs: http://vanpi.local  (on Apple Pi) | http://100.98.52.107 (via Tailscale)

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
elif sshpass -p "$PASS" ssh -o ConnectTimeout=5 -o StrictHostKeyChecking=no "sgordon1024@192.168.8.106" "echo ok" &>/dev/null; then
  PI="sgordon1024@192.168.8.106"
  echo "→ Using Apple Pi LAN (192.168.8.106)"
else
  echo "ERROR: Cannot reach Pi via Tailscale or local network."
  echo "  - Via Tailscale: connect iPhone to Tailscale first"
  echo "  - Via Apple Pi Wi-Fi: connect to Apple Pi network first"
  exit 1
fi

echo "=== Copying can-bridge.py ==="
CAN_SRC="$DIR/can-bridge.py"; [ -f "$CAN_SRC" ] || CAN_SRC="$SCRIPT_DIR/can-bridge.py"
sshpass -p "$PASS" scp "$CAN_SRC" "$PI:~/can-bridge.py" || { echo "FAILED: can-bridge.py copy"; exit 1; }

echo "=== Adding sudoers entry ==="
sshpass -p "$PASS" ssh "$PI" 'echo windows | sudo -S bash -c "echo \"sgordon1024 ALL=(ALL) NOPASSWD: /usr/bin/nmcli\" > /etc/sudoers.d/gogovan-nmcli && chmod 440 /etc/sudoers.d/gogovan-nmcli" && echo "sudoers ok"' || echo "WARNING: sudoers may already exist"

echo "=== Restarting can-bridge service ==="
sshpass -p "$PASS" ssh "$PI" 'echo windows | sudo -S systemctl restart can-bridge && echo "Service restarted"' || { echo "FAILED: service restart"; exit 1; }

echo "=== Copying index.html ==="
sshpass -p "$PASS" scp "$DIR/index.html" "$PI:~/index.html" || { echo "FAILED: index.html copy"; exit 1; }

echo "=== Copying rope-light.py ==="
ROPE_SRC="$DIR/rope-light.py"; [ -f "$ROPE_SRC" ] || ROPE_SRC="$SCRIPT_DIR/rope-light.py"
sshpass -p "$PASS" scp "$ROPE_SRC" "$PI:~/rope-light.py" || { echo "FAILED: rope-light.py copy"; exit 1; }

echo "=== Restarting rope-light service ==="
sshpass -p "$PASS" ssh "$PI" 'echo windows | sudo -S systemctl restart rope-light && echo "rope-light restarted"' || echo "WARNING: rope-light restart failed"

echo "=== Copying starlink-bridge.py ==="
STARLINK_SRC="$DIR/starlink-bridge.py"; [ -f "$STARLINK_SRC" ] || STARLINK_SRC="$SCRIPT_DIR/starlink-bridge.py"
sshpass -p "$PASS" scp "$STARLINK_SRC" "$PI:~/starlink-bridge.py" || { echo "FAILED: starlink-bridge.py copy"; exit 1; }

echo "=== Restarting starlink-bridge service ==="
sshpass -p "$PASS" ssh "$PI" 'echo windows | sudo -S systemctl restart starlink-bridge && echo "starlink-bridge restarted"' || echo "WARNING: starlink-bridge restart failed"

echo "=== Copying run-speedtest.py ==="
SPEEDTEST_SRC="$DIR/run-speedtest.py"; [ -f "$SPEEDTEST_SRC" ] || SPEEDTEST_SRC="$SCRIPT_DIR/run-speedtest.py"
if [ -f "$SPEEDTEST_SRC" ]; then
  sshpass -p "$PASS" scp "$SPEEDTEST_SRC" "$PI:~/run-speedtest.py" && chmod +x run-speedtest.py || echo "WARNING: run-speedtest.py copy failed"
fi

echo "=== Copying obd-bridge.py ==="
OBD_SRC="$DIR/obd-bridge.py"; [ -f "$OBD_SRC" ] || OBD_SRC="$SCRIPT_DIR/obd-bridge.py"
if [ -f "$OBD_SRC" ]; then
  sshpass -p "$PASS" scp "$OBD_SRC" "$PI:~/obd-bridge.py" || echo "WARNING: obd-bridge.py copy failed"
  sshpass -p "$PASS" ssh "$PI" 'echo windows | sudo -S systemctl restart obd-bridge 2>/dev/null && echo "obd-bridge restarted"' || echo "WARNING: obd-bridge not yet installed — run pi-setup/setup-obd.sh first"
fi

echo "=== Verifying services ==="
sleep 3
sshpass -p "$PASS" ssh "$PI" 'sudo systemctl is-active can-bridge starlink-bridge rope-light'

echo "=== DONE ==="
