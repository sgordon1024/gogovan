#!/usr/bin/env python3
"""
run-speedtest.py — Runs the official Ookla speedtest CLI and publishes result to MQTT.
Triggered by the systemd speedtest.timer every 30 minutes, and also
by manual "Run Speed Test" taps in the dashboard (via can-bridge.py).

Uses the official Ookla speedtest binary (multi-stream, accurate) with a fallback
to speedtest-cli (Python package) if the Ookla binary isn't found.
"""
import json, subprocess, os, shutil, socket
import paho.mqtt.client as mqtt

class _OoklaPortError(Exception):
    """Raised when Ookla fails with a socket/connect error — triggers HTTPS fallback."""
    pass

def has_internet(timeout=4):
    """Quick TCP check against two well-known hosts on port 443."""
    for host in ('1.1.1.1', '8.8.8.8'):
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(timeout)
            s.connect((host, 443))
            s.close()
            return True
        except Exception:
            pass
    return False

MQTT_HOST     = 'localhost'
MQTT_PORT     = 1883
UPSTREAM_FILE = '/tmp/gogovan_upstream'

def get_upstream():
    """Detect current upstream (tmobile / starlink / unknown) from the live
    wlan0 NetworkManager connection name (starlink-bridge owns the switching)."""
    try:
        r = subprocess.run(['nmcli', '-g', 'DEVICE,CONNECTION', 'device', 'status'],
                           capture_output=True, text=True, timeout=10)
        for line in r.stdout.splitlines():
            parts = line.strip().split(':')
            if len(parts) >= 2 and parts[0] == 'wlan0':
                conn = parts[1].lower()
                if conn == 'preconfigured':
                    return 'tmobile'
                if any(k in conn for k in ('philadelphia', 'collins', 'blaster', 'starlink')):
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
        raise _OoklaPortError(last_err)
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


def run_https_speedtest():
    """
    HTTPS-only fallback using Cloudflare speed test endpoints (port 443).
    Used when Ookla fails due to port 8080 being blocked.
    Returns (download_mbps, upload_mbps, ping_ms, server_name, timestamp).
    """
    import urllib.request, time, datetime

    server_name = 'Cloudflare (HTTPS)'
    timestamp   = datetime.datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%SZ')

    # Ping: measure latency to Cloudflare
    ping_ms = None
    try:
        t0 = time.time()
        urllib.request.urlopen('https://speed.cloudflare.com/__down?bytes=1', timeout=5)
        ping_ms = round((time.time() - t0) * 1000)
    except Exception:
        pass

    UA = 'Mozilla/5.0 (compatible; GoGoVan-SpeedTest/1.0)'

    # Download: measure for up to 15 s regardless of how much arrives
    down_mbps = None
    try:
        BUDGET = 15
        t0 = time.time()
        received = 0
        req = urllib.request.Request(
            'https://speed.cloudflare.com/__down?bytes=10000000',
            headers={'User-Agent': UA}
        )
        with urllib.request.urlopen(req, timeout=BUDGET + 5) as r:
            while time.time() - t0 < BUDGET:
                chunk = r.read(65536)
                if not chunk:
                    break
                received += len(chunk)
        elapsed = time.time() - t0
        if received > 0 and elapsed > 0:
            down_mbps = round(received * 8 / elapsed / 1_000_000, 1)
    except Exception:
        pass

    # Upload: POST up to 15 s worth of data
    up_mbps = None
    try:
        BUDGET = 15
        upload_data = b'0' * 5_000_000
        t0 = time.time()
        req = urllib.request.Request(
            'https://speed.cloudflare.com/__up',
            data=upload_data,
            headers={'Content-Type': 'application/octet-stream', 'User-Agent': UA},
            method='POST'
        )
        urllib.request.urlopen(req, timeout=BUDGET + 5)
        elapsed = time.time() - t0
        if elapsed > 0:
            up_mbps = round(len(upload_data) * 8 / elapsed / 1_000_000, 1)
    except Exception:
        pass

    if down_mbps is None:
        raise ValueError('HTTPS speed test failed — no internet connection')

    return down_mbps, up_mbps or 0, ping_ms or 0, server_name, timestamp


client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION1)
client.connect(MQTT_HOST, MQTT_PORT, 60)
client.publish('van/status/network/speedtest/running', 'true', retain=True)
client.disconnect()

upstream = get_upstream()
try:
    # Prefer the official Ookla binary (multi-stream, accurate)
    try:
        if shutil.which('speedtest'):
            download, upload, ping, server, timestamp = run_ookla()
        else:
            download, upload, ping, server, timestamp = run_speedtest_cli()
    except _OoklaPortError as e:
        # Ookla failed — fall back to HTTPS-only test via Cloudflare
        print(f'Ookla unavailable ({e}), falling back to HTTPS speed test…')
        download, upload, ping, server, timestamp = run_https_speedtest()

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
