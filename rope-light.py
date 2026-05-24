#!/usr/bin/env python3
import asyncio, threading, colorsys
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

CYCLE_SENTINEL   = b'__CYCLE__'
CANDLE_SENTINEL  = b'__CANDLE__'
BREATHE_SENTINEL = b'__BREATHE__'
AURORA_SENTINEL  = b'__AURORA__'
STROBE_SENTINEL  = b'__STROBE__'

# Mutable state shared between threads (GIL makes simple assignments safe)
brightness  = 1.0   # 0.0–1.0
cycle_speed = 2.0   # hue degrees per second (default speed 5/10 ≈ 3 min full cycle)
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
    """Smoothly rotate through the full HSV hue wheel.

    Writes to BLE once per second — stable for the controller and gives
    imperceptible per-step changes at any normal cycle_speed setting.
    cycle_speed = hue degrees advanced per second.
      speed 10 → 5°/s  → ~72s full cycle
      speed 5  → 2°/s  → ~3 min full cycle  (default)
      speed 1  → 0.4°/s → ~15 min full cycle
    """
    STEP_INTERVAL = 1.0   # 1 Hz — BLE-stable, imperceptible per step
    hue = 0.0
    while True:
        r, g, b = colorsys.hsv_to_rgb(hue / 360.0, 1.0, 1.0)
        # Byte order is B-R-G-W; apply brightness here
        cmd = bytes([0x56,
                     int(b * 255 * brightness),
                     int(r * 255 * brightness),
                     int(g * 255 * brightness),
                     0x00, 0xf0, 0xaa])
        await client.write_gatt_char(CHAR_UUID, cmd)
        await asyncio.sleep(STEP_INTERVAL)
        hue = (hue + cycle_speed * STEP_INTERVAL) % 360.0

async def candle_effect(client):
    """Warm amber candlelight flicker.
    cycle_speed controls turbulence: low = gentle, high = drafty/gusty."""
    import random, math
    base       = 0.85   # slowly drifting base brightness
    gust       = 1.0    # multiplier that dips on gusts
    next_gust  = random.uniform(4, 12)

    while True:
        dt = random.uniform(0.05, 0.10)   # irregular timing adds realism

        # Slowly drift base (mean-revert toward 0.85)
        base += random.gauss(0, 0.025)
        base  = 0.85 + 0.55 * (base - 0.85)
        base  = max(0.50, min(1.0, base))

        # Occasional gust — frequency and depth scale with cycle_speed
        next_gust -= dt
        gust_prob  = 0.3 + cycle_speed * 0.07   # more gusts at high speed
        gust_depth = 0.2 + cycle_speed * 0.06   # deeper dips at high speed
        if next_gust <= 0:
            gust       = random.uniform(max(0.15, 1.0 - gust_depth), 0.65)
            next_gust  = random.uniform(max(1.5, 8 - cycle_speed * 0.6),
                                        max(3.0, 18 - cycle_speed * 1.5))
        else:
            gust = min(1.0, gust + random.uniform(0.03, 0.09))

        level = base * gust
        level = max(0.12, min(1.0, level))

        # Warm amber: full red, variable green (more G = yellower, less G = orange)
        r = int(0xff * level * brightness)
        g = int(random.uniform(0.24, 0.50) * 0xff * level * brightness)
        b = 0   # no blue in a candle flame
        await client.write_gatt_char(CHAR_UUID, bytes([0x56, b, r, g, 0x00, 0xf0, 0xaa]))
        await asyncio.sleep(dt)


async def breathe_effect(client):
    """Slow sine-wave pulse on the current color. ~6 second cycle."""
    import math
    t = 0.0
    while True:
        level = 0.54 + 0.46 * math.sin(t - math.pi / 2)   # 0.08 → 1.0
        color = last_color if last_color is not None else COLORS['white']
        if len(color) == 7 and color[0] == 0x56:
            cmd = bytes([0x56,
                         int(color[1] * level * brightness),
                         int(color[2] * level * brightness),
                         int(color[3] * level * brightness),
                         int(color[4] * level * brightness),
                         0xf0, 0xaa])
        else:
            v   = int(0xff * level * brightness)
            cmd = bytes([0x56, v, v, v, 0x00, 0xf0, 0xaa])
        await client.write_gatt_char(CHAR_UUID, cmd)
        t = (t + 0.08) % (2 * math.pi)   # full cycle ≈ 6.3 s at ~12 Hz
        await asyncio.sleep(0.08)


async def aurora_effect(client):
    """Dreamy slow sweep through northern-lights colors (green → teal → blue → purple)."""
    import math, colorsys
    t = 0.0
    while True:
        # Two overlapping sine waves for organic, non-repeating motion
        hue = 200 + 75 * math.sin(t) + 18 * math.sin(t * 2.1 + 1.2)
        sat = 0.72 + 0.18 * math.sin(t * 0.6 + 0.9)
        val = (0.55 + 0.28 * math.sin(t * 0.4 + 2.1)) * brightness
        r, g, b = colorsys.hsv_to_rgb(hue / 360.0, sat, val)
        cmd = bytes([0x56,
                     int(b * 255),
                     int(r * 255),
                     int(g * 255),
                     0x00, 0xf0, 0xaa])
        await client.write_gatt_char(CHAR_UUID, cmd)
        t += 0.05
        await asyncio.sleep(0.18)   # ~6 Hz — slow and dreamy, full hue sweep ≈ 25 s


async def strobe_effect(client):
    """On/off flash. cycle_speed 1–10 maps to ~1–12 Hz."""
    while True:
        hz          = 1.0 + (cycle_speed - 1) * 11.0 / 9.0
        half_period = max(0.04, 0.5 / hz)
        color = last_color if last_color is not None else COLORS['white']
        if len(color) == 7 and color[0] == 0x56:
            on_cmd = bytes([0x56,
                            int(color[1] * brightness),
                            int(color[2] * brightness),
                            int(color[3] * brightness),
                            int(color[4] * brightness),
                            0xf0, 0xaa])
        else:
            v      = int(0xff * brightness)
            on_cmd = bytes([0x56, v, v, v, 0x00, 0xf0, 0xaa])
        off_cmd = bytes([0x56, 0x00, 0x00, 0x00, 0x00, 0xf0, 0xaa])
        await client.write_gatt_char(CHAR_UUID, on_cmd)
        await asyncio.sleep(half_period)
        await client.write_gatt_char(CHAR_UUID, off_cmd)
        await asyncio.sleep(half_period)


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
                        if   data == CYCLE_SENTINEL:
                            cycle_task = asyncio.create_task(color_cycle(client))
                        elif data == CANDLE_SENTINEL:
                            cycle_task = asyncio.create_task(candle_effect(client))
                        elif data == BREATHE_SENTINEL:
                            cycle_task = asyncio.create_task(breathe_effect(client))
                        elif data == AURORA_SENTINEL:
                            cycle_task = asyncio.create_task(aurora_effect(client))
                        elif data == STROBE_SENTINEL:
                            cycle_task = asyncio.create_task(strobe_effect(client))
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
        if payload == "on":
            loop.call_soon_threadsafe(queue.put_nowait, CMD_ON)
            color_to_send = last_color if last_color is not None else COLORS['white']
            loop.call_soon_threadsafe(queue.put_nowait, color_to_send)
        else:
            loop.call_soon_threadsafe(queue.put_nowait, CMD_OFF)

    elif topic == "van/rope-light/color" and payload in COLORS:
        loop.call_soon_threadsafe(queue.put_nowait, CMD_ON)
        loop.call_soon_threadsafe(queue.put_nowait, COLORS[payload])

    elif topic == "van/rope-light/effect":
        _sentinels = {
            'cycle':   CYCLE_SENTINEL,
            'candle':  CANDLE_SENTINEL,
            'breathe': BREATHE_SENTINEL,
            'aurora':  AURORA_SENTINEL,
            'strobe':  STROBE_SENTINEL,
        }
        if payload in _sentinels:
            loop.call_soon_threadsafe(queue.put_nowait, CMD_ON)
            loop.call_soon_threadsafe(queue.put_nowait, _sentinels[payload])

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
            # Map 1–10 → 0.4–5.0 hue degrees per second
            # val=1: 0.4°/s (~15 min cycle)  val=5: 2°/s (~3 min)  val=10: 5°/s (~72s)
            cycle_speed = round(0.4 + (val - 1) * 0.511, 2)
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
