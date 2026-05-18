#!/usr/bin/env python3
"""
starlink-bridge.py — Starlink smart plug (Shelly Gen4) MQTT bridge.

Subscribes to van/starlink/+ commands and relays them to the Shelly plug
via its local RPC MQTT interface.  Also monitors T-Mobile signal strength
and auto-powers the plug when signal falls below threshold.

MQTT topics (in):
  van/starlink/power   — "on" / "off"
  van/starlink/auto    — "on" / "off"  (enable/disable auto-switch logic)

MQTT topics (out):
  van/status/starlink/power          — "on" / "off"  (retained)
  van/status/starlink/tmobile-signal — "0"-"100" dBm-relative or "-1" if unknown (retained)
  van/status/starlink/auto           — "on" / "off"  (retained)

Shelly Gen4 RPC topics:
  shellyplugus-{ID}/rpc              — publish JSON-RPC command
  shellyplugus-{ID}/status/switch:0  — subscribe for switch state
"""

import json
import subprocess
import threading
import time
import paho.mqtt.client as mqtt

# ── Configuration ──────────────────────────────────────────────────────────
MQTT_HOST = "localhost"
MQTT_PORT = 1883

# Replace with actual device ID after Shelly is added to GoGoVan network.
# Find it in the Shelly app under Device Info → Device ID, or check the
# label on the plug itself.  Format: shellyplugus-XXXXXXXXXXXX
SHELLY_ID = "shellyplugus-XXXXXXXXXXXX"

# T-Mobile signal thresholds (nmcli SIGNAL field, 0-100 scale)
# Below ON_THRESH  → turn Starlink ON  (bad signal, need backup)
# Above OFF_THRESH → turn Starlink OFF (good signal, save power)
# Hysteresis gap prevents rapid toggling.
SIGNAL_ON_THRESH  = 35   # signal drops below this → power Starlink on
SIGNAL_OFF_THRESH = 55   # signal rises above this → power Starlink off

# How often to poll T-Mobile signal strength (seconds)
SIGNAL_POLL_INTERVAL = 30

# ── State ──────────────────────────────────────────────────────────────────
starlink_power = None   # "on" / "off" / None (unknown)
auto_mode      = False  # whether auto-switch is enabled
tmobile_signal = -1     # 0-100 or -1 if unknown
rpc_request_id = 0      # incrementing RPC request counter

mqtt_client_ref = None


# ── Helpers ────────────────────────────────────────────────────────────────

def next_rpc_id():
    global rpc_request_id
    rpc_request_id += 1
    return rpc_request_id


def shelly_set(client, on: bool):
    """Send a Switch.Set RPC command to the Shelly plug."""
    payload = json.dumps({
        "id":     next_rpc_id(),
        "method": "Switch.Set",
        "params": {"id": 0, "on": on}
    })
    topic = f"{SHELLY_ID}/rpc"
    print(f"Shelly RPC → {topic}: {payload}")
    client.publish(topic, payload)


def get_tmobile_signal() -> int:
    """
    Query nmcli for the T-Mobile hotspot signal level.
    Returns 0-100 integer, or -1 on error / not visible.

    Uses `nmcli dev wifi list` (NOT `dev wifi` which only shows active)
    so we can see the T-Mobile SSID signal even when wlan0 is connected
    to Starlink.  If multiple entries exist for the same SSID (multi-AP),
    we return the highest signal seen.

    nmcli -t -f SSID,SIGNAL dev wifi list
    Example line: Gordie's T-Mobile Hotspot:72
    """
    try:
        result = subprocess.run(
            ["nmcli", "-t", "-f", "SSID,SIGNAL", "dev", "wifi", "list"],
            capture_output=True, text=True, timeout=10
        )
        best = -1
        for line in result.stdout.splitlines():
            # Last colon-delimited field is the signal; everything before is SSID
            # (SSID can itself contain colons, so split from right)
            idx = line.rfind(":")
            if idx < 0:
                continue
            ssid = line[:idx]
            sig_str = line[idx + 1:]
            ssid_lower = ssid.lower()
            if "t-mobile" in ssid_lower or "tmobile" in ssid_lower:
                try:
                    sig = int(sig_str)
                    if sig > best:
                        best = sig
                except ValueError:
                    pass
        return best
    except Exception as e:
        print(f"get_tmobile_signal error: {e}")
        return -1


def publish_status(client):
    """Publish all current status values (retained)."""
    if starlink_power is not None:
        client.publish("van/status/starlink/power", starlink_power, retain=True)
    client.publish("van/status/starlink/tmobile-signal", str(tmobile_signal), retain=True)
    client.publish("van/status/starlink/auto", "on" if auto_mode else "off", retain=True)


# ── Signal monitor loop ────────────────────────────────────────────────────

def signal_monitor(client):
    """
    Runs in a background thread.  Polls T-Mobile signal every
    SIGNAL_POLL_INTERVAL seconds and applies auto-power logic.
    """
    global tmobile_signal, starlink_power

    while True:
        time.sleep(SIGNAL_POLL_INTERVAL)
        try:
            sig = get_tmobile_signal()
            if sig != tmobile_signal:
                tmobile_signal = sig
                client.publish(
                    "van/status/starlink/tmobile-signal",
                    str(tmobile_signal),
                    retain=True
                )
                print(f"T-Mobile signal → {tmobile_signal}")

            if not auto_mode:
                continue

            # Auto-switch logic
            if sig != -1 and sig < SIGNAL_ON_THRESH:
                if starlink_power != "on":
                    print(f"Auto: T-Mobile signal {sig} < {SIGNAL_ON_THRESH} → Starlink ON")
                    shelly_set(client, True)
                    # Optimistic state update; confirmed via Shelly status callback
                    starlink_power = "on"
                    client.publish("van/status/starlink/power", "on", retain=True)
            elif sig != -1 and sig > SIGNAL_OFF_THRESH:
                if starlink_power == "on":
                    print(f"Auto: T-Mobile signal {sig} > {SIGNAL_OFF_THRESH} → Starlink OFF")
                    shelly_set(client, False)
                    starlink_power = "off"
                    client.publish("van/status/starlink/power", "off", retain=True)

        except Exception as e:
            print(f"signal_monitor error: {e}")


# ── MQTT callbacks ─────────────────────────────────────────────────────────

def on_connect(client, userdata, flags, rc):
    global mqtt_client_ref
    mqtt_client_ref = client
    print(f"Connected to MQTT (rc={rc})")

    # Dashboard command topics
    client.subscribe("van/starlink/power")
    client.subscribe("van/starlink/auto")

    # Shelly status topic — confirmed relay state
    client.subscribe(f"{SHELLY_ID}/status/switch:0")

    # Seed initial signal reading
    global tmobile_signal
    tmobile_signal = get_tmobile_signal()
    publish_status(client)
    print(f"Initial T-Mobile signal → {tmobile_signal}")

    # Start background signal monitor
    t = threading.Thread(target=signal_monitor, args=(client,), daemon=True)
    t.start()


def on_message(client, userdata, msg):
    global starlink_power, auto_mode
    topic   = msg.topic
    payload = msg.payload.decode().strip().lower()

    # ── Dashboard → Shelly power command ──────────────────────────────
    if topic == "van/starlink/power":
        print(f"van/starlink/power → {payload}")
        if payload == "on":
            shelly_set(client, True)
        elif payload == "off":
            shelly_set(client, False)
        return

    # ── Dashboard → auto-mode toggle ─────────────────────────────────
    if topic == "van/starlink/auto":
        auto_mode = (payload == "on")
        client.publish("van/status/starlink/auto", "on" if auto_mode else "off", retain=True)
        print(f"Starlink auto-mode → {auto_mode}")
        return

    # ── Shelly status callback — source of truth for relay state ─────
    if topic == f"{SHELLY_ID}/status/switch:0":
        try:
            data = json.loads(msg.payload)
            # Gen4 status payload: {"id":0,"output":true,"apower":...}
            output = data.get("output")
            if output is None:
                return
            new_state = "on" if output else "off"
            if new_state != starlink_power:
                starlink_power = new_state
                client.publish("van/status/starlink/power", starlink_power, retain=True)
                print(f"Shelly confirmed Starlink → {starlink_power}")
        except Exception as e:
            print(f"Shelly status parse error: {e} — raw: {msg.payload}")
        return


# ── Main ───────────────────────────────────────────────────────────────────

client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION1)
client.on_connect = on_connect
client.on_message = on_message
client.connect(MQTT_HOST, MQTT_PORT, 60)
client.loop_forever()
