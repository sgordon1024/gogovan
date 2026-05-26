#!/usr/bin/env python3
"""
obd-bridge.py — Reads OBD-II data from the vGate iCar Pro BT3 and publishes to MQTT.
Connects via /dev/rfcomm0 (Bluetooth Classic SPP, bound by rfcomm-obd.service).

Run pi-setup/setup-obd.sh once to pair the adapter, create the rfcomm binding
service, and install this service. After that, deploy-to-pi.sh handles updates.

Topics published (all retain=True):
  van/status/obd/connected    — "ok" / "searching" / "error"
  van/status/obd/rpm          — integer
  van/status/obd/speed        — integer mph
  van/status/obd/coolant-temp — integer °F
  van/status/obd/fuel-level   — integer %
  van/status/obd/throttle-pos — integer %
  van/status/obd/voltage      — float V (e.g. "14.2")
  van/status/obd/mil          — "on" / "off"
  van/status/obd/dtcs         — JSON array of strings
"""

import json
import time
import threading
import paho.mqtt.client as mqtt
import obd

MQTT_HOST  = 'localhost'
MQTT_PORT  = 1883
OBD_PORT   = '/dev/rfcomm0'
POLL_FAST  = 2    # seconds — live gauges
POLL_SLOW  = 30   # seconds — MIL + DTCs

BASE  = 'van/status/obd'
T_CONN     = f'{BASE}/connected'
T_RPM      = f'{BASE}/rpm'
T_SPEED    = f'{BASE}/speed'
T_COOLANT  = f'{BASE}/coolant-temp'
T_FUEL     = f'{BASE}/fuel-level'
T_THROTTLE = f'{BASE}/throttle-pos'
T_VOLTAGE  = f'{BASE}/voltage'
T_MIL      = f'{BASE}/mil'
T_DTCS     = f'{BASE}/dtcs'

_mqttc = None
_mqtt_ready = threading.Event()

# ── MQTT thread ───────────────────────────────────────────────────────────────

def _mqtt_thread():
    global _mqttc
    c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)

    def on_connect(client, userdata, flags, reason_code, properties):
        print(f'MQTT connected (rc={reason_code})')
        _mqtt_ready.set()

    c.on_connect = on_connect
    c.connect(MQTT_HOST, MQTT_PORT, 60)
    _mqttc = c
    c.loop_forever()

def publish(topic, value):
    if _mqttc and _mqttc.is_connected():
        _mqttc.publish(topic, str(value), retain=True)

# ── OBD helpers ───────────────────────────────────────────────────────────────

def _query(conn, cmd):
    """Query a PID; return the response or None if unsupported / null."""
    try:
        if cmd not in conn.supported_commands:
            return None
        r = conn.query(cmd)
        return None if r.is_null() else r
    except Exception:
        return None

def _c_to_f(celsius):
    return round(celsius * 9 / 5 + 32)

def _poll_gauges(conn):
    """Read the fast-poll PIDs and publish whatever the car supports."""
    r = _query(conn, obd.commands.RPM)
    if r:
        publish(T_RPM, round(r.value.magnitude))

    r = _query(conn, obd.commands.SPEED)
    if r:
        # python-obd returns kph; convert to mph
        mph = round(r.value.to('mph').magnitude)
        publish(T_SPEED, mph)

    r = _query(conn, obd.commands.COOLANT_TEMP)
    if r:
        publish(T_COOLANT, _c_to_f(r.value.magnitude))

    r = _query(conn, obd.commands.FUEL_LEVEL)
    if r:
        publish(T_FUEL, round(r.value.magnitude))

    r = _query(conn, obd.commands.THROTTLE_POS)
    if r:
        publish(T_THROTTLE, round(r.value.magnitude))

    r = _query(conn, obd.commands.CONTROL_MODULE_VOLTAGE)
    if r:
        publish(T_VOLTAGE, round(r.value.magnitude, 1))

def _poll_dtcs(conn):
    """Read MIL status and fault codes; publish results."""
    r = _query(conn, obd.commands.STATUS)
    if r:
        publish(T_MIL, 'on' if r.value.MIL else 'off')

    r = _query(conn, obd.commands.GET_DTC)
    if r is not None:
        codes = [f'{c[0]}: {c[1]}' for c in r.value] if r.value else []
        publish(T_DTCS, json.dumps(codes))

# ── Main OBD loop ─────────────────────────────────────────────────────────────

def obd_loop():
    _mqtt_ready.wait(timeout=10)  # wait for MQTT to connect before publishing

    while True:
        try:
            print(f'OBD: connecting to {OBD_PORT}…')
            publish(T_CONN, 'searching')

            # fast=False is more compatible with ELM327 clones on BT
            conn = obd.OBD(OBD_PORT, baudrate=None, fast=False, timeout=5)
            if not conn.is_connected():
                raise ConnectionError('OBD adapter did not connect')

            print(f'OBD: connected — {len(conn.supported_commands)} PIDs supported')
            publish(T_CONN, 'ok')

            fast_due = 0.0
            slow_due = 0.0

            while conn.is_connected():
                now = time.time()

                if now >= fast_due:
                    fast_due = now + POLL_FAST
                    _poll_gauges(conn)

                if now >= slow_due:
                    slow_due = now + POLL_SLOW
                    _poll_dtcs(conn)

                time.sleep(0.2)

            conn.close()
            raise ConnectionError('OBD disconnected')

        except Exception as e:
            print(f'OBD error: {e}')
            publish(T_CONN, 'error')
            time.sleep(5)


if __name__ == '__main__':
    threading.Thread(target=_mqtt_thread, daemon=True).start()
    obd_loop()
