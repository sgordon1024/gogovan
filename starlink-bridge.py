#!/usr/bin/env python3
"""
starlink-bridge.py — Peplink-style dual-WAN failover controller.

THE single authority for switching the Pi's wlan0 uplink between T-Mobile and
Starlink. The old `gogovan-watchdog` must be disabled — this replaces it.

Design principles
-----------------
1. T-Mobile is the DEFAULT (cheaper, lower power). Starlink dish is powered OFF
   whenever we're happily on T-Mobile.
2. ALL switching decisions are based on ACTUAL INTERNET reachability (ping to
   8.8.8.8 / 1.1.1.1), never on signal bars alone. Signal is only used as a
   cheap pre-filter to avoid pointlessly interrupting Starlink to test a
   T-Mobile that isn't even there.
3. Plug control is BEST-EFFORT. If the Tuya plug is unreachable, network
   failover still happens — we never let a missing plug strand the connection.

Behavior
--------
- On T-Mobile: every HEALTH_POLL_INTERVAL, test internet. FAIL_CONFIRM
  consecutive failures → fail over to Starlink (power plug on → wait for dish
  warmup → switch routing).
- On Starlink: every HEALTH_POLL_INTERVAL, test internet (publish quality).
  Every TMOBILE_RECHECK_INTERVAL, if T-Mobile has signal, briefly switch to it
  and test REAL internet — if good, stay on T-Mobile and power the dish off;
  if not, fall back to Starlink. If Starlink itself fails and T-Mobile has
  signal, try T-Mobile right away.
- Manual override (van/network/upstream): forces a side and suspends auto for
  MANUAL_OVERRIDE_SECS. Never powers off the dish unless the target's internet
  is confirmed (no stranding).

States: tmobile | starlink | warming_up | reverting | checking_tmobile | unknown

MQTT subscribe:
  van/starlink/power     — "on"/"off"            manual plug control
  van/starlink/auto      — "on"/"off"            enable/disable auto failover
  van/starlink/threshold — "0"-"100"             min T-Mobile signal to bother re-testing
  van/network/upstream   — "tmobile"/"starlink"  manual routing override

MQTT publish (all retained):
  van/status/starlink/power           — "on"/"off"/"unknown"
  van/status/starlink/tmobile-signal  — "0"-"100" or "-1"
  van/status/starlink/auto            — "on"/"off"
  van/status/starlink/threshold       — "0"-"100"
  van/status/starlink/quality         — "good"/"poor"/"unknown"
  van/status/starlink/state           — state machine state
  van/status/starlink/warmup_eta      — seconds remaining (warming_up only)
  van/status/network/upstream         — "tmobile"/"starlink"/"unknown"
  van/status/starlink/manual-override — "on"/"off"
"""

import os
import subprocess
import threading
import time
import paho.mqtt.client as mqtt
import tinytuya

# ── Network connection names (NetworkManager profiles on wlan0) ──────────────
TMOBILE_CONN  = "preconfigured"        # T-Mobile Home Internet
STARLINK_CONN = "PhiladelphiaCollins"  # Starlink Wi-Fi (5GHz-locked in NM profile)

# ── MQTT ─────────────────────────────────────────────────────────────────────
MQTT_HOST = "localhost"
MQTT_PORT = 1883

# ── Tuya plug (Starlink dish power) ──────────────────────────────────────────
PLUG_CLOUD_NAME       = "Smart Socket 3"            # Tuya cloud name of the dish plug — used to auto-refresh id+key
PLUG_DEV_ID           = "eb826ee30e0fd77018gwq2"    # local id (rotates on re-pair; auto-refreshed from cloud at startup)
PLUG_LOCAL_KEY        = "HlYX{/Y-Pv-M':)7"          # fallback key; auto-refreshed from cloud at startup
PLUG_VERSION          = 3.3
PLUG_ADDRESS_FALLBACK = "192.168.8.248"             # plug's reserved DHCP IP on the Apple Pi network
PLUG_ADDRESS_FILE     = os.path.expanduser("~/.starlink_plug_address")
PLUG_CREDS_FILE       = os.path.expanduser("~/.starlink_plug_creds")   # cached {id,key} from last cloud fetch
TUYA_CFG_FILE         = os.path.expanduser("~/tinytuya.json")          # saved Tuya cloud API creds

# ── Persistence ──────────────────────────────────────────────────────────────
THRESH_FILE = os.path.expanduser("~/.starlink_threshold")   # min T-Mobile signal
AUTO_FILE   = os.path.expanduser("~/.starlink_auto")        # auto on/off, survives restart

# ── Timing / thresholds ──────────────────────────────────────────────────────
PING_HOSTS               = ["8.8.8.8", "1.1.1.1"]
HEALTH_POLL_INTERVAL     = 20        # seconds between internet health checks
FAIL_CONFIRM             = 3         # consecutive failed checks before failover (~60s)
TMOBILE_RECHECK_INTERVAL = 20 * 60   # seconds between T-Mobile recovery attempts on Starlink
DEFAULT_MIN_SIGNAL       = 20        # don't bother testing T-Mobile below this signal
WARMUP_MAX_SECS          = 180       # max wait for Starlink dish to boot
WARMUP_POLL_SECS         = 10        # seconds between SSID scans during warmup
ROUTING_TIMEOUT          = 25        # seconds for an nmcli 'up' to succeed
MANUAL_OVERRIDE_SECS     = 30 * 60   # manual switch suspends auto this long
MANUAL_BAD_MBPS          = 2.0       # a manual speed test below this (or an error) triggers a source switch

# ── Module state ─────────────────────────────────────────────────────────────
state                   = "unknown"  # see States above
starlink_plug           = None       # "on" / "off" / None(unknown)
auto_mode               = True       # auto failover armed (persisted to AUTO_FILE)
min_signal              = DEFAULT_MIN_SIGNAL
tmobile_signal          = -1
tmobile_ssid            = ""
warmup_start            = 0.0
fail_count              = 0          # consecutive internet failures on current link
tmobile_stable_count    = 0          # consecutive good T-Mobile checks (gates dish power-off)
last_tmobile_recheck    = 0.0
manual_override         = False
manual_override_expires = 0.0
manual_test_pending     = 0.0       # epoch of last manual speed-test request (gates failover-on-result)
transitioning           = False     # True while a switch is in progress

_state_lock = threading.Lock()
mqtt_client = None


# ── Persistence helpers ──────────────────────────────────────────────────────

def load_threshold() -> int:
    try:
        return max(0, min(100, int(open(THRESH_FILE).read().strip())))
    except Exception:
        return DEFAULT_MIN_SIGNAL

def save_threshold(val: int):
    try:
        open(THRESH_FILE, "w").write(str(val))
    except Exception as e:
        print(f"save_threshold error: {e}")

def load_auto() -> bool:
    try:
        return open(AUTO_FILE).read().strip() != "off"
    except Exception:
        return True   # default ON

def save_auto(on: bool):
    try:
        open(AUTO_FILE, "w").write("on" if on else "off")
    except Exception as e:
        print(f"save_auto error: {e}")


# ── MQTT publish helper ──────────────────────────────────────────────────────

def pub(topic, payload, retain=True):
    if mqtt_client:
        mqtt_client.publish(topic, str(payload), retain=retain)


# ── Internet / connectivity ──────────────────────────────────────────────────

def internet_up() -> bool:
    """True if ANY ping host is reachable through the current uplink."""
    for host in PING_HOSTS:
        try:
            r = subprocess.run(["ping", "-c", "2", "-W", "2", "-q", host],
                               capture_output=True, timeout=8)
            if r.returncode == 0:
                return True
        except Exception:
            pass
    return False


# ── nmcli helpers ────────────────────────────────────────────────────────────

def get_current_upstream() -> str:
    """Return 'tmobile', 'starlink', or 'unknown' from the active wlan0 profile."""
    try:
        r = subprocess.run(["nmcli", "-t", "-f", "DEVICE,CONNECTION", "device", "status"],
                           capture_output=True, text=True, timeout=5)
        for line in r.stdout.splitlines():
            parts = line.split(":")
            if len(parts) >= 2 and parts[0] == "wlan0":
                conn = parts[1]
                if conn == TMOBILE_CONN:
                    return "tmobile"
                if conn == STARLINK_CONN:
                    return "starlink"
                return "unknown"
    except Exception as e:
        print(f"get_current_upstream error: {e}")
    return "unknown"

def nmcli_up(conn: str) -> bool:
    """Bring up a NetworkManager connection. Returns True on success."""
    try:
        r = subprocess.run(["sudo", "nmcli", "connection", "up", conn],
                           capture_output=True, text=True, timeout=ROUTING_TIMEOUT + 5)
        ok = r.returncode == 0
        print(f"nmcli up {conn}: {'ok' if ok else 'FAILED'} | {r.stderr.strip()}")
        return ok
    except Exception as e:
        print(f"nmcli_up({conn}) error: {e}")
        return False

def get_tmobile_ssid() -> str:
    try:
        r = subprocess.run(["nmcli", "-g", "802-11-wireless.ssid", "connection", "show", TMOBILE_CONN],
                           capture_output=True, text=True, timeout=5)
        return r.stdout.strip()
    except Exception as e:
        print(f"get_tmobile_ssid error: {e}")
    return ""

def get_tmobile_signal() -> int:
    """
    0-100 signal for T-Mobile, or -1 if not visible. Works while on Starlink
    (scans), or reads live signal directly when already on T-Mobile.
    """
    if get_current_upstream() == "tmobile":
        try:
            r = subprocess.run(["nmcli", "-t", "-f", "GENERAL.SIGNAL", "dev", "show", "wlan0"],
                               capture_output=True, text=True, timeout=5)
            for line in r.stdout.splitlines():
                if "SIGNAL" in line:
                    try:
                        return int(line.split(":")[-1])
                    except ValueError:
                        pass
        except Exception as e:
            print(f"get_tmobile_signal(direct) error: {e}")

    try:
        r = subprocess.run(["nmcli", "-t", "-f", "SSID,SIGNAL", "dev", "wifi", "list", "--rescan", "yes"],
                           capture_output=True, text=True, timeout=20)
        best = -1
        for line in r.stdout.splitlines():
            idx = line.rfind(":")
            if idx < 0:
                continue
            ssid_part, sig_part = line[:idx], line[idx + 1:]
            is_match = ((tmobile_ssid and ssid_part == tmobile_ssid)
                        or "t-mobile" in ssid_part.lower()
                        or "tmobile" in ssid_part.lower())
            if is_match:
                try:
                    best = max(best, int(sig_part))
                except ValueError:
                    pass
        return best
    except Exception as e:
        print(f"get_tmobile_signal(scan) error: {e}")
    return -1

def starlink_ssid_visible() -> bool:
    try:
        r = subprocess.run(["nmcli", "-t", "-f", "SSID", "dev", "wifi", "list", "--rescan", "yes"],
                           capture_output=True, text=True, timeout=20)
        return any(line.strip() == STARLINK_CONN for line in r.stdout.splitlines())
    except Exception as e:
        print(f"starlink_ssid_visible error: {e}")
        return False


# ── Tuya plug (best-effort) ──────────────────────────────────────────────────

_plug_address = None

def refresh_plug_creds():
    """
    Re-pairing the plug in the Smart Life app rotates its local id AND key.
    On startup, fetch the current id+key from the Tuya cloud (matched by the
    device's cloud name) so we auto-recover from re-pairs. Falls back to a
    cached file, then the hardcoded constants. Best-effort — never raises.
    """
    global PLUG_DEV_ID, PLUG_LOCAL_KEY
    import json
    # 1. Try the cloud (needs internet + saved API creds in tinytuya.json)
    try:
        c = json.load(open(TUYA_CFG_FILE))
        cloud = tinytuya.Cloud(apiRegion=c["apiRegion"], apiKey=c["apiKey"], apiSecret=c["apiSecret"])
        res = cloud.getdevices(True)
        devs = res.get("result", []) if isinstance(res, dict) else res
        for d in devs:
            if d.get("name") == PLUG_CLOUD_NAME:
                i = d.get("id"); k = d.get("local_key") or d.get("key")
                if i and k:
                    PLUG_DEV_ID, PLUG_LOCAL_KEY = i, k
                    try:
                        open(PLUG_CREDS_FILE, "w").write(json.dumps({"id": i, "key": k}))
                    except Exception:
                        pass
                    print(f"Plug creds refreshed from cloud: id={i}")
                    return
        print(f"Cloud reachable but '{PLUG_CLOUD_NAME}' not found — keeping current creds")
    except Exception as e:
        print(f"Cloud plug-cred refresh skipped ({e}) — using cached/hardcoded")
    # 2. Fall back to cached creds from the last successful cloud fetch
    try:
        cached = json.load(open(PLUG_CREDS_FILE))
        if cached.get("id") and cached.get("key"):
            PLUG_DEV_ID, PLUG_LOCAL_KEY = cached["id"], cached["key"]
            print(f"Using cached plug creds: id={PLUG_DEV_ID}")
            return
    except Exception:
        pass
    print(f"Using hardcoded plug creds: id={PLUG_DEV_ID}")

def discover_plug_address() -> str:
    global _plug_address
    if _plug_address:
        return _plug_address
    try:
        print("Scanning for Starlink plug…")
        found = tinytuya.deviceScan(verbose=False, maxretry=5)
        for ip, info in found.items():
            if info.get("gwId") == PLUG_DEV_ID or info.get("id") == PLUG_DEV_ID:
                print(f"Plug found at {ip}")
                _plug_address = ip
                try:
                    open(PLUG_ADDRESS_FILE, "w").write(ip)
                except Exception:
                    pass
                return ip
    except Exception as e:
        print(f"Plug scan error: {e}")
    try:
        addr = open(PLUG_ADDRESS_FILE).read().strip()
        if addr:
            _plug_address = addr
            return addr
    except Exception:
        pass
    _plug_address = PLUG_ADDRESS_FALLBACK
    return PLUG_ADDRESS_FALLBACK

def _make_device():
    d = tinytuya.OutletDevice(dev_id=PLUG_DEV_ID, address=discover_plug_address(),
                              local_key=PLUG_LOCAL_KEY, version=PLUG_VERSION)
    d.set_socketTimeout(5)
    d.set_socketRetryLimit(2)
    return d

def plug_set(on: bool) -> bool:
    """Best-effort plug control. Returns True on success, False if unreachable."""
    global starlink_plug, _plug_address
    try:
        result = _make_device().set_value(1, on)
        if "Error" not in str(result):
            starlink_plug = "on" if on else "off"
            pub("van/status/starlink/power", starlink_plug)
            print(f"Plug → {'ON' if on else 'OFF'}")
            return True
        print(f"Plug set error: {result}")
        _plug_address = None
    except Exception as e:
        print(f"plug_set error: {e}")
        _plug_address = None
    return False

def plug_get_state() -> str:
    try:
        status = _make_device().status()
        if "dps" in status:
            return "on" if status["dps"].get("1", False) else "off"
    except Exception as e:
        print(f"plug_get_state error: {e}")
    return "unknown"


# ── State helpers ────────────────────────────────────────────────────────────

def set_state(new_state: str):
    global state
    with _state_lock:
        state = new_state
    pub("van/status/starlink/state", new_state)
    print(f"State → {new_state}")

def _begin_transition() -> bool:
    """Claim the transition lock. Returns False if a switch is already running."""
    global transitioning
    with _state_lock:
        if transitioning:
            return False
        transitioning = True
    return True

def _end_transition():
    global transitioning
    with _state_lock:
        transitioning = False


# ── Transitions ──────────────────────────────────────────────────────────────

def switch_to_starlink(reason: str) -> bool:
    """Power on dish → warmup → route to Starlink → verify. Returns True if Starlink has internet."""
    global warmup_start, fail_count
    if not _begin_transition():
        print("switch_to_starlink: already transitioning, skip")
        return False
    try:
        print(f"FAILOVER → Starlink ({reason})")

        # Power the dish on (best-effort). Don't abort if the plug is unreachable —
        # the dish may already be powered, and network failover matters more.
        if starlink_plug != "on":
            if not plug_set(True):
                print("Plug unreachable — continuing failover anyway (dish may already be on)")

        # Wait for the Starlink SSID to appear (dish boot), then connect.
        warmup_start = time.time()
        set_state("warming_up")
        pub("van/status/starlink/warmup_eta", WARMUP_MAX_SECS)
        deadline = warmup_start + WARMUP_MAX_SECS
        while time.time() < deadline:
            remaining = max(0, int(deadline - time.time()))
            pub("van/status/starlink/warmup_eta", remaining)
            if starlink_ssid_visible():
                print("Starlink SSID visible — connecting")
                break
            print(f"Warmup: waiting for Starlink… {remaining}s left")
            time.sleep(WARMUP_POLL_SECS)
        pub("van/status/starlink/warmup_eta", 0)

        ok = nmcli_up(STARLINK_CONN)
        if not ok:
            time.sleep(10)
            ok = nmcli_up(STARLINK_CONN)
        result = False
        if ok:
            set_state("starlink")
            pub("van/status/network/upstream", "starlink")
            time.sleep(4)
            result = internet_up()
            pub("van/status/starlink/quality", "good" if result else "poor")
        else:
            print("Could not connect to Starlink — leaving state unknown")
            set_state("unknown")
        fail_count = 0
        return result
    finally:
        _end_transition()

def switch_to_tmobile(reason: str, power_off_dish: bool = True) -> bool:
    """
    Route to T-Mobile and verify internet. Only powers the dish off if T-Mobile
    internet is confirmed (never strand the connection). Returns True if we
    ended up on a working T-Mobile.
    """
    global fail_count
    if not _begin_transition():
        print("switch_to_tmobile: already transitioning, skip")
        return False
    try:
        print(f"→ T-Mobile ({reason})")
        set_state("reverting")
        if not nmcli_up(TMOBILE_CONN):
            print("nmcli T-Mobile failed — falling back to Starlink")
            nmcli_up(STARLINK_CONN)
            set_state("starlink")
            return False

        time.sleep(4)
        if internet_up():
            set_state("tmobile")
            pub("van/status/network/upstream", "tmobile")
            pub("van/status/starlink/quality", "unknown")
            fail_count = 0
            if power_off_dish:
                # We have working T-Mobile — safe to drop the dish to save power.
                if not plug_set(False):
                    print("Plug unreachable — dish left on (can't power off)")
            return True
        else:
            print("T-Mobile has no internet — staying on Starlink")
            nmcli_up(STARLINK_CONN)
            set_state("starlink")
            pub("van/status/network/upstream", "starlink")
            return False
    finally:
        _end_transition()

def handle_bad_connection(reason: str):
    """
    A manual speed test reported the current link is bad/down. Switch to the
    OTHER source. If that source also has no usable internet, raise an alert
    toast on the dashboard. Works with no internet on the current link (the
    switch + verification are all local nmcli/ping).
    """
    with _state_lock:
        if transitioning:
            print("handle_bad_connection: transition in progress — skipping")
            return
    cur = get_current_upstream()
    print(f"Manual speed test says current link ({cur}) is bad ({reason}) — switching source")
    if cur == "starlink":
        ok = switch_to_tmobile(f"manual test: {reason}")
    else:
        ok = switch_to_starlink(f"manual test: {reason}")
    if ok:
        pub("van/status/network/alert", "", retain=False)   # clear any warning
    else:
        msg = "Both T-Mobile and Starlink have no usable internet right now."
        print("ALERT: " + msg)
        pub("van/status/network/alert", msg, retain=False)


# ── Control loop ─────────────────────────────────────────────────────────────

def control_loop():
    global tmobile_signal, fail_count, last_tmobile_recheck, tmobile_stable_count
    global manual_override, manual_override_expires

    while True:
        time.sleep(HEALTH_POLL_INTERVAL)
        try:
            # Manual override countdown
            if manual_override:
                if time.time() < manual_override_expires:
                    continue
                manual_override = False
                pub("van/status/starlink/manual-override", "off")
                print("Manual override expired — auto resumed")

            if not auto_mode:
                continue

            with _state_lock:
                if transitioning:
                    continue
                cur = state

            # Resolve from transient states by reading the actual interface
            if cur in ("unknown", "warming_up", "reverting", "checking_tmobile"):
                cur = get_current_upstream()

            # Refresh T-Mobile signal for the dashboard (cheap scan)
            sig = get_tmobile_signal()
            if sig != tmobile_signal:
                tmobile_signal = sig
                pub("van/status/starlink/tmobile-signal", sig)

            # ── On T-Mobile: watch its internet ───────────────────────────
            if cur == "tmobile":
                if internet_up():
                    fail_count = 0
                    tmobile_stable_count += 1
                    # Power-saving: once T-Mobile has been solidly up (~1 min), the
                    # Starlink dish isn't needed — power it off (best-effort).
                    if starlink_plug == "on" and tmobile_stable_count >= 3:
                        print("T-Mobile stable — powering Starlink dish off (power saving)")
                        plug_set(False)
                else:
                    fail_count += 1
                    tmobile_stable_count = 0
                    print(f"T-Mobile internet check failed ({fail_count}/{FAIL_CONFIRM})")
                    if fail_count >= FAIL_CONFIRM:
                        fail_count = 0
                        threading.Thread(target=switch_to_starlink,
                                         args=("T-Mobile internet down",), daemon=True).start()

            # ── On Starlink: monitor + periodically try to get back to T-Mobile ──
            elif cur == "starlink":
                sl_ok = internet_up()
                pub("van/status/starlink/quality", "good" if sl_ok else "poor")

                if not sl_ok:
                    # Starlink itself is failing. If T-Mobile has signal, try it now.
                    fail_count += 1
                    print(f"Starlink internet check failed ({fail_count}/{FAIL_CONFIRM})")
                    if fail_count >= FAIL_CONFIRM and sig >= min_signal:
                        fail_count = 0
                        threading.Thread(target=switch_to_tmobile,
                                         args=("Starlink down, trying T-Mobile",), daemon=True).start()
                        continue
                else:
                    fail_count = 0

                # Periodic T-Mobile recovery attempt
                if time.time() - last_tmobile_recheck >= TMOBILE_RECHECK_INTERVAL:
                    last_tmobile_recheck = time.time()
                    if sig >= min_signal:
                        print(f"T-Mobile recheck: signal={sig} present, testing real internet…")
                        set_state("checking_tmobile")
                        threading.Thread(target=switch_to_tmobile,
                                         args=("periodic recovery check",), daemon=True).start()
                    else:
                        print(f"T-Mobile recheck: signal={sig} too low, staying on Starlink")

            # ── Unknown: establish a link, preferring T-Mobile ────────────
            else:
                print("No clear upstream — establishing (prefer T-Mobile)…")
                if not switch_to_tmobile("startup/recover", power_off_dish=False):
                    threading.Thread(target=switch_to_starlink,
                                     args=("T-Mobile unavailable",), daemon=True).start()

        except Exception as e:
            print(f"control_loop error: {e}")


# ── MQTT callbacks ───────────────────────────────────────────────────────────

def on_connect(client, userdata, flags, rc):
    global mqtt_client, starlink_plug, min_signal, tmobile_ssid, tmobile_signal, auto_mode
    global last_tmobile_recheck
    mqtt_client = client
    print(f"MQTT connected (rc={rc})")

    # Don't disrupt a working link immediately on (re)start — let the first
    # periodic T-Mobile recheck happen after the normal interval.
    last_tmobile_recheck = time.time()

    min_signal   = load_threshold()
    auto_mode    = load_auto()
    tmobile_ssid = get_tmobile_ssid()
    print(f"T-Mobile SSID: '{tmobile_ssid}' | auto={auto_mode} | min_signal={min_signal}")

    client.subscribe("van/starlink/power")
    client.subscribe("van/starlink/auto")
    client.subscribe("van/starlink/threshold")
    client.subscribe("van/network/upstream")
    client.subscribe("van/network/speedtest")          # manual test trigger ("run")
    client.subscribe("van/status/network/speedtest")   # test result (react to manual ones)

    # Initial plug + upstream snapshot
    plug_state    = plug_get_state()
    starlink_plug = plug_state if plug_state != "unknown" else None
    pub("van/status/starlink/power", starlink_plug or "unknown")

    upstream = get_current_upstream()
    pub("van/status/network/upstream", upstream)
    set_state(upstream if upstream in ("tmobile", "starlink") else "unknown")

    tmobile_signal = get_tmobile_signal()
    pub("van/status/starlink/tmobile-signal", tmobile_signal)
    pub("van/status/starlink/auto", "on" if auto_mode else "off")
    pub("van/status/starlink/threshold", min_signal)
    pub("van/status/starlink/quality", "unknown")
    pub("van/status/starlink/manual-override", "on" if manual_override else "off")

    print(f"Init: upstream={upstream}, plug={starlink_plug}, signal={tmobile_signal}")
    threading.Thread(target=control_loop, daemon=True).start()


def _arm_manual_override():
    global manual_override, manual_override_expires
    manual_override         = True
    manual_override_expires = time.time() + MANUAL_OVERRIDE_SECS
    pub("van/status/starlink/manual-override", "on")
    print(f"Manual override armed — auto suspended {MANUAL_OVERRIDE_SECS // 60} min")


def on_message(client, userdata, msg):
    global auto_mode, min_signal, manual_override, manual_test_pending
    topic   = msg.topic
    payload = msg.payload.decode().strip().lower()

    if topic == "van/starlink/power":
        print(f"Manual plug → {payload}")
        plug_set(payload == "on")
        if payload != "on":
            pub("van/status/starlink/quality", "unknown")
        return

    if topic == "van/starlink/auto":
        auto_mode = (payload == "on")
        save_auto(auto_mode)
        pub("van/status/starlink/auto", "on" if auto_mode else "off")
        print(f"Auto → {auto_mode}")
        if auto_mode:
            manual_override = False
            pub("van/status/starlink/manual-override", "off")
        return

    if topic == "van/starlink/threshold":
        try:
            min_signal = max(0, min(100, int(payload)))
            save_threshold(min_signal)
            pub("van/status/starlink/threshold", min_signal)
            print(f"Min signal → {min_signal}")
        except ValueError:
            pass
        return

    if topic == "van/network/upstream":
        print(f"Manual upstream → {payload}")
        _arm_manual_override()
        if payload == "starlink":
            threading.Thread(target=switch_to_starlink, args=("manual override",), daemon=True).start()
        elif payload == "tmobile":
            # Manual T-Mobile: don't power dish off unless T-Mobile internet confirmed.
            threading.Thread(target=switch_to_tmobile, args=("manual override",), daemon=True).start()
        return

    if topic == "van/network/speedtest":
        if payload == "run":
            manual_test_pending = time.time()
            print("Manual speed test requested — will check its result for failover")
        return

    if topic == "van/status/network/speedtest":
        # Only react to a RECENT manual test — ignore the retained/periodic results
        if time.time() - manual_test_pending > 150:
            return
        try:
            import json as _json
            res = _json.loads(msg.payload.decode())   # raw payload (not lowercased)
        except Exception:
            return
        manual_test_pending = 0.0   # consume this request
        dl  = res.get("download")
        err = res.get("error")
        bad = bool(err) or (isinstance(dl, (int, float)) and dl < MANUAL_BAD_MBPS)
        if bad:
            why = f"error: {err}" if err else f"only {dl} Mbps"
            print(f"Manual speed test BAD ({why}) — switching source")
            threading.Thread(target=handle_bad_connection, args=(why,), daemon=True).start()
        else:
            print(f"Manual speed test OK ({dl} Mbps) — no switch needed")
        return


# ── Main ─────────────────────────────────────────────────────────────────────

min_signal = load_threshold()
auto_mode  = load_auto()
refresh_plug_creds()   # pull current plug id+key from Tuya cloud (auto-recovers from re-pairs)

client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION1)
client.on_connect = on_connect
client.on_message = on_message
client.connect(MQTT_HOST, MQTT_PORT, 60)
client.loop_forever()
