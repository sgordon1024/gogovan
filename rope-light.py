#!/usr/bin/env python3
import asyncio, threading
import paho.mqtt.client as mqtt
from bleak import BleakClient, BleakScanner
import subprocess

MAC       = "92:18:11:00:F7:24"
CHAR_UUID = "0000ffd9-0000-1000-8000-00805f9b34fb"
MQTT_HOST = "localhost"

CMD_ON  = bytes([0xcc, 0x23, 0x33])
CMD_OFF = bytes([0xcc, 0x24, 0x33])

def c(b, r, g): return bytes([0x56, b, r, g, 0x00, 0xf0, 0xaa])

COLORS = {
    'red':    c(0x00, 0xff, 0x00),
    'orange': c(0x00, 0xff, 0x66),
    'amber':  c(0x00, 0xff, 0x33),
    'yellow': c(0x00, 0xff, 0xff),
    'lime':   c(0x00, 0x55, 0xff),
    'green':  c(0x00, 0x00, 0xff),
    'teal':   c(0x88, 0x00, 0xcc),
    'cyan':   c(0xff, 0x00, 0xff),
    'sky':    c(0xff, 0x00, 0x55),
    'blue':   c(0xff, 0x00, 0x00),
    'navy':   c(0x55, 0x00, 0x00),
    'purple': c(0xff, 0xff, 0x00),
    'pink':   c(0xff, 0xdd, 0x00),
    'white':  c(0xff, 0xff, 0xff),
}

CYCLE_COLORS = [
    c(0x00, 0xff, 0x00),  # red
    c(0x00, 0xff, 0x44),  # orange
    c(0x00, 0xff, 0xff),  # yellow
    c(0x00, 0x00, 0xff),  # green
    c(0xff, 0x00, 0xff),  # cyan
    c(0xff, 0x00, 0x00),  # blue
    c(0xff, 0xff, 0x00),  # purple
    c(0xff, 0xdd, 0x00),  # pink
]

CYCLE_SENTINEL = b'__CYCLE__'

# Mutable state shared between threads (GIL makes simple assignments safe)
brightness  = 1.0   # 0.0–1.0
cycle_speed = 0.5   # seconds per step (default speed 5/10)
last_color  = None  # last solid color bytes (pre-brightness), for re-send on brightness change

loop  = asyncio.new_event_loop()
queue = asyncio.Queue()

def dim(data):
    """Apply brightness to a 56-format color command."""
    if len(data) == 7 and data[0] == 0x56:
        return bytes([0x56,
                      int(data[1] * brightness),
                      int(data[2] * brightness),
                      int(data[3] * brightness),
                      int(data[4] * brightness),
                      0xf0, 0xaa])
    return data

async def color_cycle(client):
    i = 0
    while True:
        await client.write_gatt_char(CHAR_UUID, dim(CYCLE_COLORS[i % len(CYCLE_COLORS)]))
        i += 1
        await asyncio.sleep(cycle_speed)

async def ble_loop():
    global last_color
    cycle_task = None
    while True:
        try:
            subprocess.run(["bluetoothctl", "remove", MAC], capture_output=True)
            device = await BleakScanner.find_device_by_address(MAC, timeout=10.0)
            if device is None:
                print("BLE device not found, retrying...")
                await asyncio.sleep(5)
                continue
            async with BleakClient(device) as client:
                print("BLE connected")
                while client.is_connected:
                    try:
                        data = await asyncio.wait_for(queue.get(), timeout=5.0)
                        # Any new command cancels an active cycle
                        if cycle_task and not cycle_task.done():
                            cycle_task.cancel()
                            cycle_task = None
                        if data == CYCLE_SENTINEL:
                            cycle_task = asyncio.create_task(color_cycle(client))
                        else:
                            if len(data) == 7 and data[0] == 0x56:
                                last_color = data  # track for brightness re-send
                            await client.write_gatt_char(CHAR_UUID, dim(data) if data[0] == 0x56 else data)
                    except asyncio.TimeoutError:
                        pass
        except Exception as e:
            print(f"BLE error: {e}, retrying in 5s...")
            if cycle_task and not cycle_task.done():
                cycle_task.cancel()
                cycle_task = None
            await asyncio.sleep(5)

def on_message(mqttc, userdata, msg):
    global brightness, cycle_speed, last_color
    topic   = msg.topic
    payload = msg.payload.decode().strip().lower()

    if topic == "van/rope-light/power":
        loop.call_soon_threadsafe(queue.put_nowait, CMD_ON if payload == "on" else CMD_OFF)

    elif topic == "van/rope-light/color" and payload in COLORS:
        loop.call_soon_threadsafe(queue.put_nowait, CMD_ON)
        loop.call_soon_threadsafe(queue.put_nowait, COLORS[payload])

    elif topic == "van/rope-light/effect" and payload == "cycle":
        loop.call_soon_threadsafe(queue.put_nowait, CMD_ON)
        loop.call_soon_threadsafe(queue.put_nowait, CYCLE_SENTINEL)

    elif topic == "van/rope-light/brightness":
        try:
            val = max(1, min(100, int(payload)))
            brightness = val / 100.0
            # Re-send current solid color at new brightness (if not in cycle)
            if last_color:
                loop.call_soon_threadsafe(queue.put_nowait, last_color)
        except ValueError:
            pass

    elif topic == "van/rope-light/speed":
        try:
            val = max(1, min(10, int(payload)))
            # Map 1–10 → 1.5–0.1 seconds per step
            cycle_speed = round(1.5 / val, 2)
        except ValueError:
            pass

def mqtt_thread():
    mqttc = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
    mqttc.on_message = on_message
    mqttc.connect(MQTT_HOST, 1883)
    mqttc.subscribe("van/rope-light/#")
    mqttc.loop_forever()

threading.Thread(target=mqtt_thread, daemon=True).start()
loop.run_until_complete(ble_loop())
