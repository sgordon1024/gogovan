#!/usr/bin/env python3
"""
run-speedtest.py — Runs the official Ookla speedtest CLI and publishes result to MQTT.
Triggered by the systemd speedtest.timer every 30 minutes, and also
by manual "Run Speed Test" taps in the dashboard (via can-bridge.py).

Uses the official Ookla speedtest binary (multi-stream, accurate) with a fallback
to speedtest-cli (Python package) if the Ookla binary isn't found.
"""
import json, subprocess, os, shutil
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


def run_ookla():
    """Run the official Ookla speedtest CLI. Returns parsed result dict.
    Retries up to 3 times on transient socket/connection failures."""
    last_err = None
    for attempt in range(3):
        r = subprocess.run(
            ['speedtest', '--format=json', '--accept-license', '--accept-gdpr'],
            capture_output=True, text=True, timeout=120
        )
        # Ookla emits multiple JSON lines; we want the one with "type": "result"
        data = None
        all_lines = (r.stdout + r.stderr).splitlines()
        for line in all_lines:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
                if obj.get('type') == 'result':
                    data = obj
                    break
            except json.JSONDecodeError:
                continue
        if data is not None:
            break
        # Collect the last unique error message for reporting if all retries fail
        err_msg = None
        for line in all_lines:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
                if obj.get('error'):
                    err_msg = obj['error']
                elif obj.get('type') == 'log' and obj.get('level') == 'error':
                    err_msg = obj.get('message', '')
            except json.JSONDecodeError:
                pass
        last_err = err_msg or f'exit code {r.returncode}'
    if data is None:
        raise ValueError(f'Speed test failed: {last_err}')
    # bandwidth is bytes/sec → Mbps
    download_mbps = round(data['download']['bandwidth'] * 8 / 1_000_000, 1)
    upload_mbps   = round(data['upload']['bandwidth']   * 8 / 1_000_000, 1)
    ping_ms       = round(data['ping']['latency'])
    server_name   = data.get('server', {}).get('name', 'Unknown')
    timestamp     = data.get('timestamp', '')
    return download_mbps, upload_mbps, ping_ms, server_name, timestamp


def run_speedtest_cli():
    """Fallback: run the Python speedtest-cli package."""
    r = subprocess.run(
        ['speedtest-cli', '--json', '--secure'],
        capture_output=True, text=True, timeout=120
    )
    data = json.loads(r.stdout)
    download_mbps = round(data['download'] / 1e6, 1)
    upload_mbps   = round(data['upload']   / 1e6, 1)
    ping_ms       = round(data['ping'])
    server_name   = data.get('server', {}).get('sponsor', 'Unknown')
    timestamp     = data.get('timestamp', '')
    return download_mbps, upload_mbps, ping_ms, server_name, timestamp


client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION1)
client.connect(MQTT_HOST, MQTT_PORT, 60)
client.publish('van/status/network/speedtest/running', 'true', retain=True)
client.disconnect()

upstream = get_upstream()
try:
    # Prefer the official Ookla binary (multi-stream, accurate)
    if shutil.which('speedtest'):
        download, upload, ping, server, timestamp = run_ookla()
    else:
        # Fallback to Python speedtest-cli (single-stream, may under-measure)
        download, upload, ping, server, timestamp = run_speedtest_cli()

    result = {
        'download':  download,
        'upload':    upload,
        'ping':      ping,
        'server':    server,
        'upstream':  upstream,
        'timestamp': timestamp,
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
