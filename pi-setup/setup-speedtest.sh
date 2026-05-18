#!/bin/bash
# Install speedtest-cli and set up 30-min auto-test timer on Pi
PASS="windows"
echo "=== Installing speedtest-cli ==="
echo $PASS | sudo -S apt-get install -y speedtest-cli

echo "=== Creating speedtest systemd service ==="
echo $PASS | sudo -S tee /etc/systemd/system/speedtest.service > /dev/null << 'UNIT'
[Unit]
Description=GoGoVan Internet Speed Test
[Service]
Type=oneshot
User=sgordon1024
ExecStart=/usr/bin/python3 /home/sgordon1024/run-speedtest.py
UNIT

echo "=== Creating 30-min timer ==="
echo $PASS | sudo -S tee /etc/systemd/system/speedtest.timer > /dev/null << 'UNIT'
[Unit]
Description=GoGoVan speed test every 30 minutes
[Timer]
OnBootSec=3min
OnUnitActiveSec=30min
Unit=speedtest.service
[Install]
WantedBy=timers.target
UNIT

echo "=== Writing run-speedtest.py ==="
cat > /home/sgordon1024/run-speedtest.py << 'PY'
#!/usr/bin/env python3
import json, subprocess
import paho.mqtt.client as mqtt

MQTT_HOST = 'localhost'
MQTT_PORT = 1883

def get_upstream():
    try:
        r = subprocess.run(['nmcli','-g','DEVICE,CONNECTION','device','status'],
                           capture_output=True, text=True)
        for line in r.stdout.splitlines():
            parts = line.strip().split(':')
            if len(parts) >= 2 and parts[0] == 'wlan0':
                conn = parts[1]
                if conn == 'preconfigured': return 'tmobile'
                if conn == 'wifi-blaster':  return 'starlink'
        return 'unknown'
    except: return 'unknown'

client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION1)
client.connect(MQTT_HOST, MQTT_PORT, 60)
client.publish('van/status/network/speedtest/running', 'true', retain=True)
client.disconnect()

upstream = get_upstream()
try:
    r = subprocess.run(['speedtest-cli','--json','--secure'],
                       capture_output=True, text=True, timeout=120)
    data = json.loads(r.stdout)
    result = {
        'download': round(data['download'] / 1e6, 1),
        'upload':   round(data['upload']   / 1e6, 1),
        'ping':     round(data['ping']),
        'server':   data.get('server', {}).get('sponsor', 'Unknown'),
        'upstream': upstream,
        'timestamp': data.get('timestamp', ''),
        'error': None
    }
except Exception as e:
    result = {'download':None,'upload':None,'ping':None,
              'server':None,'upstream':upstream,'timestamp':'','error':str(e)}

client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION1)
client.connect(MQTT_HOST, MQTT_PORT, 60)
client.publish('van/status/network/speedtest', json.dumps(result), retain=True)
client.publish('van/status/network/speedtest/running', 'false', retain=True)
client.disconnect()
print(json.dumps(result, indent=2))
PY
chmod +x /home/sgordon1024/run-speedtest.py

echo "=== Enabling timer ==="
echo $PASS | sudo -S systemctl daemon-reload
echo $PASS | sudo -S systemctl enable --now speedtest.timer
systemctl is-active speedtest.timer && echo "Timer active"
echo "=== Done — first test runs in 3 minutes ==="
