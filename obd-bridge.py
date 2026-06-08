#!/usr/bin/env python3
"""
obd-bridge.py — Reads OBD-II data from the vGate iCar Pro BT3 and publishes to MQTT.
Connects via /dev/rfcomm0 (Bluetooth Classic SPP, bound by rfcomm-obd.service).

Run pi-setup/setup-obd.sh once to pair the adapter, create the rfcomm binding
service, and install this service. After that, deploy-to-pi.sh handles updates.

Topics published (all retain=True), under van/status/obd/:
  connected    — "ok" / "searching" / "error"
  rpm          — integer
  speed        — integer mph
  coolant-temp — integer °F
  fuel-level   — integer %
  throttle-pos — integer %
  voltage      — float V
  engine-load  — integer %
  fuel-rate    — float gal/hr
  mpg          — float (instant; speed ÷ fuel rate; 0 at idle)
  avg-mpg      — float (rolling average while moving)
  range        — integer miles to empty (fuel remaining × avg MPG)
  fuel-remaining — float gallons
  oil-temp     — integer °F
  ambient-temp — integer °F
  run-time     — integer seconds since engine start
  barometric   — integer kPa
  accel-pos    — integer %
  distance-mil — integer miles driven with MIL on
  distance-since-clear — integer miles since codes cleared
  mil          — "on" / "off"
  dtcs         — JSON array of strings
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
POLL_SLOW  = 30   # seconds — slow-changing values + MIL + DTCs

TANK_GALLONS = 24.5    # diesel main tank (2024 Entegra Launch / Sprinter 3500)
DEFAULT_MPG  = 18.0    # seed for range until the rolling average converges
MPG_ALPHA    = 0.05    # EMA smoothing for avg MPG (updates only while moving)

BASE = 'van/status/obd'
def T(s): return f'{BASE}/{s}'

_mqttc = None
_mqtt_ready = threading.Event()
_avg_mpg = None    # rolling-average MPG (EMA), None until the first moving sample
_range_ema = None  # smoothed distance-to-empty (EMA), None until first computed

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

def _to_gph(qty):
    """Fuel-rate Pint quantity → gallons/hour."""
    try:
        return qty.to('gallon/hour').magnitude
    except Exception:
        return qty.magnitude / 3.78541   # fall back assuming liters/hour

def _to_mi(qty):
    try:
        return qty.to('mile').magnitude
    except Exception:
        return qty.magnitude / 1.60934   # fall back assuming km

def _poll_gauges(conn):
    """Fast-poll PIDs + derived MPG / range."""
    global _avg_mpg, _range_ema
    speed_mph = fuel_gph = fuel_pct = None

    r = _query(conn, obd.commands.RPM)
    if r: publish(T('rpm'), round(r.value.magnitude))

    r = _query(conn, obd.commands.SPEED)
    if r:
        speed_mph = round(r.value.to('mph').magnitude)
        publish(T('speed'), speed_mph)

    r = _query(conn, obd.commands.COOLANT_TEMP)
    if r: publish(T('coolant-temp'), _c_to_f(r.value.magnitude))

    r = _query(conn, obd.commands.FUEL_LEVEL)
    if r:
        fuel_pct = r.value.magnitude
        publish(T('fuel-level'), round(fuel_pct))

    r = _query(conn, obd.commands.THROTTLE_POS)
    if r: publish(T('throttle-pos'), round(r.value.magnitude))

    # Accelerator pedal — fast-polled so the dashboard "Accelerator" bar is responsive.
    # (THROTTLE_POS reads a stuck ~13% on this diesel; the pedal PID is the real input.)
    r = _query(conn, obd.commands.ACCELERATOR_POS_D)
    if r: publish(T('accel-pos'), round(r.value.magnitude))

    r = _query(conn, obd.commands.CONTROL_MODULE_VOLTAGE)
    if r: publish(T('voltage'), round(r.value.magnitude, 1))

    r = _query(conn, obd.commands.ENGINE_LOAD)
    if r: publish(T('engine-load'), round(r.value.magnitude))

    r = _query(conn, obd.commands.FUEL_RATE)
    if r:
        fuel_gph = _to_gph(r.value)
        publish(T('fuel-rate'), round(fuel_gph, 2))

    # Instant MPG = speed ÷ fuel rate
    if speed_mph is not None and fuel_gph is not None and fuel_gph > 0.05:
        inst = speed_mph / fuel_gph
        publish(T('mpg'), round(inst, 1))
        if speed_mph >= 10 and inst < 60:   # only learn while genuinely moving
            _avg_mpg = inst if _avg_mpg is None else (_avg_mpg * (1 - MPG_ALPHA) + inst * MPG_ALPHA)
    elif speed_mph == 0:
        publish(T('mpg'), 0)

    if _avg_mpg is not None:
        publish(T('avg-mpg'), round(_avg_mpg, 1))

    # Distance to empty = fuel remaining (gal) × avg MPG. Smooth it (EMA) and round to
    # the nearest 5 mi so the dashboard number stays steady instead of jumping each poll.
    if fuel_pct is not None:
        gal = (fuel_pct / 100.0) * TANK_GALLONS
        publish(T('fuel-remaining'), round(gal, 1))
        mpg = _avg_mpg if _avg_mpg is not None else DEFAULT_MPG
        raw_range = gal * mpg
        _range_ema = raw_range if _range_ema is None else (_range_ema * 0.9 + raw_range * 0.1)
        publish(T('range'), int(round(_range_ema / 5.0) * 5))

def _poll_extra(conn):
    """Slower-changing values for the full OBD page."""
    r = _query(conn, obd.commands.OIL_TEMP)
    if r: publish(T('oil-temp'), _c_to_f(r.value.magnitude))

    r = _query(conn, obd.commands.AMBIANT_AIR_TEMP)
    if r: publish(T('ambient-temp'), _c_to_f(r.value.magnitude))

    r = _query(conn, obd.commands.RUN_TIME)
    if r: publish(T('run-time'), round(r.value.magnitude))

    r = _query(conn, obd.commands.BAROMETRIC_PRESSURE)
    if r: publish(T('barometric'), round(r.value.magnitude))

    r = _query(conn, obd.commands.DISTANCE_W_MIL)
    if r: publish(T('distance-mil'), round(_to_mi(r.value)))

    r = _query(conn, obd.commands.DISTANCE_SINCE_DTC_CLEAR)
    if r: publish(T('distance-since-clear'), round(_to_mi(r.value)))

def _poll_dtcs(conn):
    """MIL status and fault codes."""
    r = _query(conn, obd.commands.STATUS)
    if r: publish(T('mil'), 'on' if r.value.MIL else 'off')

    r = _query(conn, obd.commands.GET_DTC)
    if r is not None:
        codes = [f'{c[0]}: {c[1]}' for c in r.value] if r.value else []
        publish(T('dtcs'), json.dumps(codes))

# ── Main OBD loop ─────────────────────────────────────────────────────────────

def obd_loop():
    _mqtt_ready.wait(timeout=10)

    while True:
        try:
            print(f'OBD: connecting to {OBD_PORT}…')
            publish(T('connected'), 'searching')

            conn = obd.OBD(OBD_PORT, baudrate=None, fast=False, timeout=5)
            if not conn.is_connected():
                raise ConnectionError('OBD adapter did not connect')

            print(f'OBD: connected — {len(conn.supported_commands)} PIDs supported')
            publish(T('connected'), 'ok')

            fast_due = slow_due = 0.0
            while conn.is_connected():
                now = time.time()
                if now >= fast_due:
                    fast_due = now + POLL_FAST
                    _poll_gauges(conn)
                if now >= slow_due:
                    slow_due = now + POLL_SLOW
                    _poll_extra(conn)
                    _poll_dtcs(conn)
                time.sleep(0.2)

            conn.close()
            raise ConnectionError('OBD disconnected')

        except Exception as e:
            print(f'OBD error: {e}')
            publish(T('connected'), 'error')
            time.sleep(5)


if __name__ == '__main__':
    threading.Thread(target=_mqtt_thread, daemon=True).start()
    obd_loop()
