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
import json, os, subprocess, time
import paho.mqtt.client as mqtt

MQTT_HOST    = 'localhost'
MQTT_PORT    = 1883
HISTORY_FILE = '/home/sgordon1024/speed-history.ndjson'

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

def append_history(result):
    if result.get('download') is None:
        return
    entry = {
        'ts':       int(time.time() * 1000),
        'isoTs':    result.get('timestamp', ''),
        'upstream': result.get('upstream', 'unknown'),
        'down':     result.get('download'),
        'up':       result.get('upload'),
        'ping':     result.get('ping'),
        'server':   result.get('server', ''),
    }
    try:
        with open(HISTORY_FILE, 'a') as f:
            f.write(json.dumps(entry) + '\n')
        one_year_ago = int(time.time() * 1000) - 365 * 24 * 60 * 60 * 1000
        with open(HISTORY_FILE, 'r') as f:
            first = f.readline().strip()
        if first and json.loads(first).get('ts', 0) < one_year_ago:
            with open(HISTORY_FILE, 'r') as f:
                lines = f.readlines()
            kept = []
            for line in lines:
                line = line.strip()
                if not line: continue
                try:
                    if json.loads(line).get('ts', 0) >= one_year_ago:
                        kept.append(line)
                except Exception:
                    pass
            with open(HISTORY_FILE, 'w') as f:
                f.write('\n'.join(kept) + ('\n' if kept else ''))
    except Exception as e:
        print(f'History write failed: {e}')

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

append_history(result)

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
