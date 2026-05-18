#!/bin/bash
# Deploy updated can-bridge.py and index.html to GoGoVan Pi
# Run this when connected to the GoGoVan hotspot (192.168.12.122)

PI="sgordon1024@100.98.52.107"
PASS="windows"
DIR="$(cd "$(dirname "$0")" && pwd)"

echo "=== Copying can-bridge.py ==="
sshpass -p "$PASS" scp "$DIR/can-bridge.py" "$PI:~/can-bridge.py" || { echo "FAILED: can-bridge.py copy"; exit 1; }

echo "=== Adding sudoers entry ==="
sshpass -p "$PASS" ssh "$PI" 'echo windows | sudo -S bash -c "echo \"sgordon1024 ALL=(ALL) NOPASSWD: /usr/bin/nmcli\" > /etc/sudoers.d/gogovan-nmcli && chmod 440 /etc/sudoers.d/gogovan-nmcli" && echo "sudoers ok"' || echo "WARNING: sudoers may already exist"

echo "=== Restarting can-bridge service ==="
sshpass -p "$PASS" ssh "$PI" 'echo windows | sudo -S systemctl restart can-bridge && echo "Service restarted"' || { echo "FAILED: service restart"; exit 1; }

echo "=== Copying index.html ==="
sshpass -p "$PASS" scp "$DIR/index.html" "$PI:~/index.html" || { echo "FAILED: index.html copy"; exit 1; }

echo "=== Verifying can-bridge is running ==="
sleep 3
sshpass -p "$PASS" ssh "$PI" 'sudo systemctl status can-bridge --no-pager -l | head -20'

echo "=== Checking subscription in logs ==="
sshpass -p "$PASS" ssh "$PI" 'sudo journalctl -u can-bridge -n 20 --no-pager'

echo "=== DONE ==="
