#!/usr/bin/env python3
import socket
import struct
import subprocess
import threading
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
    if key == "mode":
        if payload == "cool":
            send_ac("00F1FFFFFFFFFFFF")  # Cool ON
        elif payload == "off":
            send_ac("00C0FFFFFFFFFFFF")  # System OFF
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
    print(f"Connected to MQTT (rc={rc})")
    client.subscribe("van/light/+")
    client.subscribe("van/motor/+")
    client.subscribe("van/ac/+")
    print("Subscribed to van/light/+, van/motor/+, van/ac/+")
    subprocess.run(["cansend", CAN_IFACE, f"18EEFF{SA}#0000000000008000"])

    t = threading.Thread(target=can_listener, args=(client,), daemon=True)
    t.start()

def on_message(client, userdata, msg):
    parts = msg.topic.split("/")
    if len(parts) != 3:
        return
    category, name = parts[1], parts[2]
    payload = msg.payload.decode().strip().lower()

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
