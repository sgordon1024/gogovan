#!/bin/bash
# Fix hostapd to auto-restart after WiFi driver crashes
PASS="windows"

# Auto-detect Pi
if sshpass -p "$PASS" ssh -o ConnectTimeout=5 -o StrictHostKeyChecking=no "sgordon1024@100.98.52.107" "echo ok" &>/dev/null; then
  PI="sgordon1024@100.98.52.107"
elif sshpass -p "$PASS" ssh -o ConnectTimeout=5 -o StrictHostKeyChecking=no "sgordon1024@vanpi.local" "echo ok" &>/dev/null; then
  PI="sgordon1024@vanpi.local"
elif sshpass -p "$PASS" ssh -o ConnectTimeout=5 -o StrictHostKeyChecking=no "sgordon1024@192.168.4.1" "echo ok" &>/dev/null; then
  PI="sgordon1024@192.168.4.1"
else
  echo "ERROR: Cannot reach Pi"; exit 1
fi
echo "→ Using $PI"

echo "=== Installing hostapd auto-restart ==="
sshpass -p "$PASS" ssh "$PI" 'echo windows | sudo -S mkdir -p /etc/systemd/system/hostapd.service.d && echo windows | sudo -S tee /etc/systemd/system/hostapd.service.d/restart.conf > /dev/null << EOF
[Service]
Restart=always
RestartSec=5
EOF
echo windows | sudo -S systemctl daemon-reload && echo "hostapd override installed"'

echo "=== Setting fixed WiFi country code (prevents regdom crashes) ==="
sshpass -p "$PASS" ssh "$PI" 'grep -q "^country_code=" /etc/hostapd/gogovan.conf || (echo windows | sudo -S sed -i "s/^interface=uap0/country_code=US\ninterface=uap0/" /etc/hostapd/gogovan.conf && echo "country_code added") || echo "already set"'

echo "=== Verifying ==="
sshpass -p "$PASS" ssh "$PI" 'cat /etc/systemd/system/hostapd.service.d/restart.conf && systemctl show hostapd --property=Restart'

echo "=== DONE ==="
