#!/usr/bin/env python3
"""
starlink-bridge.py — Unified Starlink + network-routing bridge.

Manages the Tuya smart plug (dish power) AND nmcli routing as a single
coupled state machine.  Auto-switches T-Mobile → Starlink when T-Mobile
signal is weak, and back when it recovers.

States: tmobile | warming_up | starlink | reverting | unknown

MQTT subscribe:
  van/starlink/power     — "on"/"off"       manual plug control
  van/starlink/auto      — "on"/"off"       enable/disable auto-switch
  van/starlink/threshold — "0"-"100"        switch-on signal threshold
  van/network/upstream   — "tmobile"/"starlink"  manual routing override

MQTT publish (all retained):
  van/status/starlink/power          — "on"/"off"/"unknown"
  van/status/starlink/tmobile-signal — "0"-"100" or "-1"
  van/status/starlink/auto           — "on"/"off"
  van/status/starlink/threshold      — "0"-"100"
  van/status/starlink/quality        — "good"/"poor"/"unknown"
  van/status/starlink/state          — state machine state
  van/status/starlink/warmup_eta     — seconds remaining (warming_up only)
  van/status/network/upstream        — "tmobile"/"starlink"/"unknown"
  van/status/starlink/manual-override   — "on"/"off"
"""

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
# IP is discovered at startup via tinytuya broadcast scan (UDP 6666).
# Hardcoded fallback only used if scan fails.
PLUG_ADDRESS_FALLBACK = "192.168.8.34"
PLUG_ADDRESS_FILE     = os.path.expanduser("~/.starlink_plug_address")

THRESH_FILE            = os.path.expanduser("~/.starlink_threshold")
DEFAULT_ON_THRESH      = 35
HYSTERESIS             = 20     # off threshold = on_thresh + HYSTERESIS
SIGNAL_POLL_INTERVAL   = 30     # seconds between T-Mobile polls
CONFIRM_COUNT          = 2      # consecutive bad reads before auto-switch triggers
WARMUP_MAX_SECS        = 180    # max seconds to wait for Starlink to boot
WARMUP_POLL_SECS       = 10     # seconds between SSID scan during warmup
ROUTING_TIMEOUT        = 25     # seconds for nmcli to succeed
QUALITY_CHECK_INTERVAL = 120
QUALITY_PING_TIMEOUT   = 15
MANUAL_OVERRIDE_SECS   = 30 * 60  # manual switch holds for 30 min before auto resumes
FAILOVER_PING_HOST     = "8.8.8.8" # host to ping before tripping T-Mobile failover

# ── Module-level state ─────────────────────────────────────────────────────
state              = "unknown"  # tmobile | warming_up | starlink | reverting | unknown
starlink_plug      = None       # "on" / "off" / None
auto_mode          = True    # on by default — auto-switch is always armed
signal_on_thresh   = DEFAULT_ON_THRESH
tmobile_signal     = -1
warmup_start       = 0.0
low_signal_count   = 0
high_signal_count  = 0
last_quality_check = 0.0
tmobile_ssid       = ""              # SSID of the T-Mobile connection profile
manual_override    = False           # True while a manual switch is holding
manual_override_expires = 0.0       # epoch when manual override expires

_state_lock = threading.Lock()
mqtt_client = None

# ── Persistence ────────────────────────────────────────────────────────────

def load_threshold() -> int:
    try:
        return max(0, min(100, int(open(THRESH_FILE).read().strip())))
    except Exception:
        return DEFAULT_ON_THRESH

def save_threshold(val: int):
    try:
        open(THRESH_FILE, "w").write(str(val))
    except Exception as e:
        print(f"save_threshold error: {e}")

# ── MQTT publish helper ────────────────────────────────────────────────────

def pub(topic, payload, retain=True):
    if mqtt_client:
        mqtt_client.publish(topic, str(payload), retain=retain)

# ── nmcli helpers ──────────────────────────────────────────────────────────

def get_current_upstream() -> str:
    """Return 'tmobile', 'starlink', or 'unknown' based on active wlan0 connection."""
    try:
        r = subprocess.run(
            ["nmcli", "-g", "DEVICE,CONNECTION", "device", "status"],
            capture_output=True, text=True, timeout=5
        )
        for line in r.stdout.splitlines():
            parts = line.split(":")
            if len(parts) >= 2 and parts[0].strip() == "wlan0":
                conn = parts[1].strip()
                if conn == "preconfigured":
                    return "tmobile"
                elif conn == "PhiladelphiaCollins":
                    return "starlink"
                return "unknown"
    except Exception as e:
        print(f"get_current_upstream error: {e}")
    return "unknown"

def get_tmobile_ssid() -> str:
    """Return the SSID stored in the 'preconfigured' NM connection profile."""
    try:
        r = subprocess.run(
            ["nmcli", "-g", "802-11-wireless.ssid", "connection", "show", "preconfigured"],
            capture_output=True, text=True, timeout=5
        )
        return r.stdout.strip()
    except Exception as e:
        print(f"get_tmobile_ssid error: {e}")
    return ""

def nmcli_connect(conn_name: str) -> bool:
    """Bring up a NetworkManager connection. Returns True on success."""
    try:
        r = subprocess.run(
            ["sudo", "nmcli", "connection", "up", conn_name],
            capture_output=True, text=True, timeout=ROUTING_TIMEOUT + 5
        )
        ok = r.returncode == 0
        print(f"nmcli up {conn_name}: {'ok' if ok else 'FAILED'} | {r.stderr.strip()}")
        return ok
    except Exception as e:
        print(f"nmcli_connect({conn_name}) error: {e}")
        return False

def ssid_visible(ssid: str) -> bool:
    """Return True if the given SSID is visible in a Wi-Fi scan."""
    if not ssid:
        return False
    try:
        r = subprocess.run(
            ["nmcli", "-t", "-f", "SSID", "dev", "wifi", "list", "--rescan", "yes"],
            capture_output=True, text=True, timeout=20
        )
        return any(line.strip() == ssid for line in r.stdout.splitlines())
    except Exception as e:
        print(f"ssid_visible error: {e}")
        return False

# ── T-Mobile signal ────────────────────────────────────────────────────────

def get_tmobile_signal() -> int:
    """
    Return 0-100 signal for T-Mobile or -1 if not visible.
    When wlan0 is already on T-Mobile, reads live signal directly.
    When on Starlink, scans for the T-Mobile SSID (or any T-Mobile BSSID).
    """
    upstream = get_current_upstream()
    if upstream == "tmobile":
        try:
            r = subprocess.run(
                ["nmcli", "-t", "-f", "GENERAL.SIGNAL", "dev", "show", "wlan0"],
                capture_output=True, text=True, timeout=5
            )
            for line in r.stdout.splitlines():
                if "SIGNAL" in line:
                    try:
                        return int(line.split(":")[-1])
                    except ValueError:
                        pass
        except Exception as e:
            print(f"get_tmobile_signal(direct) error: {e}")

    # Scan for T-Mobile SSID
    try:
        r = subprocess.run(
            ["nmcli", "-t", "-f", "SSID,SIGNAL", "dev", "wifi", "list", "--rescan", "yes"],
            capture_output=True, text=True, timeout=20
        )
        best = -1
        for line in r.stdout.splitlines():
            idx = line.rfind(":")
            if idx < 0:
                continue
            ssid_part = line[:idx]
            sig_part  = line[idx + 1:]
            is_match = (
                (tmobile_ssid and ssid_part == tmobile_ssid) or
                "t-mobile" in ssid_part.lower() or
                "tmobile"  in ssid_part.lower()
            )
            if is_match:
                try:
                    best = max(best, int(sig_part))
                except ValueError:
                    pass
        return best
    except Exception as e:
        print(f"get_tmobile_signal(scan) error: {e}")
    return -1

# ── Connectivity checks ────────────────────────────────────────────────────

def check_tmobile_internet() -> bool:
    """
    Ping through the current T-Mobile connection to confirm internet is actually
    working — not just that wlan0 has signal.  Runs before triggering failover
    so a momentary signal dip doesn't cause an unnecessary switch to Starlink.
    Returns True if internet is reachable.
    """
    try:
        r = subprocess.run(
            ["ping", "-c", "2", "-W", "2", "-q", FAILOVER_PING_HOST],
            capture_output=True, text=True, timeout=8
        )
        reachable = r.returncode == 0
        print(f"T-Mobile internet check: {'ok' if reachable else 'FAILED'}")
        return reachable
    except Exception as e:
        print(f"check_tmobile_internet error: {e}")
        return False

def check_connectivity() -> str:
    """Ping quality check while on Starlink. Returns 'good', 'poor', or 'unknown'."""
    try:
        r = subprocess.run(
            ["ping", "-c", "3", "-W", "3", "-q", FAILOVER_PING_HOST],
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

# ── Plug control ───────────────────────────────────────────────────────────

_plug_address = None   # discovered at startup, cached here

def discover_plug_address() -> str:
    """
    Broadcast scan (tinytuya UDP 6666) to find the plug's current IP.
    Falls back to the last-known address saved in PLUG_ADDRESS_FILE,
    then to PLUG_ADDRESS_FALLBACK.  Caches the result for the session.
    """
    global _plug_address
    if _plug_address:
        return _plug_address

    # 1. Try broadcast scan (~3s)
    try:
        print("Scanning local network for Starlink plug...")
        found = tinytuya.deviceScan(verbose=False, maxretry=2)
        for ip, info in found.items():
            if info.get("gwId") == PLUG_DEV_ID or info.get("id") == PLUG_DEV_ID:
                print(f"Plug found via scan at {ip}")
                _plug_address = ip
                try:
                    open(PLUG_ADDRESS_FILE, "w").write(ip)
                except Exception:
                    pass
                return ip
        print("Plug not found in broadcast scan")
    except Exception as e:
        print(f"Plug scan error: {e}")

    # 2. Last-known address from file
    try:
        addr = open(PLUG_ADDRESS_FILE).read().strip()
        if addr:
            print(f"Using last-known plug address: {addr}")
            _plug_address = addr
            return addr
    except Exception:
        pass

    # 3. Hardcoded fallback
    print(f"Using fallback plug address: {PLUG_ADDRESS_FALLBACK}")
    _plug_address = PLUG_ADDRESS_FALLBACK
    return PLUG_ADDRESS_FALLBACK

def make_device():
    addr = discover_plug_address()
    d = tinytuya.OutletDevice(
        dev_id=PLUG_DEV_ID,
        address=addr,
        local_key=PLUG_LOCAL_KEY,
        version=PLUG_VERSION
    )
    d.set_socketTimeout(5)
    d.set_socketRetryLimit(2)
    return d

def plug_set(on: bool) -> bool:
    global starlink_plug, _plug_address
    try:
        d = make_device()
        result = d.set_value(1, on)
        if "Error" not in str(result):
            starlink_plug = "on" if on else "off"
            pub("van/status/starlink/power", starlink_plug)
            print(f"Plug → {'ON' if on else 'OFF'}")
            return True
        print(f"Plug set error: {result}")
        # Invalidate cached address so next attempt re-scans
        _plug_address = None
        return False
    except Exception as e:
        print(f"plug_set error: {e}")
        _plug_address = None
        return False

def plug_get_state() -> str:
    try:
        d = make_device()
        status = d.status()
        if "dps" in status:
            return "on" if status["dps"].get("1", False) else "off"
        return "unknown"
    except Exception as e:
        print(f"plug_get_state error: {e}")
        return "unknown"

# ── State machine ──────────────────────────────────────────────────────────

def set_state(new_state: str):
    global state
    with _state_lock:
        state = new_state
    pub("van/status/starlink/state", new_state)
    print(f"State → {new_state}")

def do_switch_to_starlink():
    """
    Full coupled transition: plug on → wait for boot → switch nmcli routing.
    Runs the warmup wait in a daemon thread so the signal loop keeps running.
    """
    global warmup_start
    print("Switching to Starlink: powering plug…")

    if starlink_plug != "on":
        if not plug_set(True):
            print("plug_set failed — aborting switch to Starlink")
            return

    warmup_start = time.time()
    set_state("warming_up")
    pub("van/status/starlink/warmup_eta", WARMUP_MAX_SECS)

    t = threading.Thread(target=_warmup_then_route, daemon=True)
    t.start()

def _warmup_then_route():
    """
    Poll until 'WiFi Blaster' SSID appears (Starlink booted), then connect.
    Publishes ETA updates every WARMUP_POLL_SECS seconds.
    """
    wifi_blaster_ssid = "WiFi Blaster"
    deadline = warmup_start + WARMUP_MAX_SECS

    while True:
        with _state_lock:
            current = state
        if current != "warming_up":
            print("Warmup thread: state changed externally, exiting")
            return

        elapsed   = time.time() - warmup_start
        remaining = max(0, WARMUP_MAX_SECS - elapsed)
        pub("van/status/starlink/warmup_eta", int(remaining))

        if time.time() >= deadline:
            # Timed out — try to connect anyway
            print("Warmup timeout: attempting connection regardless")
            break

        if ssid_visible(wifi_blaster_ssid):
            print(f"WiFi Blaster visible after {elapsed:.0f}s — connecting")
            break

        print(f"Warmup: waiting for WiFi Blaster… {remaining:.0f}s remaining")
        time.sleep(WARMUP_POLL_SECS)

    with _state_lock:
        if state != "warming_up":
            print("Warmup thread: aborted before connect")
            return

    pub("van/status/starlink/warmup_eta", 0)
    ok = nmcli_connect("PhiladelphiaCollins")
    if ok:
        set_state("starlink")
        pub("van/status/network/upstream", "starlink")
    else:
        # Connection failed — wait 30s and retry once
        print("nmcli PhiladelphiaCollins failed, retrying in 30s…")
        time.sleep(30)
        with _state_lock:
            if state != "warming_up":
                return
        ok = nmcli_connect("PhiladelphiaCollins")
        if ok:
            set_state("starlink")
            pub("van/status/network/upstream", "starlink")
        else:
            print("nmcli PhiladelphiaCollins retry failed — staying in warmup")
            # Reset to let auto logic try again
            set_state("unknown")

def do_switch_to_tmobile():
    """Full coupled transition: switch nmcli routing to T-Mobile → plug off."""
    global low_signal_count, high_signal_count
    print("Switching back to T-Mobile…")
    set_state("reverting")

    ok = nmcli_connect("preconfigured")
    if ok:
        set_state("tmobile")
        pub("van/status/network/upstream", "tmobile")
        # Give routing a moment to stabilise before turning off the dish
        time.sleep(5)
        plug_set(False)
        pub("van/status/starlink/quality", "unknown")
    else:
        print("nmcli preconfigured failed — staying on Starlink")
        set_state("starlink")

    low_signal_count  = 0
    high_signal_count = 0

# ── Signal monitor loop ────────────────────────────────────────────────────

def signal_monitor():
    global tmobile_signal, low_signal_count, high_signal_count, last_quality_check
    global manual_override, manual_override_expires

    while True:
        time.sleep(SIGNAL_POLL_INTERVAL)
        try:
            sig = get_tmobile_signal()
            if sig != tmobile_signal:
                tmobile_signal = sig
                pub("van/status/starlink/tmobile-signal", sig)
                print(f"T-Mobile signal → {sig}")

            with _state_lock:
                cur = state

            # ── Manual override check ─────────────────────────────────────
            if manual_override:
                if time.time() < manual_override_expires:
                    remaining = int(manual_override_expires - time.time())
                    print(f"Manual override active ({remaining}s remaining) — skipping auto-switch")
                    continue
                else:
                    # Override expired — resume auto
                    manual_override = False
                    pub("van/status/starlink/manual-override", "off")
                    print("Manual override expired — auto-switch resumed")

            if not auto_mode:
                low_signal_count = high_signal_count = 0
                continue

            off_thresh = min(signal_on_thresh + HYSTERESIS, 95)

            if cur in ("tmobile", "unknown"):
                # Signal absent (-1) or below threshold — accumulate bad reads
                if sig == -1 or sig < signal_on_thresh:
                    low_signal_count += 1
                    print(f"Weak/no T-Mobile (sig={sig}, count={low_signal_count}/{CONFIRM_COUNT})")
                    if low_signal_count >= CONFIRM_COUNT:
                        # Confirm with a real ping before failing over.
                        # Prevents unnecessary Starlink switches when signal dips
                        # momentarily but internet is still reachable.
                        if not check_tmobile_internet():
                            low_signal_count = 0
                            threading.Thread(target=do_switch_to_starlink, daemon=True).start()
                        else:
                            print("Signal weak but ping ok — holding on T-Mobile one more cycle")
                            low_signal_count = CONFIRM_COUNT - 1  # one more bad read will trip it
                else:
                    low_signal_count = max(0, low_signal_count - 1)

            elif cur == "starlink":
                if sig != -1 and sig > off_thresh:
                    high_signal_count += 1
                    print(f"T-Mobile recovered (sig={sig}, count={high_signal_count}/{CONFIRM_COUNT})")
                    if high_signal_count >= CONFIRM_COUNT:
                        high_signal_count = 0
                        threading.Thread(target=do_switch_to_tmobile, daemon=True).start()
                else:
                    high_signal_count = max(0, high_signal_count - 1)

                # Connectivity quality check while on Starlink
                now = time.time()
                if now - last_quality_check >= QUALITY_CHECK_INTERVAL:
                    last_quality_check = now
                    q = check_connectivity()
                    print(f"Starlink quality → {q}")
                    pub("van/status/starlink/quality", q)

        except Exception as e:
            print(f"signal_monitor error: {e}")

# ── MQTT callbacks ─────────────────────────────────────────────────────────

def on_connect(client, userdata, flags, rc):
    global mqtt_client, starlink_plug, signal_on_thresh, tmobile_ssid
    global warmup_start, tmobile_signal
    mqtt_client = client
    print(f"MQTT connected (rc={rc})")

    signal_on_thresh = load_threshold()
    tmobile_ssid     = get_tmobile_ssid()
    print(f"T-Mobile SSID: '{tmobile_ssid}'")

    client.subscribe("van/starlink/power")
    client.subscribe("van/starlink/auto")
    client.subscribe("van/starlink/threshold")
    client.subscribe("van/network/upstream")   # manual routing override from dashboard

    # --- Determine initial state ---
    plug_state = plug_get_state()
    starlink_plug = plug_state if plug_state != "unknown" else None
    pub("van/status/starlink/power", starlink_plug or "unknown")

    upstream = get_current_upstream()
    pub("van/status/network/upstream", upstream)

    if upstream == "starlink":
        set_state("starlink")
    elif upstream == "tmobile":
        set_state("tmobile")
    elif starlink_plug == "on":
        # Plug is on but routing unknown — try to connect to Starlink
        print("Boot: plug on but routing unclear → attempting Starlink connect")
        warmup_start = time.time()
        set_state("warming_up")
        threading.Thread(target=_warmup_then_route, daemon=True).start()
    else:
        set_state("unknown")

    # Initial signal read (fast — no rescan)
    tmobile_signal = get_tmobile_signal()
    pub("van/status/starlink/tmobile-signal", tmobile_signal)
    pub("van/status/starlink/auto", "on" if auto_mode else "off")
    pub("van/status/starlink/threshold", signal_on_thresh)
    pub("van/status/starlink/quality", "unknown" if starlink_plug != "on" else "unknown")
    pub("van/status/starlink/manual-override", "on" if manual_override else "off")

    print(f"Init: upstream={upstream}, plug={starlink_plug}, state={state}, signal={tmobile_signal}")

    # If we're on T-Mobile and signal is already bad, start switch immediately
    with _state_lock:
        cur = state
    if auto_mode and cur in ("tmobile", "unknown") and (tmobile_signal == -1 or tmobile_signal < signal_on_thresh):
        print("Boot: T-Mobile signal already bad — starting Starlink switch")
        threading.Thread(target=do_switch_to_starlink, daemon=True).start()

    threading.Thread(target=signal_monitor, daemon=True).start()


def on_message(client, userdata, msg):
    global starlink_plug, auto_mode, signal_on_thresh, warmup_start
    topic   = msg.topic
    payload = msg.payload.decode().strip().lower()

    if topic == "van/starlink/power":
        print(f"Manual plug → {payload}")
        on = (payload == "on")
        plug_set(on)
        if not on:
            pub("van/status/starlink/quality", "unknown")
        return

    if topic == "van/starlink/auto":
        auto_mode = (payload == "on")
        pub("van/status/starlink/auto", "on" if auto_mode else "off")
        print(f"Auto-mode → {auto_mode}")
        # Toggling auto clears any manual override so auto logic runs fresh
        if auto_mode:
            manual_override = False
            pub("van/status/starlink/manual-override", "off")
        return

    if topic == "van/starlink/threshold":
        try:
            val = max(0, min(100, int(payload)))
            signal_on_thresh = val
            save_threshold(val)
            pub("van/status/starlink/threshold", val)
            print(f"Threshold → {val}")
        except ValueError:
            pass
        return

    if topic == "van/network/upstream":
        # Manual routing override from dashboard
        print(f"Manual upstream override → {payload}")

        def _set_manual_override():
            global manual_override, manual_override_expires
            manual_override         = True
            manual_override_expires = time.time() + MANUAL_OVERRIDE_SECS
            pub("van/status/starlink/manual-override", "on")
            mins = MANUAL_OVERRIDE_SECS // 60
            print(f"Manual override set — auto suspended for {mins} min")

        if payload == "starlink":
            with _state_lock:
                cur = state
            if cur == "starlink":
                print("Already on Starlink — refreshing manual override")
                _set_manual_override()
                return
            _set_manual_override()
            if starlink_plug == "on":
                ok = nmcli_connect("PhiladelphiaCollins")
                if ok:
                    set_state("starlink")
                    pub("van/status/network/upstream", "starlink")
            else:
                warmup_start = time.time()
                threading.Thread(target=do_switch_to_starlink, daemon=True).start()
        elif payload == "tmobile":
            with _state_lock:
                cur = state
            if cur == "tmobile":
                print("Already on T-Mobile — refreshing manual override")
                _set_manual_override()
                return
            _set_manual_override()
            threading.Thread(target=do_switch_to_tmobile, daemon=True).start()
        return


# ── Main ───────────────────────────────────────────────────────────────────

signal_on_thresh = load_threshold()

client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION1)
client.on_connect = on_connect
client.on_message = on_message
client.connect(MQTT_HOST, MQTT_PORT, 60)
client.loop_forever()
