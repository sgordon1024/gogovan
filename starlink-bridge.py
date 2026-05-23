#!/usr/bin/env python3
"""
starlink-bridge.py — Starlink smart plug + GL.iNet repeater auto-switch bridge.

Switches the GL.iNet GL-MT3000 (Apple Pi) repeater between T-Mobile MiFi and
Starlink (WiFi Blaster) based on speed test results. Controls Starlink power
via Tuya smart plug.

Auto-switch logic:
  Default state: GL.iNet on T-Mobile, Starlink plug OFF.
  Speed test arrives < SPEED_THRESHOLD Mbps (T-Mobile):
      → Power Starlink plug ON
      → Wait STARLINK_BOOT_SECS for dish to come up
      → Switch GL.iNet repeater to WiFi Blaster
  While on Starlink:
      → Test T-Mobile speed via wlan0 every TMOBILE_CHECK_INTERVAL seconds
        (uses wlan0 directly so Starlink data is not consumed for T-Mobile checks)
      → If T-Mobile test ≥ SPEED_THRESHOLD Mbps: switch GL.iNet back to T-Mobile
        → Power Starlink plug OFF
  Starlink speed tested by normal 30-min timer (via eth0/GL.iNet) — no extra tests.

MQTT topics (subscribe):
  van/starlink/power          — "on" / "off"   manual plug control
  van/starlink/auto           — "on" / "off"   enable/disable auto-switch
  van/starlink/threshold      — Mbps value (default 5)
  van/status/network/speedtest — JSON speed test results (trigger for auto-switch)

MQTT topics (publish, retained):
  van/status/starlink/power          — "on" / "off" / "unknown"
  van/status/starlink/auto           — "on" / "off"
  van/status/starlink/threshold      — Mbps value
  van/status/starlink/quality        — "good" / "poor" / "unknown"
  van/status/network/upstream        — "tmobile" / "starlink"
"""

import json
import os
import subprocess
import threading
import time

import paho.mqtt.client as mqtt
import tinytuya

# ── Configuration ──────────────────────────────────────────────────────────
MQTT_HOST = "localhost"
MQTT_PORT = 1883

PLUG_DEV_ID    = "eb21e6caef01e8582972u9"
PLUG_LOCAL_KEY = "knGT9!<jN3jA~npU"
PLUG_VERSION   = 3.3

ROUTER_HOST       = "root@192.168.8.1"
TMOBILE_SSID      = "tmobile"
STARLINK_SSID     = "WiFi Blaster"
WIFI_PASSWORD     = "1234567890"

THRESH_FILE             = os.path.expanduser("~/.starlink_speed_threshold")
UPSTREAM_FILE           = "/tmp/gogovan_upstream"   # shared with can-bridge / run-speedtest
DEFAULT_SPEED_THRESH    = 5      # Mbps: switch to Starlink if T-Mobile below this
STARLINK_BOOT_SECS      = 60     # wait after powering plug before connecting repeater
TMOBILE_CHECK_INTERVAL  = 1800   # 30 min: how often to test T-Mobile while on Starlink
QUALITY_CHECK_INTERVAL  = 300    # 5 min: ping quality check when on Starlink
QUALITY_PING_TIMEOUT    = 15

# States
STATE_TMOBILE            = "tmobile"
STATE_SWITCHING_STARLINK = "switching_to_starlink"
STATE_STARLINK           = "starlink"
STATE_SWITCHING_TMOBILE  = "switching_to_tmobile"

# ── State ──────────────────────────────────────────────────────────────────
state              = STATE_TMOBILE
starlink_power     = None    # "on" / "off" / None
auto_mode          = False
speed_threshold    = DEFAULT_SPEED_THRESH
plug_address       = None    # discovered at startup
switch_lock        = threading.Lock()
last_tmobile_check = 0.0
last_quality_check = 0.0
mqtt_client_ref    = None

# ── Persistence ────────────────────────────────────────────────────────────

def load_threshold() -> int:
    try:
        return max(1, min(100, int(open(THRESH_FILE).read().strip())))
    except Exception:
        return DEFAULT_SPEED_THRESH

def save_threshold(val: int):
    try:
        with open(THRESH_FILE, "w") as f:
            f.write(str(val))
    except Exception as e:
        print(f"save_threshold error: {e}")

def write_upstream_file(upstream: str):
    """Write current upstream to shared file for can-bridge / run-speedtest."""
    try:
        with open(UPSTREAM_FILE, "w") as f:
            f.write(upstream)
    except Exception as e:
        print(f"write_upstream_file error: {e}")

# ── GL.iNet router control ─────────────────────────────────────────────────

def router_ssh(cmd: str, timeout: int = 20) -> tuple:
    """Run a command on GL.iNet via SSH. Returns (stdout, returncode)."""
    try:
        r = subprocess.run(
            ["ssh", "-o", "StrictHostKeyChecking=no", "-o", "ConnectTimeout=10",
             ROUTER_HOST, cmd],
            capture_output=True, text=True, timeout=timeout
        )
        return r.stdout.strip(), r.returncode
    except Exception as e:
        print(f"router_ssh error: {e}")
        return "", 1

def router_repeater_status() -> dict:
    """Get GL.iNet repeater status. Returns dict or None."""
    out, rc = router_ssh("ubus call repeater status", timeout=10)
    if out:
        try:
            return json.loads(out)
        except Exception:
            pass
    return None

def router_repeater_connect(ssid: str, key: str) -> bool:
    """Switch GL.iNet repeater to given SSID. Returns True on success."""
    params = json.dumps({"ssid": ssid, "key": key, "network": "wwan", "remember": True})
    cmd = f"ubus call repeater connect '{params}'"
    router_ssh(cmd, timeout=20)
    # Wait for connection to establish
    for _ in range(6):
        time.sleep(5)
        status = router_repeater_status()
        if status and status.get("ssid") == ssid and status.get("state_s") == "connected":
            return True
    return False

def get_router_upstream() -> str:
    """Determine current upstream from GL.iNet repeater status."""
    status = router_repeater_status()
    if not status:
        return "unknown"
    ssid = status.get("ssid", "")
    connected = status.get("state_s") == "connected"
    if not connected:
        return "unknown"
    if ssid == STARLINK_SSID:
        return "starlink"
    if ssid == TMOBILE_SSID:
        return "tmobile"
    return "unknown"

# ── Tuya plug control ──────────────────────────────────────────────────────

def find_plug_ip() -> str:
    """Scan local network for Tuya plug by device ID."""
    print("Scanning for Tuya plug...")
    try:
        devices = tinytuya.deviceScan(maxretry=8, verbose=False)
        for ip, info in devices.items():
            if info.get("gwId") == PLUG_DEV_ID:
                print(f"Found plug at {ip}")
                return ip
    except Exception as e:
        print(f"find_plug_ip error: {e}")
    return None

def make_plug(ip: str):
    d = tinytuya.OutletDevice(
        dev_id=PLUG_DEV_ID,
        address=ip,
        local_key=PLUG_LOCAL_KEY,
        version=PLUG_VERSION
    )
    d.set_socketTimeout(5)
    d.set_socketRetryLimit(2)
    return d

def plug_get_state() -> str:
    """Query plug power state. Returns 'on', 'off', or 'unknown'."""
    if not plug_address:
        return "unknown"
    try:
        d = make_plug(plug_address)
        status = d.status()
        if "dps" in status:
            return "on" if status["dps"].get("1", False) else "off"
    except Exception as e:
        print(f"plug_get_state error: {e}")
    return "unknown"

def plug_set(on: bool) -> bool:
    """Set plug power state. Returns True on success."""
    global starlink_power
    if not plug_address:
        print("plug_set: no plug address known, skipping")
        return False
    try:
        d = make_plug(plug_address)
        result = d.set_value(1, on)
        if "Error" not in str(result):
            starlink_power = "on" if on else "off"
            print(f"Plug → {'ON' if on else 'OFF'}")
            return True
        print(f"plug_set error: {result}")
    except Exception as e:
        print(f"plug_set exception: {e}")
    return False

# ── Speed testing ──────────────────────────────────────────────────────────

def get_wlan0_ip() -> str:
    """Get Pi's wlan0 IP (T-Mobile direct connection)."""
    try:
        r = subprocess.run(["ip", "addr", "show", "wlan0"],
                           capture_output=True, text=True)
        for line in r.stdout.splitlines():
            if "inet " in line:
                return line.strip().split()[1].split("/")[0]
    except Exception:
        pass
    return None

def run_tmobile_speedtest() -> float:
    """
    Speed test via wlan0 (T-Mobile MiFi direct). Returns download Mbps or None.
    Publishes result to MQTT as upstream=tmobile so it appears in stats history.
    """
    global mqtt_client_ref
    wlan0_ip = get_wlan0_ip()
    if not wlan0_ip:
        print("T-Mobile check: no wlan0 IP")
        return None
    print(f"T-Mobile speed check via wlan0 ({wlan0_ip})...")
    try:
        r = subprocess.run(
            ["speedtest-cli", "--json", "--secure", "--source", wlan0_ip],
            capture_output=True, text=True, timeout=120
        )
        data = json.loads(r.stdout)
        dl   = round(data["download"] / 1e6, 1)
        ul   = round(data["upload"]   / 1e6, 1)
        ping = round(data["ping"])
        server = data.get("server", {}).get("sponsor", "Unknown")
        print(f"T-Mobile via wlan0: ↓{dl} ↑{ul} ping={ping}ms")
        if mqtt_client_ref:
            result = {
                "download":  dl,
                "upload":    ul,
                "ping":      ping,
                "server":    server,
                "upstream":  "tmobile",
                "timestamp": data.get("timestamp", ""),
                "error":     None
            }
            mqtt_client_ref.publish(
                "van/status/network/speedtest", json.dumps(result), retain=True)
        return dl
    except Exception as e:
        print(f"T-Mobile speedtest error: {e}")
        return None

def check_connectivity() -> str:
    """Ping 8.8.8.8 three times. Returns 'good', 'poor', or 'unknown'."""
    try:
        r = subprocess.run(
            ["ping", "-c", "3", "-W", "3", "-q", "8.8.8.8"],
            capture_output=True, text=True, timeout=QUALITY_PING_TIMEOUT
        )
        if r.returncode != 0:
            return "poor"
        for line in r.stdout.splitlines():
            if "/" in line and ("rtt" in line or "round-trip" in line):
                try:
                    avg_ms = float(line.split("=")[1].strip().split("/")[1])
                    return "poor" if avg_ms > 2000 else "good"
                except Exception:
                    pass
        return "good"
    except Exception as e:
        print(f"check_connectivity error: {e}")
        return "unknown"

# ── State transitions ──────────────────────────────────────────────────────

def switch_to_starlink():
    """Background thread: T-Mobile → Starlink."""
    global state, starlink_power
    if not switch_lock.acquire(blocking=False):
        print("switch_to_starlink: switch already in progress")
        return
    try:
        print("Auto-switch: T-Mobile speed low → starting Starlink sequence")
        state = STATE_SWITCHING_STARLINK

        # 1. Power on Starlink plug
        if plug_set(True):
            if mqtt_client_ref:
                mqtt_client_ref.publish(
                    "van/status/starlink/power", "on", retain=True)
        else:
            print("Warning: could not confirm Starlink plug ON")
            if mqtt_client_ref:
                mqtt_client_ref.publish(
                    "van/status/starlink/power", "on", retain=True)

        # 2. Wait for Starlink dish to come up
        print(f"Waiting {STARLINK_BOOT_SECS}s for Starlink to boot...")
        time.sleep(STARLINK_BOOT_SECS)

        # 3. Switch GL.iNet repeater to WiFi Blaster
        print("Switching GL.iNet to WiFi Blaster (Starlink)...")
        if router_repeater_connect(STARLINK_SSID, WIFI_PASSWORD):
            state = STATE_STARLINK
            write_upstream_file("starlink")
            if mqtt_client_ref:
                mqtt_client_ref.publish(
                    "van/status/network/upstream", "starlink", retain=True)
            print("✓ Now on Starlink")
        else:
            print("✗ Failed to connect to WiFi Blaster — staying on T-Mobile")
            state = STATE_TMOBILE
            # Power Starlink back off since we couldn't connect
            if plug_set(False):
                if mqtt_client_ref:
                    mqtt_client_ref.publish(
                        "van/status/starlink/power", "off", retain=True)
    finally:
        switch_lock.release()

def switch_to_tmobile():
    """Background thread: Starlink → T-Mobile."""
    global state
    if not switch_lock.acquire(blocking=False):
        print("switch_to_tmobile: switch already in progress")
        return
    try:
        print("Auto-switch: T-Mobile speed good → switching back to T-Mobile")
        state = STATE_SWITCHING_TMOBILE

        # 1. Switch GL.iNet repeater to T-Mobile
        print("Switching GL.iNet to T-Mobile...")
        if router_repeater_connect(TMOBILE_SSID, WIFI_PASSWORD):
            state = STATE_TMOBILE
            write_upstream_file("tmobile")
            if mqtt_client_ref:
                mqtt_client_ref.publish(
                    "van/status/network/upstream", "tmobile", retain=True)
            print("✓ Now on T-Mobile")

            # 2. Power off Starlink
            print("Powering off Starlink...")
            if plug_set(False):
                if mqtt_client_ref:
                    mqtt_client_ref.publish(
                        "van/status/starlink/power", "off", retain=True)
                    mqtt_client_ref.publish(
                        "van/status/starlink/quality", "unknown", retain=True)
        else:
            print("✗ Failed to connect to T-Mobile — staying on Starlink")
            state = STATE_STARLINK
    finally:
        switch_lock.release()

# ── Monitor loop ───────────────────────────────────────────────────────────

def monitor_loop(client):
    """Background loop: T-Mobile checks when on Starlink, quality pings."""
    global last_tmobile_check, last_quality_check

    while True:
        time.sleep(30)
        try:
            now = time.time()

            if state == STATE_STARLINK:
                # T-Mobile speed check to decide when to switch back
                if now - last_tmobile_check >= TMOBILE_CHECK_INTERVAL:
                    last_tmobile_check = now
                    dl = run_tmobile_speedtest()
                    if dl is not None and auto_mode and dl >= speed_threshold:
                        print(f"T-Mobile recovered: {dl} Mbps ≥ {speed_threshold} Mbps → switching back")
                        t = threading.Thread(target=switch_to_tmobile, daemon=True)
                        t.start()

                # Quality ping
                if now - last_quality_check >= QUALITY_CHECK_INTERVAL:
                    last_quality_check = now
                    quality = check_connectivity()
                    print(f"Starlink quality → {quality}")
                    client.publish("van/status/starlink/quality", quality, retain=True)

        except Exception as e:
            print(f"monitor_loop error: {e}")

# ── MQTT callbacks ─────────────────────────────────────────────────────────

def on_connect(client, userdata, flags, rc):
    global state, starlink_power, auto_mode, speed_threshold, plug_address, mqtt_client_ref
    print(f"MQTT connected (rc={rc})")
    mqtt_client_ref = client

    client.subscribe("van/starlink/power")
    client.subscribe("van/starlink/auto")
    client.subscribe("van/starlink/threshold")
    client.subscribe("van/status/network/speedtest")

    # Discover plug
    plug_address = find_plug_ip()
    if not plug_address:
        print("Warning: Tuya plug not found on network")

    # Read current router upstream
    upstream = get_router_upstream()
    if upstream == "starlink":
        state = STATE_STARLINK
    else:
        state = STATE_TMOBILE
        upstream = "tmobile"
    write_upstream_file(upstream)
    print(f"Initial upstream: {upstream}")

    # Read plug state
    pw = plug_get_state()
    starlink_power = pw if pw != "unknown" else None
    client.publish("van/status/starlink/power",
                   starlink_power or "unknown", retain=True)

    # Publish initial status
    client.publish("van/status/network/upstream", upstream, retain=True)
    client.publish("van/status/starlink/auto",
                   "on" if auto_mode else "off", retain=True)
    client.publish("van/status/starlink/threshold",
                   str(speed_threshold), retain=True)
    if starlink_power != "on":
        client.publish("van/status/starlink/quality", "unknown", retain=True)

    print(f"Initial: state={state}, plug={starlink_power}, "
          f"threshold={speed_threshold} Mbps, auto={auto_mode}")

    # Start monitor loop
    t = threading.Thread(target=monitor_loop, args=(client,), daemon=True)
    t.start()


def on_message(client, userdata, msg):
    global starlink_power, auto_mode, speed_threshold
    topic   = msg.topic
    payload = msg.payload.decode().strip()
    lower   = payload.lower()

    # ── Manual plug control ──────────────────────────────────────────────
    if topic == "van/starlink/power":
        print(f"Manual: Starlink plug → {lower}")
        on = (lower == "on")
        if plug_set(on):
            client.publish("van/status/starlink/power",
                           "on" if on else "off", retain=True)
            if not on:
                client.publish("van/status/starlink/quality",
                               "unknown", retain=True)
        return

    # ── Auto-switch toggle ───────────────────────────────────────────────
    if topic == "van/starlink/auto":
        auto_mode = (lower == "on")
        client.publish("van/status/starlink/auto",
                       "on" if auto_mode else "off", retain=True)
        print(f"Auto-mode → {auto_mode}")
        return

    # ── Speed threshold ──────────────────────────────────────────────────
    if topic == "van/starlink/threshold":
        try:
            val = max(1, min(100, int(payload)))
            speed_threshold = val
            save_threshold(val)
            client.publish("van/status/starlink/threshold", str(val), retain=True)
            print(f"Speed threshold → {val} Mbps")
        except ValueError:
            pass
        return

    # ── Speed test result — main auto-switch trigger ─────────────────────
    if topic == "van/status/network/speedtest":
        if not auto_mode:
            return
        # Only trigger when we're currently on T-Mobile and not mid-switch
        if state not in (STATE_TMOBILE,):
            return
        try:
            data = json.loads(payload)
            dl       = data.get("download")
            upstream = data.get("upstream", "unknown")
            err      = data.get("error")
            if err or dl is None:
                return
            # Only act on T-Mobile results (not Starlink or wlan0 T-Mobile checks)
            # upstream=="tmobile" means it came through GL.iNet while on T-Mobile
            if upstream != "tmobile":
                return
            print(f"Speed test: {dl} Mbps via {upstream} (threshold: {speed_threshold} Mbps)")
            if dl < speed_threshold:
                print(f"Speed {dl} < {speed_threshold} Mbps → switching to Starlink")
                t = threading.Thread(target=switch_to_starlink, daemon=True)
                t.start()
        except Exception as e:
            print(f"speedtest handler error: {e}")
        return

# Need client ref for on_message too
client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION1)
client.on_connect = on_connect
client.on_message = on_message
client.connect(MQTT_HOST, MQTT_PORT, 60)
client.loop_forever()
