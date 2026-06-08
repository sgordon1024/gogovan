#!/usr/bin/env python3
import socket
import struct
import subprocess
import threading
import time
import paho.mqtt.client as mqtt

MQTT_HOST = "localhost"
MQTT_PORT = 1883
CAN_IFACE = "can1"
SA = "44"

LIGHTS = {
    "kitchen":  "16",
    "bed":      "18",
    "ceiling":  "20",
    "cargo":    "19",
    "bunk":     "22",
    "bench":    "23",
    "awning":   "15",
    "step":     "17",
    "pump":     "2C",
}

# Tank heater: 4 outputs (fresh/grey/black tanks + underbelly) switched together
TANK_HEATER_INSTANCES = ["05", "06", "07", "08"]

# Reverse map: instance hex -> light name (for reading G12 status)
INSTANCE_TO_LIGHT = {v: k for k, v in LIGHTS.items()}
for _inst in TANK_HEATER_INSTANCES:
    INSTANCE_TO_LIGHT[_inst] = "tank-heater"

MOTORS = {
    "awning-extend":  {"on_inst": "03", "off_inst": "04"},
    "awning-retract": {"on_inst": "04", "off_inst": "03"},
}

# AC control: PGN 0x1FEF9 (proprietary Firefly thermostat command), SA=0x44
# Discovered from candump of LCD (SA=0x9F) controlling G12 thermostat.
AC_CAN_ID = "19FEF944"

# Module-level MQTT client reference (set in on_connect)
mqtt_client_ref = None

# ── AC sleep timer ────────────────────────────────────────────────────────────
# Runs server-side so the AC turns off at the scheduled time even when the phone
# is asleep / the dashboard is closed (a browser setTimeout would not fire then).
ac_timer = None
ac_timer_lock = threading.Lock()

def _clear_ac_timer():
    global ac_timer
    with ac_timer_lock:
        if ac_timer is not None:
            ac_timer.cancel()
            ac_timer = None

def _fire_ac_timer():
    global ac_timer
    with ac_timer_lock:
        ac_timer = None
    print("AC sleep timer expired → turning AC off")
    _clear_ac_cycle()            # also stop any running duty cycle
    send_ac("00C0FFFFFFFFFFFF")  # System OFF
    if mqtt_client_ref:
        mqtt_client_ref.publish("van/status/ac/mode", "off", retain=True)
        mqtt_client_ref.publish("van/status/ac/timer", "", retain=True)

# ── AC sleep cycle (sensor-independent duty cycling) ──────────────────────────
# Alternates Cool-ON (on Low) / System-OFF on a fixed timer so cooling doesn't depend
# on the temp sensor (the bathroom door blocks it). Runs server-side so it keeps
# cycling while the phone is asleep.
ac_cycle_timer = None
ac_cycle_spec  = None   # (on_min, off_min) or None
ac_cycle_lock  = threading.Lock()

def _clear_ac_cycle(publish_status=True):
    global ac_cycle_timer, ac_cycle_spec
    with ac_cycle_lock:
        if ac_cycle_timer is not None:
            ac_cycle_timer.cancel()
            ac_cycle_timer = None
        ac_cycle_spec = None
    if publish_status and mqtt_client_ref:
        mqtt_client_ref.publish("van/status/ac/cycle", "", retain=True)

def _ac_cycle_step(phase):
    """Run one phase of the duty cycle, then schedule the next."""
    global ac_cycle_timer
    with ac_cycle_lock:
        spec = ac_cycle_spec
    if spec is None:
        return
    on_min, off_min = spec
    if phase == "on":
        send_ac("00F1FFFFFFFFFFFF")  # Cool ON
        send_ac("00DF64FFFFFFFFFF")  # Fan LOW
        if mqtt_client_ref:
            mqtt_client_ref.publish("van/status/ac/mode", "cool", retain=True)
            mqtt_client_ref.publish("van/status/ac/fan", "low", retain=True)
        nxt, secs = "off", on_min * 60
    else:
        send_ac("00C0FFFFFFFFFFFF")  # System OFF
        if mqtt_client_ref:
            mqtt_client_ref.publish("van/status/ac/mode", "off", retain=True)
        nxt, secs = "on", off_min * 60
    with ac_cycle_lock:
        if ac_cycle_spec is None:
            return
        ac_cycle_timer = threading.Timer(secs, _ac_cycle_step, args=(nxt,))
        ac_cycle_timer.daemon = True
        ac_cycle_timer.start()

def _start_ac_cycle(on_min, off_min):
    global ac_cycle_spec
    _clear_ac_cycle(publish_status=False)
    with ac_cycle_lock:
        ac_cycle_spec = (on_min, off_min)
    if mqtt_client_ref:
        mqtt_client_ref.publish("van/status/ac/cycle", f"{on_min}/{off_min}", retain=True)
    print(f"AC cycle started: {on_min} min on / {off_min} min off")
    _ac_cycle_step("on")   # begin with an on-phase immediately

def cansend(data):
    frame = f"19FEDB{SA}#{data}"
    print(f"cansend {CAN_IFACE} {frame}")
    subprocess.run(["cansend", CAN_IFACE, frame])

def send_can(instance, payload):
    if payload in ("off", "0"):
        cansend(f"{instance}FF0006FF00FFFF")
    elif payload == "on":
        cansend(f"{instance}FFFA05FF00FFFF")
    elif payload.isdigit():
        pct = max(1, min(100, int(payload)))
        cansend(f"{instance}FF{round(pct * 2.0):02X}00FF00FFFF")

def send_motor(direction, payload):
    motor = MOTORS.get(direction)
    if not motor:
        print(f"Unknown motor: {direction}")
        return
    on_inst, off_inst = motor["on_inst"], motor["off_inst"]
    if payload == "on":
        cansend(f"{off_inst}FF0003FF00FFFF")
        cansend(f"{on_inst}FFC8010200FFFF")
    elif payload == "off":
        cansend(f"{on_inst}FF0003FF00FFFF")
        cansend(f"{off_inst}FF0003FF00FFFF")

def send_ac(data):
    """Send a Firefly thermostat command (PGN 0x1FEF9) from SA=0x44."""
    frame = f"{AC_CAN_ID}#{data}"
    print(f"cansend {CAN_IFACE} {frame}")
    subprocess.run(["cansend", CAN_IFACE, frame])

def handle_ac(key, payload):
    """
    Thermostat command bytes decoded from candump of LCD (SA=0x9F):
      Byte 0: instance (always 0x00)
      Byte 1: mode/fan control byte
              0xF1 = Cool ON, 0xC0 = Off
              0xDF = fan speed command (byte 2 sets level)
      Byte 2: fan level (0xC8=high, 0x64=low, 0x00=auto)
      Byte 5: setpoint step (0xF9=step down; 0xFA=step up, hypothesized)
      All other bytes: 0xFF (don't-care)
    """
    global ac_timer
    if key == "mode":
        # A manual mode change means the user is taking control — stop any duty cycle.
        _clear_ac_cycle()
        if payload == "cool":
            send_ac("00F1FFFFFFFFFFFF")  # Cool ON
        elif payload == "off":
            send_ac("00C0FFFFFFFFFFFF")  # System OFF
            # Manually turning the AC off cancels any pending sleep timer.
            _clear_ac_timer()
            if mqtt_client_ref:
                mqtt_client_ref.publish("van/status/ac/timer", "", retain=True)
    elif key == "fan":
        if payload == "high":
            send_ac("00D5C8FFFFFFFFFF")  # Fan HIGH (0xC8 = 100%)
        elif payload == "low":
            send_ac("00DF64FFFFFFFFFF")  # Fan LOW  (0x64 = 50%)
        elif payload == "auto":
            send_ac("00CFFFFFFFFFFFFF")  # Fan AUTO — confirmed from candump of LCD (SA=0x9F)
    elif key == "setpoint":
        if payload == "up":
            # NOTE: 0xFA is hypothesized for +1°F step; test and adjust if needed
            send_ac("00FFFFFFFFFAFFFF")
        elif payload == "down":
            send_ac("00FFFFFFFFF9FFFF")  # Confirmed step -1°F
    elif key == "timer":
        # payload = minutes until AC auto-off ("0"/"off" cancels)
        try:
            minutes = int(float(payload))
        except ValueError:
            minutes = 0
        _clear_ac_timer()
        if minutes > 0:
            secs   = minutes * 60
            end_ms = int((time.time() + secs) * 1000)
            with ac_timer_lock:
                ac_timer = threading.Timer(secs, _fire_ac_timer)
                ac_timer.daemon = True
                ac_timer.start()
            print(f"AC sleep timer set: {minutes} min")
            if mqtt_client_ref:
                mqtt_client_ref.publish("van/status/ac/timer", str(end_ms), retain=True)
        else:
            print("AC sleep timer cancelled")
            if mqtt_client_ref:
                mqtt_client_ref.publish("van/status/ac/timer", "", retain=True)
    elif key == "cycle":
        # payload = "ON/OFF" minutes (e.g. "30/20"), or "off"/"0" to stop.
        if payload in ("off", "0", ""):
            _clear_ac_cycle()
            print("AC cycle cancelled")
        else:
            try:
                on_s, off_s = payload.split("/")
                on_min, off_min = int(on_s), int(off_s)
            except (ValueError, AttributeError):
                on_min = off_min = 0
            if on_min > 0 and off_min > 0:
                _start_ac_cycle(on_min, off_min)
            else:
                print(f"AC cycle: bad spec '{payload}' — ignored")

def get_current_upstream():
    """Detect which upstream Wi-Fi connection wlan0 is using via nmcli."""
    try:
        result = subprocess.run(
            ["nmcli", "-g", "DEVICE,CONNECTION", "device", "status"],
            capture_output=True, text=True, timeout=5
        )
        for line in result.stdout.splitlines():
            parts = line.split(":")
            if len(parts) >= 2 and parts[0].strip() == "wlan0":
                conn = parts[1].strip()
                if conn == "preconfigured":
                    return "tmobile"
                elif conn == "wifi-blaster":
                    return "starlink"
                else:
                    return "unknown"
    except Exception as e:
        print(f"get_current_upstream error: {e}")
    return "unknown"

def handle_network(key, payload):
    """Handle network commands. Upstream switching is owned by starlink-bridge.py;
    this handler only triggers speed tests."""
    if key == "speedtest":
        lite = (payload == "lite")   # small, low-data test (driving checks)
        t = threading.Thread(target=run_speedtest, args=(lite,), daemon=True)
        t.start()
    # upstream switching is handled by starlink-bridge.py via van/network/upstream

def run_speedtest(lite=False):
    """Manual trigger — run the SAME Ookla-based script the periodic timer uses
    (run-speedtest.py). It handles its own MQTT publish (running + result).
    `lite=True` runs the small low-data variant (--lite) used while driving."""
    try:
        cmd = ["python3", "/home/sgordon1024/run-speedtest.py"]
        if lite:
            cmd.append("--lite")
        subprocess.run(cmd, timeout=180)
    except Exception as e:
        print(f"run_speedtest error: {e}")

def can_listener(mqtt_client):
    """
    Listen on CAN bus for status frames and publish to MQTT.

    Monitored CAN IDs:
      0x19FEDA9B  G12 DC_DIMMER_STATUS — light/output levels
      0x19FFCAE1  THERMOSTAT_STATUS_1 from SA=0xE1 — byte[1] = active setpoint °F
                  (only valid when AC is running; 0x00 when off)
      0x19FFE29B  G12 thermostat status — byte[1]=mode, byte[2]=fan,
                  bytes[3-4]=ambient temp K×32 little-endian (confirmed)
      0x19FEA3E1  TEMPERATURE_STATUS from SA=0xE1 — inverter heat-sink, ignored
    """
    s = socket.socket(socket.AF_CAN, socket.SOCK_RAW, socket.CAN_RAW)
    s.bind((CAN_IFACE,))
    last_light = {}
    last_ac    = {}

    while True:
        try:
            frame      = s.recv(16)
            can_id_raw = struct.unpack_from('<I', frame, 0)[0]
            actual_id  = can_id_raw & 0x1FFFFFFF
            data       = frame[8:16]

            # ── G12 light / output status ──────────────────────────────
            if actual_id == 0x19FEDA9B:
                instance_hex = f"{data[0]:02X}"
                level        = data[2]   # 0x00-0xC8 = 0-100%
                name = INSTANCE_TO_LIGHT.get(instance_hex)
                if not name:
                    continue
                pct    = min(100, round(level / 2.0))
                status = "off" if pct == 0 else str(pct)
                if last_light.get(name) != status:
                    last_light[name] = status
                    mqtt_client.publish(f"van/status/light/{name}", status, retain=True)
                    print(f"G12 light → {name}: {status}")

            # ── Thermostat setpoint (SA=0xE1) ──────────────────────────
            # Byte[1] = cool setpoint directly in °F (proprietary Firefly encoding)
            # Only valid when AC is actively running; byte[1]=0x00 when off.
            elif actual_id == 0x19FFCAE1:
                setpoint_f = data[1]
                if not (55 <= setpoint_f <= 95):
                    continue   # out of range means AC is off or data is invalid
                status = str(setpoint_f)
                if last_ac.get("setpoint") != status:
                    last_ac["setpoint"] = status
                    mqtt_client.publish("van/status/ac/setpoint", status, retain=True)
                    print(f"AC setpoint → {setpoint_f}°F")

            # ── G12 ambient temperature (proprietary PGN 0x1FF9C) ────────
            # Bytes[1-2]: ambient temp (K×32, little-endian) — confirmed
            elif actual_id == 0x19FF9C9B:
                raw_t  = data[1] | (data[2] << 8)
                temp_f = int((raw_t / 32.0 - 273.15) * 9.0 / 5.0 + 32)
                if 50 <= temp_f <= 110:
                    tstr = str(temp_f)
                    if last_ac.get("temp") != tstr:
                        last_ac["temp"] = tstr
                        mqtt_client.publish("van/status/ac/temp", tstr, retain=True)
                        print(f"AC temp → {temp_f}°F")

            # ── G12 thermostat mode / fan / setpoint ──────────────────
            # Byte[1]: 0x00=off, 0x01=cool (bit 0)
            # Byte[2]: 0x00=auto, 0x64=low, 0xC8=high
            # Bytes[3-4]: cool setpoint (K×32, little-endian)
            # Bytes[5-6]: heat setpoint (K×32, little-endian) — same as cool, not used
            elif actual_id == 0x19FFE29B:
                mode_byte = data[1]
                fan_byte  = data[2]
                mode = "cool" if (mode_byte & 0x01) else "off"
                # When AC is off the Firefly LCD shows "Auto" regardless of stored fan speed
                if mode == "off":
                    fan = "auto"
                elif fan_byte == 0x64: fan = "low"
                elif fan_byte == 0xC8: fan = "high"
                else:                  fan = "auto"
                if last_ac.get("mode") != mode:
                    last_ac["mode"] = mode
                    mqtt_client.publish("van/status/ac/mode", mode, retain=True)
                    print(f"AC mode → {mode}")
                if last_ac.get("fan") != fan:
                    last_ac["fan"] = fan
                    mqtt_client.publish("van/status/ac/fan", fan, retain=True)
                    print(f"AC fan → {fan}")
                # bytes[3-4] = cool setpoint (K×32, little-endian)
                raw_sp = data[3] | (data[4] << 8)
                sp_f   = round((raw_sp / 32.0 - 273.15) * 9.0 / 5.0 + 32)
                if 55 <= sp_f <= 95:
                    sstr = str(sp_f)
                    if last_ac.get("setpoint") != sstr:
                        last_ac["setpoint"] = sstr
                        mqtt_client.publish("van/status/ac/setpoint", sstr, retain=True)
                        print(f"AC setpoint → {sp_f}°F")

            # ── MultiPlus inverter temperature (SA=0xE1) ───────────────
            # Bytes[2-3] K×32 little-endian — this is heat-sink temp, NOT
            # ambient room temp.  We decode it but don't publish to MQTT.
            elif actual_id == 0x19FEA3E1:
                pass   # inverter heat-sink; ambient temp is in 19FFE29B bytes[3-4]

        except Exception as e:
            print(f"CAN listener error: {e}")

def on_connect(client, userdata, flags, rc):
    global mqtt_client_ref
    mqtt_client_ref = client
    print(f"Connected to MQTT (rc={rc})")
    client.subscribe("van/light/+")
    client.subscribe("van/motor/+")
    client.subscribe("van/ac/+")
    client.subscribe("van/network/speedtest")  # only speedtest; upstream owned by starlink-bridge
    print("Subscribed to van/light/+, van/motor/+, van/ac/+, van/network/speedtest")
    # A restart loses any in-memory sleep timer / cycle — clear their retained status so
    # the dashboard doesn't show a countdown or active cycle that will never fire.
    client.publish("van/status/ac/timer", "", retain=True)
    client.publish("van/status/ac/cycle", "", retain=True)
    subprocess.run(["cansend", CAN_IFACE, f"18EEFF{SA}#0000000000008000"])

    t = threading.Thread(target=can_listener, args=(client,), daemon=True)
    t.start()

def on_message(client, userdata, msg):
    topic   = msg.topic
    payload = msg.payload.decode().strip().lower()

    # Direct speedtest trigger (subscribed as van/network/speedtest)
    if topic == "van/network/speedtest":
        handle_network("speedtest", payload)
        return

    parts = topic.split("/")
    if len(parts) != 3:
        return
    category, name = parts[1], parts[2]

    if category == "light":
        if name == "tank-heater":
            print(f"light/tank-heater -> {payload}")
            for inst in TANK_HEATER_INSTANCES:
                send_can(inst, payload)
            return
        instance = LIGHTS.get(name)
        if not instance:
            print(f"Unknown light: {name}")
            return
        print(f"light/{name} -> {payload}")
        send_can(instance, payload)
    elif category == "motor":
        print(f"motor/{name} -> {payload}")
        send_motor(name, payload)
    elif category == "ac":
        print(f"ac/{name} -> {payload}")
        handle_ac(name, payload)

client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION1)
client.on_connect = on_connect
client.on_message = on_message
client.connect(MQTT_HOST, MQTT_PORT, 60)
client.loop_forever()
