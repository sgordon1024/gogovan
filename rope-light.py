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
brightness    = 1.0    # 0.0–1.0
cycle_speed   = 2.0    # hue degrees per second (default speed 5/10 ≈ 3 min full cycle)
last_color    = None   # last solid color bytes (pre-brightness), for re-send on brightness change
effect_active = False  # True when a software effect is running (effects read brightness directly)

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
    """Candlelight — visible glow in/out with deep orange/red palette.
    cycle_speed controls turbulence: low = gentle, high = gusty."""
    import random, math

    DT = 1 / 30   # 30 fps

    print("candle_effect started")

    # ── target values ───────────────────────────────────────────────────────
    tgt_base   = 0.85
    tgt_gust   = 1.0
    tgt_warmth = 0.10   # G/R: 0.04=deep red, 0.16=red-orange

    # ── smoothed current values ─────────────────────────────────────────────
    cur_base   = tgt_base
    cur_gust   = tgt_gust
    cur_warmth = tgt_warmth

    # ── micro-flicker (fast flutter on top) ─────────────────────────────────
    micro       = 0.0
    tgt_micro   = 0.0
    micro_count = 0
    micro_next  = random.randint(2, 4)   # every 100–200 ms at 20 fps

    # ── slow sine sway — the visible "breathing" glow ───────────────────────
    sway_t = random.uniform(0, 6.28)

    # ── gust scheduler ──────────────────────────────────────────────────────
    next_gust_in = random.uniform(0.3, 1.5)

    frame = 0

    while True:
        frame        += 1
        next_gust_in -= DT
        sway_t        = (sway_t + DT * 1.2) % (2 * math.pi)   # ~5-s sway

        # ── base drifts wide every ~0.13 s (every 4 frames @ 30 fps) ───────
        if frame % 4 == 0:
            tgt_base += random.gauss(0, 0.055)
            tgt_base  = 0.82 + 0.45 * (tgt_base - 0.82)
            tgt_base  = max(0.30, min(1.0, tgt_base))

        # ── gusts: deep, frequent ────────────────────────────────────────────
        gust_depth = 0.65 + cycle_speed * 0.07
        if next_gust_in <= 0:
            tgt_gust     = random.uniform(max(0.12, 1.0 - gust_depth), 0.60)
            next_gust_in = random.uniform(max(0.3, 1.5 - cycle_speed * 0.1),
                                          max(0.8, 3.5 - cycle_speed * 0.3))
        else:
            tgt_gust = min(1.0, tgt_gust + DT * 0.20)

        # ── warmth drifts in red → red-orange band every ~0.5 s ─────────────
        if frame % 15 == 0:
            tgt_warmth += random.gauss(0, 0.008)
            tgt_warmth  = max(0.04, min(0.16, tgt_warmth))

        # ── micro-flicker every 3–6 frames (100–200 ms) ─────────────────────
        micro_count += 1
        if micro_count >= micro_next:
            tgt_micro   = random.gauss(0, 0.08)
            micro_next  = random.randint(3, 6)
            micro_count = 0

        # ── exponential smoothing (α scaled for 30 fps, same time constants) ─
        cur_base   += (tgt_base   - cur_base)   * 0.036
        cur_warmth += (tgt_warmth - cur_warmth) * 0.020

        # Asymmetric gust: sharp dip, slow dreamy recovery
        if tgt_gust < cur_gust:
            cur_gust += (tgt_gust - cur_gust) * 0.28   # fast dip
        else:
            cur_gust += (tgt_gust - cur_gust) * 0.035  # ~0.9 s recovery

        micro += (tgt_micro - micro) * 0.30

        # ── compose level ─────────────────────────────────────────────────────
        # Sway amplitude ±18% gives clearly visible slow breathing
        sway  = 0.18 * math.sin(sway_t) + 0.06 * math.sin(sway_t * 1.7 + 0.8)
        level = max(0.08, min(1.0, cur_base * cur_gust + sway + micro))

        r = int(0xff * level * brightness)
        g = int(cur_warmth * 0xff * level * brightness)
        b = 0
        await client.write_gatt_char(CHAR_UUID, bytes([0x56, b, r, g, 0x00, 0xf0, 0xaa]))
        await asyncio.sleep(DT)


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
    global brightness, cycle_speed, last_color, effect_active
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
        effect_active = False
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
            effect_active = True
            loop.call_soon_threadsafe(queue.put_nowait, CMD_ON)
            loop.call_soon_threadsafe(queue.put_nowait, _sentinels[payload])

    elif topic == "van/rope-light/brightness":
        try:
            val = max(1, min(100, int(payload)))
            brightness = val / 100.0
            # Only re-send solid color if no effect is running —
            # effects read the brightness global directly each frame
            if last_color and not effect_active:
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
