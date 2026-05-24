#!/usr/bin/env python3
"""
run-speedtest.py — Runs speedtest-cli and publishes result to MQTT.
Triggered by the systemd speedtest.timer every 30 minutes, and also
by manual "Run Speed Test" taps in the dashboard (via can-bridge.py).
"""
import json, subprocess, os
import paho.mqtt.client as mqtt

MQTT_HOST     = 'localhost'
MQTT_PORT     = 1883
UPSTREAM_FILE = '/tmp/gogovan_upstream'

def get_upstream():
    """
    Detect current upstream (tmobile / starlink / unknown).

    Primary: read /tmp/gogovan_upstream written by starlink-bridge.py —
    this is always accurate because starlink-bridge owns the switching logic.

    Fallback: nmcli connection name on wlan0 (for edge cases where
    starlink-bridge hasn't written the file yet).
    """
    try:
        val = open(UPSTREAM_FILE).read().strip().lower()
        if val in ('tmobile', 'starlink'):
            return val
    except Exception:
        pass
    # Fallback: check nmcli
    try:
        r = subprocess.run(['nmcli', '-g', 'DEVICE,CONNECTION', 'device', 'status'],
                           capture_output=True, text=True, timeout=10)
        for line in r.stdout.splitlines():
            parts = line.strip().split(':')
            if len(parts) >= 2 and parts[0] == 'wlan0':
                conn = parts[1].lower()
                if conn == 'preconfigured':
                    return 'tmobile'
                if 'blaster' in conn or 'starlink' in conn:
                    return 'starlink'
    except Exception:
        pass
    return 'unknown'


client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION1)
client.connect(MQTT_HOST, MQTT_PORT, 60)
client.publish('van/status/network/speedtest/running', 'true', retain=True)
client.disconnect()

upstream = get_upstream()
try:
    r = subprocess.run(
        ['speedtest-cli', '--json', '--secure'],
        capture_output=True, text=True, timeout=120
    )
    data = json.loads(r.stdout)
    result = {
        'download':  round(data['download'] / 1e6, 1),
        'upload':    round(data['upload']   / 1e6, 1),
        'ping':      round(data['ping']),
        'server':    data.get('server', {}).get('sponsor', 'Unknown'),
        'upstream':  upstream,
        'timestamp': data.get('timestamp', ''),
        'error':     None,
    }
except Exception as e:
    result = {
        'download': None, 'upload': None, 'ping': None,
        'server': None, 'upstream': upstream, 'timestamp': '', 'error': str(e),
    }

client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION1)
client.connect(MQTT_HOST, MQTT_PORT, 60)
client.publish('van/status/network/speedtest', json.dumps(result), retain=True)
client.publish('van/status/network/speedtest/running', 'false', retain=True)
client.disconnect()
print(json.dumps(result, indent=2))
