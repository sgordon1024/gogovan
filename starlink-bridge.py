#!/usr/bin/env python3
"""
starlink-bridge.py — Starlink smart plug + GL.iNet repeater auto-switch bridge.

Default state: GL.iNet on T-Mobile Home, Starlink plug OFF.

Auto-switch logic:
  On startup: if GL.iNet is on T-Mobile, immediately run a T-Mobile speed test.
    - If < threshold → switch to Starlink automatically.
    - If >= threshold → stay on T-Mobile.
  While on Starlink, test T-Mobile via wlan0 every 30 min (no Starlink data used).
    - If T-Mobile >= threshold → switch GL.iNet back to T-Mobile, power off Starlink.
  Speed test results arriving via MQTT also trigger switch-to-Starlink when on T-Mobile.

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

class _FakeMsg:
    """Minimal MQTT message stub for replaying stored payloads."""
    def __init__(self, topic, payload):
        self.topic   = topic
        self.payload = payload.encode() if isinstance(payload, str) else payload

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
AUTO_MODE_FILE          = os.path.expanduser("~/.starlink_auto_mode")
UPSTREAM_FILE           = "/tmp/gogovan_upstream"
DEFAULT_SPEED_THRESH    = 5      # Mbps: switch to Starlink if T-Mobile below this
STARLINK_BOOT_SECS      = 60     # wait after powering plug before connecting repeater
TMOBILE_CHECK_INTERVAL  = 1800   # 30 min: how often to test T-Mobile while on Starlink
QUALITY_CHECK_INTERVAL  = 300    # 5 min: ping quality check when on Starlink
REPEATER_CHECK_INTERVAL = 120    # 2 min: watchdog — reconnect repeater if it drops

# States
STATE_TMOBILE            = "tmobile"
STATE_SWITCHING_STARLINK = "switching_to_starlink"
STATE_STARLINK           = "starlink"
STATE_SWITCHING_TMOBILE  = "switching_to_tmobile"

# ── State ──────────────────────────────────────────────────────────────────
state              = STATE_TMOBILE
starlink_power     = None
auto_mode          = True    # on by default (overwritten from disk in on_connect)
speed_threshold    = DEFAULT_SPEED_THRESH
plug_address       = None
switch_lock          = threading.Lock()
last_tmobile_check   = 0.0
last_quality_check   = 0.0
last_repeater_check  = 0.0
mqtt_client_ref    = None
startup_complete   = False   # blocks speedtest handler until startup sets real state
pending_speedtest  = None    # stores speedtest message received during startup
manual_pause_auto  = False   # set when user manually turns off Starlink; blocks auto-switch

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

def load_auto_mode() -> bool:
    """Load persisted auto_mode. Avoids startup race with retained MQTT message."""
    try:
        return open(AUTO_MODE_FILE).read().strip().lower() != "off"
    except Exception:
        return True   # default: auto on

def save_auto_mode(val: bool):
    try:
        with open(AUTO_MODE_FILE, "w") as f:
            f.write("on" if val else "off")
    except Exception as e:
        print(f"save_auto_mode error: {e}")

def write_upstream_file(upstream: str):
    try:
        with open(UPSTREAM_FILE, "w") as f:
            f.write(upstream)
    except Exception as e:
        print(f"write_upstream_file error: {e}")

# ── GL.iNet router control ─────────────────────────────────────────────────

def router_ssh(cmd: str, timeout: int = 20) -> tuple:
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
    out, rc = router_ssh("ubus call repeater status", timeout=10)
    if out:
        try:
            return json.loads(out)
        except Exception:
            pass
    return None

def router_repeater_connect(ssid: str, key: str, wait_secs: int = 35) -> bool:
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

def get_router_upstream() -> str:
    status = router_repeater_status()
    if not status:
        return "unknown"
    ssid      = status.get("ssid", "")
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

def ensure_plug_ip() -> bool:
    """Return True if plug address is known; re-scan once if not."""
    global plug_address
    if plug_address:
        return True
    plug_address = find_plug_ip()
    return plug_address is not None

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
    if not plug_address:
        return "unknown"
    for attempt in range(3):
        try:
            d = make_plug(plug_address)
            status = d.status()
            if "dps" in status:
                return "on" if status["dps"].get("1", False) else "off"
            if attempt < 2:
                time.sleep(1)
        except Exception as e:
            print(f"plug_get_state error (attempt {attempt+1}): {e}")
            if attempt < 2:
                time.sleep(1)
    return "unknown"

def plug_set(on: bool) -> bool:
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
    Background speed test via wlan0 (T-Mobile direct, no Starlink data used).
    Returns download Mbps or None.

    Intentionally does NOT publish to van/status/network/speedtest — that topic
    is for user-initiated tests shown in the dashboard. Publishing here caused two
    problems: (1) the monitor_loop and the on_message handler both saw the result
    and raced to start a second switch thread; (2) background T-Mobile checks
    overwrote the dashboard's "Last Test" display while the user was on Starlink.
    """
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
        if not r.stdout.strip():
            print("T-Mobile speedtest: empty output (T-Mobile may be down)")
            return None
        data = json.loads(r.stdout)
        dl   = round(data["download"] / 1e6, 1)
        ul   = round(data["upload"]   / 1e6, 1)
        ping = round(data["ping"])
        print(f"T-Mobile via wlan0: ↓{dl} ↑{ul} ping={ping}ms")
        return dl
    except Exception as e:
        print(f"T-Mobile speedtest error: {e}")
        return None

def check_connectivity() -> str:
    try:
        r = subprocess.run(
            ["ping", "-c", "3", "-W", "3", "-q", "8.8.8.8"],
            capture_output=True, text=True, timeout=30
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
    """Background thread: power on Starlink plug and switch GL.iNet to WiFi Blaster."""
    global state
    if not switch_lock.acquire(blocking=False):
        print("switch_to_starlink: switch already in progress")
        return
    try:
        print("Switching to Starlink...")
        state = STATE_SWITCHING_STARLINK

        # 1. Ensure Tuya plug is reachable (re-scan if not found at startup)
        if not ensure_plug_ip():
            print("✗ Cannot switch to Starlink: Tuya plug not found")
            state = STATE_TMOBILE
            if mqtt_client_ref:
                mqtt_client_ref.publish("van/status/starlink/power", "off", retain=True)
            return

        # 2. Power on Starlink plug; abort if plug_set fails
        if not plug_set(True):
            print("✗ Cannot switch to Starlink: plug_set failed")
            state = STATE_TMOBILE
            return
        if mqtt_client_ref:
            mqtt_client_ref.publish("van/status/starlink/power", "on", retain=True)

        # 3. Wait for Starlink dish to boot
        print(f"Waiting {STARLINK_BOOT_SECS}s for Starlink to boot...")
        time.sleep(STARLINK_BOOT_SECS)

        # 4. Switch GL.iNet to WiFi Blaster
        print("Switching GL.iNet to WiFi Blaster...")
        if router_repeater_connect(STARLINK_SSID, WIFI_PASSWORD):
            state = STATE_STARLINK
            write_upstream_file("starlink")
            if mqtt_client_ref:
                mqtt_client_ref.publish(
                    "van/status/network/upstream", "starlink", retain=True)
            print("✓ On Starlink")
        else:
            print("✗ Could not connect to WiFi Blaster — reverting to T-Mobile")
            state = STATE_TMOBILE
            plug_set(False)
            if mqtt_client_ref:
                mqtt_client_ref.publish("van/status/starlink/power", "off", retain=True)
    finally:
        switch_lock.release()

def switch_to_tmobile(force_off: bool = False):
    """
    Background thread: switch GL.iNet to T-Mobile and power off Starlink plug.

    force_off=True  — called from a manual "power off" tap. If T-Mobile SSID is
                      unreachable, still cut the plug (user explicitly wants it off).
                      Skips the post-switch connectivity check so the user's intent
                      is never silently reversed.
    force_off=False — called from auto-switch logic. If T-Mobile has no internet
                      after connecting, fall back to Starlink automatically.
    """
    global state
    if not switch_lock.acquire(blocking=False):
        print("switch_to_tmobile: switch already in progress")
        return
    fallback_to_starlink = False
    try:
        label = "Manual power-off" if force_off else "Switching to T-Mobile"
        print(f"{label}...")
        state = STATE_SWITCHING_TMOBILE

        # 1. Switch GL.iNet to T-Mobile
        print("Switching GL.iNet to T-Mobile...")
        if router_repeater_connect(TMOBILE_SSID, WIFI_PASSWORD):
            state = STATE_TMOBILE
            write_upstream_file("tmobile")
            if mqtt_client_ref:
                mqtt_client_ref.publish(
                    "van/status/network/upstream", "tmobile", retain=True)
            print("✓ On T-Mobile")

            # 2. Power off Starlink
            plug_set(False)
            if mqtt_client_ref:
                mqtt_client_ref.publish("van/status/starlink/power", "off", retain=True)
                mqtt_client_ref.publish(
                    "van/status/starlink/quality", "unknown", retain=True)

            # 3. Verify internet — only for auto-switch; skip for manual power-off
            #    (manual_pause_auto ensures no auto-switch fires after this anyway)
            if not force_off:
                print("Verifying T-Mobile internet...")
                quality = check_connectivity()
                if quality == "poor":
                    print("T-Mobile has no internet — will switch back to Starlink")
                    fallback_to_starlink = True
                else:
                    print(f"T-Mobile verified: {quality}")
        else:
            # T-Mobile SSID unreachable
            if force_off:
                # User explicitly wants dish off — honor it even without T-Mobile
                print("T-Mobile SSID not reachable; force_off=True → cutting plug anyway")
                plug_set(False)
                state = STATE_STARLINK   # router still on Starlink config; dish is off
                if mqtt_client_ref:
                    mqtt_client_ref.publish("van/status/starlink/power", "off", retain=True)
            else:
                print("✗ Could not connect to T-Mobile — staying on Starlink")
                state = STATE_STARLINK
                # Correct optimistic UI: dashboard showed "off" but we're still on
                if mqtt_client_ref:
                    mqtt_client_ref.publish("van/status/starlink/power", "on", retain=True)
    finally:
        # Always release lock before spawning the fallback thread (avoids lock race)
        switch_lock.release()
        if fallback_to_starlink:
            threading.Thread(target=switch_to_starlink, daemon=True).start()

# ── Monitor loop ───────────────────────────────────────────────────────────

def router_repeater_reconnect_if_needed():
    """
    Watchdog: if the GL.iNet repeater has dropped, reconnect to whichever
    network matches the current state (T-Mobile or WiFi Blaster).
    Only runs when we're not mid-switch.
    """
    if switch_lock.locked():
        return
    if state not in (STATE_TMOBILE, STATE_STARLINK):
        return
    status = router_repeater_status()
    if status is None:
        return  # SSH failed — don't act on missing data
    if status.get("state_s") == "connected":
        return  # all good
    expected_ssid = STARLINK_SSID if state == STATE_STARLINK else TMOBILE_SSID
    print(f"Watchdog: repeater dropped (state={status.get('state_s')!r}), "
          f"reconnecting to {expected_ssid!r}...")
    if router_repeater_connect(expected_ssid, WIFI_PASSWORD):
        print(f"✓ Watchdog: reconnected to {expected_ssid!r}")
    else:
        print(f"✗ Watchdog: reconnect to {expected_ssid!r} failed")


def monitor_loop(client):
    global last_tmobile_check, last_quality_check, last_repeater_check

    while True:
        time.sleep(30)
        try:
            now = time.time()

            # Repeater watchdog — runs every 2 min regardless of upstream state
            if now - last_repeater_check >= REPEATER_CHECK_INTERVAL:
                last_repeater_check = now
                router_repeater_reconnect_if_needed()

            if state == STATE_STARLINK:
                # T-Mobile speed check every 30 min to decide when to switch back.
                # Skipped when auto is off OR when user manually turned off Starlink
                # (manual_pause_auto) — prevents re-enabling dish the user just silenced.
                if now - last_tmobile_check >= TMOBILE_CHECK_INTERVAL:
                    last_tmobile_check = now
                    if not auto_mode or manual_pause_auto:
                        print(f"Skipping T-Mobile check (auto={auto_mode}, "
                              f"manual_pause={manual_pause_auto})")
                    else:
                        dl = run_tmobile_speedtest()
                        if dl is not None and dl >= speed_threshold:
                            print(f"T-Mobile recovered: {dl} Mbps ≥ {speed_threshold} "
                                  f"Mbps → switching back")
                            threading.Thread(target=switch_to_tmobile, daemon=True).start()

                # Quality ping every 5 min — skip when dish is intentionally off
                if not manual_pause_auto and now - last_quality_check >= QUALITY_CHECK_INTERVAL:
                    last_quality_check = now
                    quality = check_connectivity()
                    print(f"Starlink quality → {quality}")
                    client.publish("van/status/starlink/quality", quality, retain=True)

        except Exception as e:
            print(f"monitor_loop error: {e}")

# ── MQTT callbacks ─────────────────────────────────────────────────────────

def on_connect(client, userdata, flags, rc):
    global auto_mode, speed_threshold, mqtt_client_ref
    print(f"MQTT connected (rc={rc})")
    mqtt_client_ref = client

    client.subscribe("van/starlink/power")
    client.subscribe("van/starlink/auto")
    client.subscribe("van/starlink/threshold")
    client.subscribe("van/status/network/speedtest")

    speed_threshold = load_threshold()
    auto_mode       = load_auto_mode()   # load persisted setting before _startup runs
    print(f"Loaded settings: threshold={speed_threshold} Mbps, auto={auto_mode}")

    # Run startup in background so MQTT loop isn't blocked
    t = threading.Thread(target=_startup, args=(client,), daemon=True)
    t.start()

    # Start monitor loop
    t2 = threading.Thread(target=monitor_loop, args=(client,), daemon=True)
    t2.start()

def _startup(client):
    """
    Startup logic:
    1. Find plug, read state.
    2. Check GL.iNet current upstream.
    3. If on T-Mobile: run immediate speed test.
       - Too slow → auto-switch to Starlink.
       - Fast enough → stay on T-Mobile.
    4. If already on Starlink (e.g. service restart mid-session): stay there.
    """
    global plug_address, starlink_power, state, auto_mode, speed_threshold

    # Find and read plug
    plug_address = find_plug_ip()
    if plug_address:
        pw = plug_get_state()
        starlink_power = pw if pw != "unknown" else None
        print(f"Starlink plug: {starlink_power}")
    else:
        print("Warning: Tuya plug not found")

    client.publish("van/status/starlink/power",
                   starlink_power or "unknown", retain=True)
    client.publish("van/status/starlink/auto",
                   "on" if auto_mode else "off", retain=True)
    client.publish("van/status/starlink/threshold",
                   str(speed_threshold), retain=True)

    # Check GL.iNet current upstream
    upstream = get_router_upstream()
    print(f"GL.iNet upstream: {upstream}")

    if upstream == "starlink":
        # Already on Starlink — stay here, treat as if auto-switch fired
        state = STATE_STARLINK
        write_upstream_file("starlink")
        client.publish("van/status/network/upstream", "starlink", retain=True)
        # Clear any stale "poor" quality from before the restart; monitor loop
        # will run a real check within 5 minutes and publish the accurate result.
        client.publish("van/status/starlink/quality", "unknown", retain=True)
        # GL.iNet is on WiFi Blaster → Starlink plug must be on
        if starlink_power != "on":
            starlink_power = "on"
            client.publish("van/status/starlink/power", "on", retain=True)
            print("Inferred Starlink plug ON (GL.iNet connected to WiFi Blaster)")
        print("Starting in Starlink state")

    else:
        # On T-Mobile (or unknown) — ensure repeater is connected first
        state = STATE_TMOBILE
        write_upstream_file("tmobile")
        client.publish("van/status/network/upstream", "tmobile", retain=True)

        if upstream == "unknown":
            # Repeater disconnected (router rebooted, or WiFi Blaster was selected but
            # Starlink dish is off). Reconnect to T-Mobile before running speed test.
            print("GL.iNet repeater not connected — reconnecting to T-Mobile...")
            if router_repeater_connect(TMOBILE_SSID, WIFI_PASSWORD):
                print("✓ Repeater reconnected to T-Mobile")
            else:
                print("✗ Repeater reconnect to T-Mobile failed — speed test may fail")

        print("Starting in T-Mobile state — running immediate speed test...")

        if auto_mode:
            dl = run_tmobile_speedtest()
            if dl is None:
                # Speedtest failed — treat as slow, switch to Starlink
                print("T-Mobile speedtest failed — assuming slow, switching to Starlink")
                t = threading.Thread(target=switch_to_starlink, daemon=True)
                t.start()
            elif dl < speed_threshold:
                print(f"T-Mobile too slow ({dl} Mbps < {speed_threshold} Mbps) — switching to Starlink")
                t = threading.Thread(target=switch_to_starlink, daemon=True)
                t.start()
            else:
                print(f"T-Mobile fast enough ({dl} Mbps ≥ {speed_threshold} Mbps) — staying on T-Mobile")
        else:
            print("Auto mode off — staying on T-Mobile")

    if starlink_power != "on":
        client.publish("van/status/starlink/quality", "unknown", retain=True)

    global startup_complete, pending_speedtest
    startup_complete = True
    print(f"Startup complete: upstream={upstream}, plug={starlink_power}, "
          f"threshold={speed_threshold} Mbps, auto={auto_mode}")

    # Replay any speedtest that arrived during startup
    if pending_speedtest:
        print("Replaying speedtest received during startup...")
        on_message(client, None, _FakeMsg("van/status/network/speedtest", pending_speedtest))
        pending_speedtest = None


def on_message(client, userdata, msg):
    global starlink_power, auto_mode, speed_threshold, manual_pause_auto
    topic   = msg.topic
    payload = msg.payload.decode().strip()
    lower   = payload.lower()

    # ── Manual plug / network control ────────────────────────────────────
    if topic == "van/starlink/power":
        print(f"Manual: Starlink → {lower}")
        on = (lower == "on")
        if on:
            # Turning on: clear manual pause and do the full switch
            manual_pause_auto = False
            threading.Thread(target=switch_to_starlink, daemon=True).start()
        else:
            # Turning off: set manual pause so auto-switch won't re-enable the dish
            manual_pause_auto = True
            if auto_mode:
                # Auto mode: try to switch to T-Mobile; force plug off if T-Mobile
                # SSID is unreachable (user's intent must be honored regardless)
                threading.Thread(
                    target=lambda: switch_to_tmobile(force_off=True),
                    daemon=True).start()
            else:
                # Manual mode (auto OFF): direct plug control only — no router
                # switching, no loops, just cut the dish power immediately
                plug_set(False)
                if mqtt_client_ref:
                    mqtt_client_ref.publish(
                        "van/status/starlink/power", "off", retain=True)
        return

    # ── Auto-switch toggle ───────────────────────────────────────────────
    if topic == "van/starlink/auto":
        auto_mode = (lower == "on")
        if auto_mode:
            manual_pause_auto = False   # resuming auto clears any manual pause
        save_auto_mode(auto_mode)
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

    # ── Speed test result — auto-switch trigger ──────────────────────────
    # Note: run_tmobile_speedtest() no longer publishes here (would create duplicate
    # switch threads). This handler only sees results from can-bridge / run-speedtest.py.
    if topic == "van/status/network/speedtest":
        if not startup_complete:
            global pending_speedtest
            pending_speedtest = payload   # replay after startup completes
            return
        if not auto_mode or manual_pause_auto:
            return
        try:
            data     = json.loads(payload)
            dl       = data.get("download")
            upstream = data.get("upstream", "unknown")
            err      = data.get("error")
            if err or dl is None:
                return
            # Only act on T-Mobile results
            if upstream != "tmobile":
                return
            # Reject stale results — only act on tests from the last 10 minutes
            ts_str = data.get("timestamp", "")
            if ts_str:
                try:
                    from datetime import datetime, timezone
                    ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                    age_min = (datetime.now(timezone.utc) - ts).total_seconds() / 60
                    if age_min > 10:
                        print(f"Ignoring stale T-Mobile result "
                              f"({age_min:.0f} min old, {dl} Mbps)")
                        return
                except Exception:
                    pass  # unparseable timestamp — allow through

            print(f"Speed test: {dl} Mbps via T-Mobile (threshold: {speed_threshold} Mbps)")
            if dl < speed_threshold and state == STATE_TMOBILE:
                print(f"T-Mobile slow ({dl} Mbps) → switching to Starlink")
                threading.Thread(target=switch_to_starlink, daemon=True).start()
            elif dl >= speed_threshold and state == STATE_STARLINK:
                print(f"T-Mobile recovered ({dl} Mbps) → switching back to T-Mobile")
                threading.Thread(target=switch_to_tmobile, daemon=True).start()
        except Exception as e:
            print(f"speedtest handler error: {e}")
        return


client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION1)
client.on_connect = on_connect
client.on_message = on_message
client.connect(MQTT_HOST, MQTT_PORT, 60)
client.loop_forever()
