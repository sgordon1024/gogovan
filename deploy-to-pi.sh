#!/bin/bash
# Deploy GoGoVan dashboard files to Pi
# URLs: http://vanpi.local  |  http://100.98.52.107  |  https://vanpi.tail27a0b4.ts.net

PASS="windows"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

# Prefer CWD when it has an index.html (e.g. running from a worktree).
if [ -f "$(pwd)/index.html" ]; then
  DIR="$(pwd)"
else
  DIR="$SCRIPT_DIR"
fi
echo "→ Source directory: $DIR"

# ── Detect Pi ──────────────────────────────────────────────────────────────
echo "=== Detecting Pi connection ==="
PI=""

# 1. Tailscale (works from anywhere when both devices are logged in)
if sshpass -p "$PASS" ssh -o ConnectTimeout=5 -o StrictHostKeyChecking=no "sgordon1024@100.98.52.107" "echo ok" &>/dev/null; then
  PI="sgordon1024@100.98.52.107"
  echo "→ Using Tailscale (100.98.52.107)"
fi

# 2. Known static IP on 'apple pi' router network
if [ -z "$PI" ]; then
  if sshpass -p "$PASS" ssh -o ConnectTimeout=5 -o StrictHostKeyChecking=no "sgordon1024@192.168.8.106" "echo ok" &>/dev/null; then
    PI="sgordon1024@192.168.8.106"
    echo "→ Using apple pi network (192.168.8.106)"
  fi
fi

# 3. mDNS — vanpi.local (works when Mac is on same LAN as Pi)
if [ -z "$PI" ]; then
  if sshpass -p "$PASS" ssh -o ConnectTimeout=5 -o StrictHostKeyChecking=no "sgordon1024@vanpi.local" "echo ok" &>/dev/null; then
    PI="sgordon1024@vanpi.local"
    echo "→ Using local mDNS (vanpi.local)"
  fi
fi

# 4. Try Pi's IP directly if caller set PI_IP env var
if [ -z "$PI" ] && [ -n "$PI_IP" ]; then
  if sshpass -p "$PASS" ssh -o ConnectTimeout=5 -o StrictHostKeyChecking=no "sgordon1024@$PI_IP" "echo ok" &>/dev/null; then
    PI="sgordon1024@$PI_IP"
    echo "→ Using PI_IP=$PI_IP"
  fi
fi

if [ -z "$PI" ]; then
  echo ""
  echo "ERROR: Cannot reach Pi. Try one of:"
  echo "  1. Log into Tailscale on this Mac, then re-run"
  echo "  2. Connect this Mac to the same Wi-Fi as the Pi (e.g. 'apple pi'), then re-run"
  echo "  3. Run:  PI_IP=<pi-ip> ./deploy-to-pi.sh"
  echo "     (find Pi IP in your router's device list, or run 'arp -a' on the Pi's network)"
  exit 1
fi

# ── Sudoers (nmcli without password) ──────────────────────────────────────
echo "=== Ensuring sudoers entry for nmcli ==="
sshpass -p "$PASS" ssh "$PI" \
  'echo windows | sudo -S bash -c "echo \"sgordon1024 ALL=(ALL) NOPASSWD: /usr/bin/nmcli\" > /etc/sudoers.d/gogovan-nmcli && chmod 440 /etc/sudoers.d/gogovan-nmcli" && echo sudoers ok' \
  || echo "(sudoers entry may already exist — continuing)"

# ── can-bridge.py ─────────────────────────────────────────────────────────
echo "=== Copying can-bridge.py ==="
CAN_SRC="$DIR/can-bridge.py"; [ -f "$CAN_SRC" ] || CAN_SRC="$SCRIPT_DIR/can-bridge.py"
sshpass -p "$PASS" scp "$CAN_SRC" "$PI:~/can-bridge.py" || { echo "FAILED: can-bridge.py copy"; exit 1; }

echo "=== Restarting can-bridge ==="
sshpass -p "$PASS" ssh "$PI" 'echo windows | sudo -S systemctl restart can-bridge && echo "can-bridge restarted"' \
  || { echo "FAILED: can-bridge restart"; exit 1; }

# ── starlink-bridge.py ────────────────────────────────────────────────────
echo "=== Copying starlink-bridge.py ==="
SL_SRC="$DIR/starlink-bridge.py"; [ -f "$SL_SRC" ] || SL_SRC="$SCRIPT_DIR/starlink-bridge.py"
sshpass -p "$PASS" scp "$SL_SRC" "$PI:~/starlink-bridge.py" || { echo "FAILED: starlink-bridge.py copy"; exit 1; }

echo "=== Installing starlink-bridge systemd service ==="
sshpass -p "$PASS" ssh "$PI" 'bash -s' << 'REMOTE'
set -e
PASS="windows"
# Write the service file (correct path — home dir, not gogovan subdir)
echo "$PASS" | sudo -S tee /etc/systemd/system/starlink-bridge.service > /dev/null << 'UNIT'
[Unit]
Description=Starlink smart plug + network routing bridge
After=network.target mosquitto.service
Wants=mosquitto.service

[Service]
ExecStart=/usr/bin/python3 /home/sgordon1024/starlink-bridge.py
WorkingDirectory=/home/sgordon1024
Restart=always
RestartSec=10
User=sgordon1024

[Install]
WantedBy=multi-user.target
UNIT
echo "$PASS" | sudo -S systemctl daemon-reload
echo "$PASS" | sudo -S systemctl enable starlink-bridge
echo "$PASS" | sudo -S systemctl restart starlink-bridge
echo "starlink-bridge installed and started"
REMOTE

# ── Disable old watchdog (superseded by starlink-bridge failover) ──────────
echo "=== Disabling old gogovan-watchdog (replaced by starlink-bridge) ==="
sshpass -p "$PASS" ssh "$PI" 'echo windows | sudo -S systemctl disable --now gogovan-watchdog.timer 2>/dev/null; echo "watchdog disabled"' \
  || echo "(watchdog already disabled or not present — continuing)"

# ── index.html ────────────────────────────────────────────────────────────
echo "=== Copying index.html ==="
sshpass -p "$PASS" scp "$DIR/index.html" "$PI:~/index.html" || { echo "FAILED: index.html copy"; exit 1; }

# ── rope-light.py ─────────────────────────────────────────────────────────
echo "=== Copying rope-light.py ==="
ROPE_SRC="$DIR/rope-light.py"; [ -f "$ROPE_SRC" ] || ROPE_SRC="$SCRIPT_DIR/rope-light.py"
sshpass -p "$PASS" scp "$ROPE_SRC" "$PI:~/rope-light.py" || { echo "FAILED: rope-light.py copy"; exit 1; }

echo "=== Restarting rope-light ==="
sshpass -p "$PASS" ssh "$PI" 'echo windows | sudo -S systemctl restart rope-light && echo "rope-light restarted"' \
  || echo "WARNING: rope-light restart failed"

# ── run-speedtest.py (no service restart — run on demand by timer/can-bridge) ──
echo "=== Copying run-speedtest.py ==="
RS_SRC="$DIR/run-speedtest.py"; [ -f "$RS_SRC" ] || RS_SRC="$SCRIPT_DIR/run-speedtest.py"
sshpass -p "$PASS" scp "$RS_SRC" "$PI:~/run-speedtest.py" 2>/dev/null && echo "run-speedtest.py copied" \
  || echo "WARNING: run-speedtest.py copy failed"

# ── obd-bridge.py ──────────────────────────────────────────────────────────
echo "=== Copying obd-bridge.py ==="
OBD_SRC="$DIR/obd-bridge.py"; [ -f "$OBD_SRC" ] || OBD_SRC="$SCRIPT_DIR/obd-bridge.py"
sshpass -p "$PASS" scp "$OBD_SRC" "$PI:~/obd-bridge.py" 2>/dev/null && echo "obd-bridge.py copied" \
  || echo "WARNING: obd-bridge.py copy failed"
sshpass -p "$PASS" ssh "$PI" 'echo windows | sudo -S systemctl restart obd-bridge 2>/dev/null && echo "obd-bridge restarted" || echo "(obd-bridge service not installed — run pi-setup/setup-obd.sh)"'

# ── voice-bridge.py (Claude voice control) ─────────────────────────────────
echo "=== Copying voice-bridge.py ==="
VOICE_SRC="$DIR/voice-bridge.py"; [ -f "$VOICE_SRC" ] || VOICE_SRC="$SCRIPT_DIR/voice-bridge.py"
sshpass -p "$PASS" scp "$VOICE_SRC" "$PI:~/voice-bridge.py" 2>/dev/null && echo "voice-bridge.py copied" \
  || echo "WARNING: voice-bridge.py copy failed"

echo "=== Installing voice-bridge systemd service ==="
sshpass -p "$PASS" ssh "$PI" 'bash -s' << 'REMOTE'
set -e
PASS="windows"
echo "$PASS" | sudo -S tee /etc/systemd/system/voice-bridge.service > /dev/null << 'UNIT'
[Unit]
Description=GoGoVan voice control bridge (Claude)
After=network.target mosquitto.service
Wants=mosquitto.service

[Service]
ExecStart=/usr/bin/python3 /home/sgordon1024/voice-bridge.py
WorkingDirectory=/home/sgordon1024
Restart=always
RestartSec=10
User=sgordon1024

[Install]
WantedBy=multi-user.target
UNIT
echo "$PASS" | sudo -S systemctl daemon-reload
echo "$PASS" | sudo -S systemctl enable voice-bridge
echo "$PASS" | sudo -S systemctl restart voice-bridge
echo "voice-bridge installed and started"
REMOTE

# ── Status check ──────────────────────────────────────────────────────────
echo ""
echo "=== Service status ==="
sleep 3
sshpass -p "$PASS" ssh "$PI" '
  echo "--- can-bridge ---"
  sudo systemctl status can-bridge --no-pager -l | head -8
  echo "--- starlink-bridge ---"
  sudo systemctl status starlink-bridge --no-pager -l | head -8
  echo "--- starlink-bridge recent logs ---"
  sudo journalctl -u starlink-bridge -n 20 --no-pager
'

echo ""
echo "=== DONE ==="
echo "Dashboard: http://vanpi.local | https://vanpi.tail27a0b4.ts.net"
