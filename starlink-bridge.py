#!/usr/bin/env python3
"""
starlink-bridge.py — Starlink smart plug (Tuya X5P) MQTT bridge.

Controls the Tuya smart plug via tinytuya local API.
Monitors T-Mobile signal and auto-powers Starlink when signal is poor.
Monitors connectivity quality when Starlink is active upstream.

MQTT topics (subscribe):
  van/starlink/power     — "on" / "off"  (manual control from dashboard)
  van/starlink/auto      — "on" / "off"  (enable/disable auto-switch)
  van/starlink/threshold — "0"-"100"     (signal level below which Starlink turns on)

MQTT topics (publish, retained):
  van/status/starlink/power          — "on" / "off" / "unknown"
  van/status/starlink/tmobile-signal — "0"-"100" or "-1" if unknown
  van/status/starlink/auto           — "on" / "off"
  van/status/starlink/threshold      — "0"-"100"
  van/status/starlink/quality        — "good" / "poor" / "unknown"
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
PLUG_ADDRESS   = "192.168.4.34"
PLUG_LOCAL_KEY = "knGT9!<jN3jA~npU"
PLUG_VERSION   = 3.3

THRESH_FILE            = os.path.expanduser("~/.starlink_threshold")
DEFAULT_ON_THRESH      = 35      # signal below this → Starlink ON
HYSTERESIS             = 20      # off threshold = on_thresh + HYSTERESIS
SIGNAL_POLL_INTERVAL   = 30      # seconds between T-Mobile signal polls
QUALITY_CHECK_INTERVAL = 120     # seconds between ping quality checks (when on Starlink)
QUALITY_PING_TIMEOUT   = 15      # total seconds for ping subprocess

# ── Helpers ────────────────────────────────────────────────────────────────

def load_threshold() -> int:
    try:
        return max(0, min(100, int(open(THRESH_FILE).read().strip())))
    except Exception:
        return DEFAULT_ON_THRESH

def save_threshold(val: int):
    try:
        with open(THRESH_FILE, "w") as f:
            f.write(str(val))
    except Exception as e:
        print(f"save_threshold error: {e}")

# ── State ──────────────────────────────────────────────────────────────────
starlink_power     = None    # "on" / "off" / None (unknown)
auto_mode          = False
tmobile_signal     = -1
signal_on_thresh   = load_threshold()
network_upstream   = "unknown"   # from van/status/network/upstream
last_quality_check = 0.0         # timestamp of last connectivity check

# ── Plug control ───────────────────────────────────────────────────────────

def make_device():
    d = tinytuya.OutletDevice(
        dev_id=PLUG_DEV_ID,
        address=PLUG_ADDRESS,
        local_key=PLUG_LOCAL_KEY,
        version=PLUG_VERSION
    )
    d.set_socketTimeout(5)
    d.set_socketRetryLimit(2)
    return d


def plug_set(on: bool) -> bool:
    """Set plug state. Returns True on success."""
    global starlink_power
    try:
        d = make_device()
        result = d.set_value(1, on)
        if "Error" not in str(result):
            starlink_power = "on" if on else "off"
            print(f"Plug → {'ON' if on else 'OFF'}: {result}")
            return True
        else:
            print(f"Plug set error: {result}")
            return False
    except Exception as e:
        print(f"plug_set exception: {e}")
        return False


def plug_get_state() -> str:
    """Query current plug state. Returns 'on', 'off', or 'unknown'."""
    try:
        d = make_device()
        status = d.status()
        if "dps" in status:
            return "on" if status["dps"].get("1", False) else "off"
        return "unknown"
    except Exception as e:
        print(f"plug_get_state exception: {e}")
        return "unknown"


# ── T-Mobile signal ────────────────────────────────────────────────────────

def get_tmobile_signal() -> int:
    """
    Returns 0-100 signal level for T-Mobile, or -1 if not visible.
    Checks both connected signal (nmcli dev show) and scan results.
    """
    try:
        # First: check if wlan0 is currently on T-Mobile (fastest path)
        result = subprocess.run(
            ["nmcli", "-t", "-f", "GENERAL.CONNECTION,GENERAL.SIGNAL",
             "dev", "show", "wlan0"],
            capture_output=True, text=True, timeout=5
        )
        for line in result.stdout.splitlines():
            if "SIGNAL" in line:
                try:
                    return int(line.split(":")[-1])
                except ValueError:
                    pass

        # Fallback: scan for T-Mobile SSID
        result = subprocess.run(
            ["nmcli", "-t", "-f", "SSID,SIGNAL", "dev", "wifi", "list"],
            capture_output=True, text=True, timeout=10
        )
        best = -1
        for line in result.stdout.splitlines():
            idx = line.rfind(":")
            if idx < 0:
                continue
            ssid = line[:idx].lower()
            if "t-mobile" in ssid or "tmobile" in ssid or ssid == "tmobile":
                try:
                    sig = int(line[idx + 1:])
                    best = max(best, sig)
                except ValueError:
                    pass
        return best
    except Exception as e:
        print(f"get_tmobile_signal error: {e}")
        return -1


# ── Connectivity quality check ─────────────────────────────────────────────

def check_connectivity() -> str:
    """
    Ping 8.8.8.8 three times and assess quality.
    Returns 'good', 'poor', or 'unknown'.
    Typical healthy Starlink: 20-80 ms avg.
    Obstructed / degraded: fails to respond or >2000 ms avg.
    """
    try:
        result = subprocess.run(
            ["ping", "-c", "3", "-W", "3", "-q", "8.8.8.8"],
            capture_output=True, text=True, timeout=QUALITY_PING_TIMEOUT
        )
        if result.returncode != 0:
            return "poor"
        # Parse "rtt min/avg/max/mdev = X/X/X/X ms" line
        for line in result.stdout.splitlines():
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


# ── Signal monitor loop ────────────────────────────────────────────────────

def signal_monitor(client):
    global tmobile_signal, starlink_power, last_quality_check

    while True:
        time.sleep(SIGNAL_POLL_INTERVAL)
        try:
            sig = get_tmobile_signal()
            if sig != tmobile_signal:
                tmobile_signal = sig
                client.publish("van/status/starlink/tmobile-signal",
                               str(tmobile_signal), retain=True)
                print(f"T-Mobile signal → {tmobile_signal}")

            if auto_mode:
                off_thresh = min(signal_on_thresh + HYSTERESIS, 95)
                if sig != -1 and sig < signal_on_thresh and starlink_power != "on":
                    print(f"Auto: signal {sig} < {signal_on_thresh} → Starlink ON")
                    if plug_set(True):
                        client.publish("van/status/starlink/power", "on", retain=True)

                elif sig != -1 and sig > off_thresh and starlink_power == "on":
                    print(f"Auto: signal {sig} > {off_thresh} → Starlink OFF")
                    if plug_set(False):
                        client.publish("van/status/starlink/power", "off", retain=True)
                        client.publish("van/status/starlink/quality",
                                       "unknown", retain=True)

            # Quality check: only when Starlink plug is on AND Pi is routing through it
            now = time.time()
            if (starlink_power == "on"
                    and network_upstream == "starlink"
                    and now - last_quality_check >= QUALITY_CHECK_INTERVAL):
                last_quality_check = now
                quality = check_connectivity()
                print(f"Starlink quality → {quality}")
                client.publish("van/status/starlink/quality", quality, retain=True)

        except Exception as e:
            print(f"signal_monitor error: {e}")


# ── MQTT callbacks ─────────────────────────────────────────────────────────

def on_connect(client, userdata, flags, rc):
    global tmobile_signal, starlink_power
    print(f"MQTT connected (rc={rc})")

    client.subscribe("van/starlink/power")
    client.subscribe("van/starlink/auto")
    client.subscribe("van/starlink/threshold")
    client.subscribe("van/status/network/upstream")  # track upstream for quality check

    state = plug_get_state()
    starlink_power = state if state != "unknown" else None
    client.publish("van/status/starlink/power",
                   starlink_power or "unknown", retain=True)

    tmobile_signal = get_tmobile_signal()
    client.publish("van/status/starlink/tmobile-signal",
                   str(tmobile_signal), retain=True)
    client.publish("van/status/starlink/auto",
                   "on" if auto_mode else "off", retain=True)
    client.publish("van/status/starlink/threshold",
                   str(signal_on_thresh), retain=True)
    # Clear quality on (re)start so stale "poor" doesn't persist if plug is off
    if starlink_power != "on":
        client.publish("van/status/starlink/quality", "unknown", retain=True)

    print(f"Initial: plug={starlink_power}, tmobile={tmobile_signal}, "
          f"thresh={signal_on_thresh}, auto={auto_mode}")

    t = threading.Thread(target=signal_monitor, args=(client,), daemon=True)
    t.start()


def on_message(client, userdata, msg):
    global starlink_power, auto_mode, signal_on_thresh, network_upstream
    topic   = msg.topic
    payload = msg.payload.decode().strip().lower()

    if topic == "van/starlink/power":
        print(f"Manual: Starlink → {payload}")
        on = (payload == "on")
        if plug_set(on):
            client.publish("van/status/starlink/power",
                           "on" if on else "off", retain=True)
            if not on:
                client.publish("van/status/starlink/quality",
                               "unknown", retain=True)
        return

    if topic == "van/starlink/auto":
        auto_mode = (payload == "on")
        client.publish("van/status/starlink/auto",
                       "on" if auto_mode else "off", retain=True)
        print(f"Auto-mode → {auto_mode}")
        return

    if topic == "van/starlink/threshold":
        try:
            val = max(0, min(100, int(payload)))
            signal_on_thresh = val
            save_threshold(val)
            client.publish("van/status/starlink/threshold",
                           str(val), retain=True)
            print(f"Threshold → {val}")
        except ValueError:
            pass
        return

    if topic == "van/status/network/upstream":
        network_upstream = payload
        # Reset quality check timer when upstream changes so we check quickly
        global last_quality_check
        last_quality_check = 0.0
        return


# ── Main ───────────────────────────────────────────────────────────────────

client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION1)
client.on_connect = on_connect
client.on_message = on_message
client.connect(MQTT_HOST, MQTT_PORT, 60)
client.loop_forever()
