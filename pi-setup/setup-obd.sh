#!/bin/bash
# setup-obd.sh — One-time Pi setup for OBD-II integration
# Run from the project root: bash pi-setup/setup-obd.sh
# Requires: vehicle on, vGate iCar Pro BT3 plugged into OBD-II port

PASS="windows"
PI_HOST=""

# Auto-detect Pi
echo "=== Detecting Pi ==="
for host in "sgordon1024@100.98.52.107" "sgordon1024@vanpi.local" "sgordon1024@192.168.8.106"; do
  if sshpass -p "$PASS" ssh -o ConnectTimeout=5 -o StrictHostKeyChecking=no "$host" "echo ok" &>/dev/null; then
    PI_HOST="$host"; echo "→ Found Pi at $host"; break
  fi
done

if [ -z "$PI_HOST" ]; then
  echo "ERROR: Cannot reach Pi. Connect to GoGoVan Wi-Fi or Tailscale first."
  exit 1
fi

# All sudo commands use -S so the password can be piped non-interactively
ssh_pi()      { sshpass -p "$PASS" ssh -o StrictHostKeyChecking=no "$PI_HOST" "$@"; }
ssh_pi_sudo() { sshpass -p "$PASS" ssh -o StrictHostKeyChecking=no "$PI_HOST" "echo $PASS | sudo -S $*"; }

echo ""
echo "=== Installing python-obd ==="
ssh_pi "echo $PASS | sudo -S pip3 install --break-system-packages obd && echo 'python-obd installed'"

echo ""
echo "=== Adding sgordon1024 to dialout group (for /dev/rfcomm0) ==="
ssh_pi_sudo "usermod -a -G dialout sgordon1024 && echo 'dialout ok'"

echo ""
echo "=== Scanning for vGate iCar Pro BT3 ==="
echo "    Make sure the adapter is plugged in and the vehicle ignition is ON."
echo "    Scanning 15 seconds…"
echo ""

# Scan — grab all NEW Device lines so user can identify the adapter
SCAN_OUT=$(ssh_pi 'timeout 15 bluetoothctl scan on 2>&1 | grep -E "\[NEW\] Device"' 2>/dev/null)

# Try to auto-match known vGate names
MAC=$(echo "$SCAN_OUT" | grep -iE "OBD|vGate|ICAR|ELM|ICP" | grep -oE "([0-9A-F]{2}:){5}[0-9A-F]{2}" | head -1)

if [ -z "$MAC" ]; then
  echo "    Devices found during scan:"
  if [ -z "$SCAN_OUT" ]; then
    echo "    (none — make sure ignition is on and adapter has power)"
  else
    echo "$SCAN_OUT"
  fi
  echo ""
  echo "    Tip: the vGate typically shows up as 'OBDII' or a MAC starting with"
  echo "    a manufacturer prefix. If you see a new unknown device, that's likely it."
  echo ""
  read -p "Enter the MAC address of the vGate adapter (XX:XX:XX:XX:XX:XX): " MAC
fi

if [ -z "$MAC" ]; then
  echo "ERROR: No MAC address provided. Exiting."
  exit 1
fi

# Normalize to uppercase
MAC=$(echo "$MAC" | tr '[:lower:]' '[:upper:]')
echo "→ Using MAC: $MAC"

echo ""
echo "=== Pairing and trusting $MAC ==="
ssh_pi "bluetoothctl pair $MAC 2>&1 | tail -3"
ssh_pi "bluetoothctl trust $MAC 2>&1 | tail -2"

echo ""
echo "=== Binding rfcomm0 ==="
ssh_pi_sudo "rfcomm release /dev/rfcomm0 2>/dev/null; rfcomm bind /dev/rfcomm0 $MAC 1 && echo 'rfcomm0 bound OK'"

echo ""
echo "=== Writing systemd service files ==="

# rfcomm-obd.service
ssh_pi "echo $PASS | sudo -S tee /etc/systemd/system/rfcomm-obd.service > /dev/null" << SVCEOF
[Unit]
Description=Bind OBD Bluetooth adapter to /dev/rfcomm0
After=bluetooth.target
Wants=bluetooth.target

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/usr/bin/rfcomm bind /dev/rfcomm0 ${MAC} 1
ExecStop=/usr/bin/rfcomm release /dev/rfcomm0

[Install]
WantedBy=multi-user.target
SVCEOF
echo "rfcomm-obd.service written"

# obd-bridge.service
ssh_pi "echo $PASS | sudo -S tee /etc/systemd/system/obd-bridge.service > /dev/null" << SVCEOF
[Unit]
Description=GoGoVan OBD Bridge
After=rfcomm-obd.service mosquitto.service
Requires=rfcomm-obd.service

[Service]
User=sgordon1024
ExecStart=/usr/bin/python3 /home/sgordon1024/obd-bridge.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
SVCEOF
echo "obd-bridge.service written"

echo ""
echo "=== Enabling and starting services ==="
ssh_pi_sudo "systemctl daemon-reload && systemctl enable rfcomm-obd obd-bridge && systemctl start rfcomm-obd && echo 'services enabled'"

echo ""
echo "=== Deploying obd-bridge.py ==="
SCRIPT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
sshpass -p "$PASS" scp "$SCRIPT_DIR/obd-bridge.py" "$PI_HOST:~/obd-bridge.py" && echo "obd-bridge.py copied"

echo ""
echo "=== Starting obd-bridge ==="
ssh_pi_sudo "systemctl start obd-bridge && echo 'obd-bridge started'"

echo ""
echo "=== Waiting 8s for adapter to connect… ==="
sleep 8

echo ""
echo "=== Service status ==="
ssh_pi_sudo "systemctl status obd-bridge --no-pager -l | head -25"

echo ""
echo "=== Live MQTT (5 seconds) ==="
ssh_pi 'timeout 5 mosquitto_sub -h localhost -t "van/status/obd/#" -v 2>/dev/null || echo "(no MQTT yet)"'

echo ""
echo "=== DONE ==="
echo "  If you see 'van/status/obd/connected ok' above, it's working."
echo "  Now run: ./deploy-to-pi.sh   to push the Engine tab to the dashboard."
