#!/bin/bash
# Deploy to Pi while on Apple Pi network (no internet needed)
# Run this from Terminal: bash ~/Desktop/deploy-local.sh
# Or from the project folder: ./deploy-local.sh

PASS="windows"
USER="sgordon1024"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

echo "=== GoGoVan Deploy (Local Network) ==="
echo ""

# Try known IPs first
PI=""
for IP in 192.168.8.106 192.168.4.1 100.98.52.107; do
  echo -n "Trying $IP... "
  if sshpass -p "$PASS" ssh -o ConnectTimeout=5 -o StrictHostKeyChecking=no "$USER@$IP" "echo ok" &>/dev/null; then
    PI="$USER@$IP"
    echo "CONNECTED ✓"
    break
  else
    echo "no"
  fi
done

# If still not found, scan 192.168.8.x subnet
if [ -z "$PI" ]; then
  echo "Scanning 192.168.8.x subnet for Pi..."
  for i in $(seq 100 150); do
    IP="192.168.8.$i"
    if sshpass -p "$PASS" ssh -o ConnectTimeout=2 -o StrictHostKeyChecking=no "$USER@$IP" "echo ok" &>/dev/null; then
      PI="$USER@$IP"
      echo "Found Pi at $IP ✓"
      break
    fi
  done
fi

if [ -z "$PI" ]; then
  echo ""
  echo "ERROR: Cannot reach Pi. Make sure:"
  echo "  - Pi is powered on (red/green LEDs lit)"
  echo "  - You're connected to Apple Pi or GoGoVan WiFi"
  exit 1
fi

echo ""
echo "=== Deploying files to $PI ==="

echo "Copying rope-light.py..."
sshpass -p "$PASS" scp "$SCRIPT_DIR/rope-light.py" "$PI:~/rope-light.py" && echo "  ✓ rope-light.py" || echo "  ✗ FAILED"

echo "Copying starlink-bridge.py..."
sshpass -p "$PASS" scp "$SCRIPT_DIR/starlink-bridge.py" "$PI:~/starlink-bridge.py" && echo "  ✓ starlink-bridge.py" || echo "  ✗ FAILED"

echo "Copying can-bridge.py..."
sshpass -p "$PASS" scp "$SCRIPT_DIR/can-bridge.py" "$PI:~/can-bridge.py" && echo "  ✓ can-bridge.py" || echo "  ✗ FAILED"

echo "Copying index.html..."
sshpass -p "$PASS" scp "$SCRIPT_DIR/index.html" "$PI:~/index.html" && echo "  ✓ index.html" || echo "  ✗ FAILED"

echo ""
echo "=== Restarting services ==="
sshpass -p "$PASS" ssh "$PI" 'echo windows | sudo -S systemctl restart rope-light can-bridge starlink-bridge && echo "Services restarted ✓"'

echo ""
echo "=== Service status ==="
sleep 3
sshpass -p "$PASS" ssh "$PI" 'sudo systemctl is-active rope-light can-bridge starlink-bridge'

echo ""
echo "=== DONE ==="
