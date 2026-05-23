#!/usr/bin/env python3
"""
starlink-bridge.py — Starlink smart plug + network monitoring bridge.

Architecture:
  GL.iNet (Apple Pi) always stays connected to WiFi Blaster (Starlink) for
  client internet. The Pi's wlan0 stays connected to T-Mobile for Cerbo GX
  bridge. T-Mobile cannot serve internet through GL.iNet repeater, so we
  never switch GL.iNet to T-Mobile.

  On startup, if GL.iNet is not on WiFi Blaster, we switch it back.

Auto-switch logic:
  Default: Starlink plug ON, GL.iNet on WiFi Blaster.
  If auto mode enabled and T-Mobile speed (via wlan0) >= threshold:
      → Power OFF Starlink plug to save data cap.
      → GL.iNet will lose WiFi Blaster — attempts reconnect or goes offline.
        (Only disable if you know you can live without Apple Pi internet.)
  If T-Mobile speed < threshold (or auto just turned on and Starlink is off):
      → Power ON Starlink plug.
      → Wait STARLINK_BOOT_SECS for dish.
      → GL.iNet reconnects to WiFi Blaster automatically (remembered network).

Note: auto-switch only controls the Starlink PLUG. It does NOT switch the
GL.iNet repeater network. Starlink off = no WiFi Blaster = no Apple Pi internet.
Use auto mode only if you're OK with Apple Pi being offline when T-Mobile is good.

MQTT topics (subscribe):
  van/starlink/power          — "on" / "off"   manual plug control
  van/starlink/auto           — "on" / "off"   enable/disable auto-switch
  van/starlink/threshold      — Mbps value (default 5)
  van/status/network/speedtest — JSON speed test results (from run-speedtest.py timer)

MQTT topics (publish, retained):
  van/status/starlink/power          — "on" / "off" / "unknown"
  van/status/starlink/auto           — "on" / "off"
  van/status/starlink/threshold      — Mbps value
  van/status/starlink/quality        — "good" / "poor" / "unknown"
  van/status/network/upstream        — "starlink" / "tmobile" (display only)
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
STARLINK_SSID     = "WiFi Blaster"
TMOBILE_SSID      = "tmobile"
WIFI_PASSWORD     = "1234567890"

THRESH_FILE             = os.path.expanduser("~/.starlink_speed_threshold")
UPSTREAM_FILE           = "/tmp/gogovan_upstream"   # shared with can-bridge / run-speedtest
DEFAULT_SPEED_THRESH    = 5      # Mbps
STARLINK_BOOT_SECS      = 60     # wait after powering plug before GL.iNet reconnects
TMOBILE_CHECK_INTERVAL  = 1800   # 30 min: how often to test T-Mobile via wlan0
QUALITY_CHECK_INTERVAL  = 300    # 5 min: ping quality check when Starlink is on

# ── State ──────────────────────────────────────────────────────────────────
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

def router_repeater_connect(ssid: str, key: str, wait_secs: int = 30) -> bool:
    """Switch GL.iNet repeater to given SSID. Returns True on success."""
    params = json.dumps({"ssid": ssid, "key": key, "network": "wwan", "remember": True})
    cmd = f"ubus call repeater connect '{params}'"
    router_ssh(cmd, timeout=20)
    deadline = time.time() + wait_secs
    while time.time() < deadline:
        time.sleep(5)
        status = router_repeater_status()
        if status and status.get("ssid") == ssid and status.get("state_s") == "connected":
            return True
    return False

def ensure_on_starlink():
    """
    Check GL.iNet repeater and switch to WiFi Blaster if not already there.
    Called at startup to recover from any mis-state.
    """
    status = router_repeater_status()
    if not status:
        print("ensure_on_starlink: could not get GL.iNet status")
        return
    ssid       = status.get("ssid", "")
    state_s    = status.get("state_s", "")
    connected  = (state_s == "connected")
    if connected and ssid == STARLINK_SSID:
        print(f"GL.iNet already on {STARLINK_SSID} ✓")
        return
    print(f"GL.iNet is on '{ssid}' (state={state_s}) — switching to {STARLINK_SSID}...")
    ok = router_repeater_connect(STARLINK_SSID, WIFI_PASSWORD, wait_secs=40)
    if ok:
        print(f"GL.iNet switched to {STARLINK_SSID} ✓")
    else:
        print(f"Warning: could not switch GL.iNet to {STARLINK_SSID}")

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
    Speed test via wlan0 (T-Mobile direct — does not consume Starlink data).
    Returns download Mbps or None.
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
        dl     = round(data["download"] / 1e6, 1)
        ul     = round(data["upload"]   / 1e6, 1)
        ping   = round(data["ping"])
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
            capture_output=True, text=True, timeout=QUALITY_CHECK_INTERVAL
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

# ── Starlink plug power control (with GL.iNet reconnect) ──────────────────

def power_on_starlink():
    """
    Turn Starlink plug ON and wait for GL.iNet to reconnect to WiFi Blaster.
    Called when auto-switch decides T-Mobile is too slow.
    """
    global starlink_power
    print("Powering ON Starlink...")
    if not plug_set(True):
        print("Warning: could not confirm Starlink plug ON")
    if mqtt_client_ref:
        mqtt_client_ref.publish("van/status/starlink/power", "on", retain=True)

    print(f"Waiting {STARLINK_BOOT_SECS}s for Starlink dish to boot...")
    time.sleep(STARLINK_BOOT_SECS)

    # GL.iNet should auto-reconnect to WiFi Blaster (it remembers it).
    # Verify and force-reconnect if needed.
    print("Verifying GL.iNet is on WiFi Blaster...")
    ensure_on_starlink()

    write_upstream_file("starlink")
    if mqtt_client_ref:
        mqtt_client_ref.publish("van/status/network/upstream", "starlink", retain=True)
    print("✓ Starlink ON and GL.iNet on WiFi Blaster")

def power_off_starlink():
    """
    Turn Starlink plug OFF (GL.iNet will lose WiFi Blaster upstream).
    Only used when auto mode determines T-Mobile is fast enough.
    Apple Pi clients will lose internet when this is called.
    """
    global starlink_power
    print("Powering OFF Starlink...")
    if plug_set(False):
        if mqtt_client_ref:
            mqtt_client_ref.publish("van/status/starlink/power", "off", retain=True)
            mqtt_client_ref.publish("van/status/starlink/quality", "unknown", retain=True)
    # Upstream becomes T-Mobile (Pi's wlan0 for reference; GL.iNet has no internet)
    write_upstream_file("tmobile")
    if mqtt_client_ref:
        mqtt_client_ref.publish("van/status/network/upstream", "tmobile", retain=True)
    print("Starlink OFF — Apple Pi clients are now offline")

# ── Monitor loop ───────────────────────────────────────────────────────────

def monitor_loop(client):
    """
    Background loop:
    - Every 30 min: test T-Mobile speed via wlan0 (doesn't use Starlink data)
    - Every 5 min: ping quality check on Starlink
    - Auto mode: if T-Mobile fast → power off Starlink; if slow and off → power on
    """
    global last_tmobile_check, last_quality_check

    while True:
        time.sleep(30)
        try:
            now = time.time()

            # ── T-Mobile speed check ──────────────────────────────────────
            if now - last_tmobile_check >= TMOBILE_CHECK_INTERVAL:
                last_tmobile_check = now
                dl = run_tmobile_speedtest()

                if dl is not None and auto_mode:
                    if dl >= speed_threshold and starlink_power == "on":
                        print(f"Auto: T-Mobile fast ({dl} Mbps ≥ {speed_threshold}) → powering off Starlink")
                        t = threading.Thread(target=power_off_starlink, daemon=True)
                        t.start()
                    elif dl < speed_threshold and starlink_power == "off":
                        print(f"Auto: T-Mobile slow ({dl} Mbps < {speed_threshold}) → powering on Starlink")
                        t = threading.Thread(target=power_on_starlink, daemon=True)
                        t.start()

            # ── Starlink quality check (only when plug is on) ─────────────
            if starlink_power == "on" and now - last_quality_check >= QUALITY_CHECK_INTERVAL:
                last_quality_check = now
                quality = check_connectivity()
                print(f"Starlink quality → {quality}")
                client.publish("van/status/starlink/quality", quality, retain=True)

        except Exception as e:
            print(f"monitor_loop error: {e}")

# ── MQTT callbacks ─────────────────────────────────────────────────────────

def on_connect(client, userdata, flags, rc):
    global auto_mode, speed_threshold, plug_address, mqtt_client_ref
    print(f"MQTT connected (rc={rc})")
    mqtt_client_ref = client

    client.subscribe("van/starlink/power")
    client.subscribe("van/starlink/auto")
    client.subscribe("van/starlink/threshold")
    client.subscribe("van/status/network/speedtest")

    # Load persisted threshold
    speed_threshold = load_threshold()

    # ── Ensure GL.iNet is on Starlink (WiFi Blaster) ──────────────────────
    # This recovers from any previous mis-state (e.g. stuck on T-Mobile).
    t_ensure = threading.Thread(target=_startup_ensure_starlink, args=(client,), daemon=True)
    t_ensure.start()

    # Start monitor loop
    t = threading.Thread(target=monitor_loop, args=(client,), daemon=True)
    t.start()

def _startup_ensure_starlink(client):
    """Run startup checks in background so MQTT loop isn't blocked."""
    global plug_address, starlink_power, auto_mode, speed_threshold

    # Discover plug
    plug_address = find_plug_ip()
    if not plug_address:
        print("Warning: Tuya plug not found on network")

    # Read plug state
    pw = plug_get_state()
    starlink_power = pw if pw != "unknown" else None
    print(f"Starlink plug: {starlink_power}")
    client.publish("van/status/starlink/power",
                   starlink_power or "unknown", retain=True)

    # Ensure GL.iNet is on WiFi Blaster
    ensure_on_starlink()

    # Upstream is always starlink (GL.iNet on WiFi Blaster)
    write_upstream_file("starlink")
    client.publish("van/status/network/upstream", "starlink", retain=True)

    # Publish initial status
    client.publish("van/status/starlink/auto",
                   "on" if auto_mode else "off", retain=True)
    client.publish("van/status/starlink/threshold",
                   str(speed_threshold), retain=True)
    if starlink_power != "on":
        client.publish("van/status/starlink/quality", "unknown", retain=True)

    print(f"Startup complete: plug={starlink_power}, threshold={speed_threshold} Mbps, auto={auto_mode}")


def on_message(client, userdata, msg):
    global starlink_power, auto_mode, speed_threshold
    topic   = msg.topic
    payload = msg.payload.decode().strip()
    lower   = payload.lower()

    # ── Manual plug control ──────────────────────────────────────────────
    if topic == "van/starlink/power":
        print(f"Manual: Starlink plug → {lower}")
        on = (lower == "on")
        if on:
            t = threading.Thread(target=power_on_starlink, daemon=True)
            t.start()
        else:
            t = threading.Thread(target=power_off_starlink, daemon=True)
            t.start()
        return

    # ── Auto-switch toggle ───────────────────────────────────────────────
    if topic == "van/starlink/auto":
        auto_mode = (lower == "on")
        client.publish("van/status/starlink/auto",
                       "on" if auto_mode else "off", retain=True)
        print(f"Auto-mode → {auto_mode}")
        # If auto just turned on and Starlink is off, check T-Mobile immediately
        if auto_mode and starlink_power == "off":
            t = threading.Thread(target=_auto_check_now, daemon=True)
            t.start()
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

    # ── Speed test result — Starlink auto-switch trigger ─────────────────
    # When a Starlink speed test arrives and auto mode is on,
    # check if Starlink is too slow to warrant keeping it on.
    if topic == "van/status/network/speedtest":
        if not auto_mode:
            return
        try:
            data     = json.loads(payload)
            dl       = data.get("download")
            upstream = data.get("upstream", "unknown")
            err      = data.get("error")
            if err or dl is None:
                return
            # Only react to Starlink speed tests (not T-Mobile wlan0 checks)
            if upstream != "starlink":
                return
            print(f"Starlink speed test: ↓{dl} Mbps (threshold: {speed_threshold} Mbps)")
            # If Starlink is slow, just log it — we can't fall back to T-Mobile
            # (T-Mobile doesn't provide internet via GL.iNet repeater)
            if dl < speed_threshold:
                print(f"Starlink slow ({dl} < {speed_threshold} Mbps) — no fallback available")
                client.publish("van/status/starlink/quality", "poor", retain=True)
            else:
                client.publish("van/status/starlink/quality", "good", retain=True)
        except Exception as e:
            print(f"speedtest handler error: {e}")
        return

def _auto_check_now():
    """Immediately run T-Mobile speed check when auto mode is enabled."""
    global last_tmobile_check
    last_tmobile_check = 0  # reset timer to force immediate check
    dl = run_tmobile_speedtest()
    if dl is not None and auto_mode and starlink_power == "off":
        if dl < speed_threshold:
            print(f"Auto check: T-Mobile slow ({dl} Mbps) → powering on Starlink")
            power_on_starlink()


client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION1)
client.on_connect = on_connect
client.on_message = on_message
client.connect(MQTT_HOST, MQTT_PORT, 60)
client.loop_forever()
