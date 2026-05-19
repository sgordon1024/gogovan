#!/usr/bin/env python3
"""
Full BLE notification sweep — logs every byte for every prefix.
Resets to solid-red between tests so a working animation command
won't corrupt subsequent state queries.
"""
import asyncio
from bleak import BleakClient, BleakScanner
import subprocess

MAC        = "92:18:11:00:F7:24"
W_UUID     = "0000ffd9-0000-1000-8000-00805f9b34fb"
N_UUID     = "0000ffd4-0000-1000-8000-00805f9b34fb"

QUERY      = bytes([0xef, 0x01, 0x77])
CMD_ON     = bytes([0xcc, 0x23, 0x33])
SOLID_RED  = bytes([0x56, 0x00, 0xff, 0x00, 0x00, 0xf0, 0xaa])  # red

notifs = []

def on_notify(sender, data):
    notifs.append(bytes(data))   # always safe — no indexing here

async def write(cl, data):
    await cl.write_gatt_char(W_UUID, data, response=False)

async def reset(cl):
    """Return to a known solid-red state."""
    await write(cl, CMD_ON)
    await asyncio.sleep(0.15)
    await write(cl, SOLID_RED)
    await asyncio.sleep(0.25)

async def query_mode(cl):
    """Send state query; return mode byte or None."""
    notifs.clear()
    await write(cl, QUERY)
    await asyncio.sleep(0.5)
    for n in notifs:
        if len(n) >= 5 and n[0] == 0x66:
            return n[4]
    return None

async def main():
    subprocess.run(["bluetoothctl", "remove", MAC], capture_output=True)
    dev = await BleakScanner.find_device_by_address(MAC, timeout=15.0)
    if not dev:
        print("Device not found!"); return

    async with BleakClient(dev) as cl:
        await cl.start_notify(N_UUID, on_notify)
        print("Connected + notify enabled")
        await reset(cl)

        skip = {0x56, 0xcc, 0xef}
        found_animation = []

        print("\n=== PHASE 1: full prefix sweep [prefix, 0x02, 0x0e, 0x44] ===\n")

        for p in range(256):
            if p in skip:
                continue

            cmd = bytes([p, 0x02, 0x0e, 0x44])
            notifs.clear()
            try:
                await write(cl, cmd)
            except Exception as e:
                print(f"[0x{p:02x}] WRITE ERROR: {e}")
                await reset(cl)
                continue

            await asyncio.sleep(0.4)
            cmd_notifs = list(notifs)       # notifications triggered by the command itself

            # Now query state
            mode = await query_mode(cl)

            changed  = mode is not None and mode != 0x32
            has_cmd_notif = len(cmd_notifs) > 0

            # Print anything interesting, or every 32 steps so we can see progress
            if changed or has_cmd_notif or p % 32 == 0:
                print(f"[0x{p:02x}] immediate-notifs={[n.hex() for n in cmd_notifs]}  "
                      f"mode={hex(mode) if mode is not None else 'no-response'}")
                if changed:
                    print(f"  *** MODE CHANGED TO {hex(mode)} — ANIMATION COMMAND FOUND! ***")
                    found_animation.append((p, cmd, mode))

            await reset(cl)     # back to solid red for next iteration

        # -------------------------------------------------------
        print("\n=== PHASE 2: targeted 38-family sweep ===\n")
        # Try all payloads for the 0x38 prefix since it's our strongest lead
        for second in range(256):
            cmd = bytes([0x38, second, 0x02, 0x0e])
            notifs.clear()
            try:
                await write(cl, cmd)
            except Exception as e:
                print(f"[38 {second:02x}] WRITE ERROR: {e}")
                await reset(cl)
                continue
            await asyncio.sleep(0.6)        # longer wait — 38 commands seem slow
            cmd_notifs = list(notifs)

            mode = await query_mode(cl)

            changed       = mode is not None and mode != 0x32
            no_state_resp = mode is None
            has_cmd_notif = len(cmd_notifs) > 0

            if changed or no_state_resp or has_cmd_notif or second % 32 == 0:
                print(f"[38 {second:02x}] immediate-notifs={[n.hex() for n in cmd_notifs]}  "
                      f"mode={hex(mode) if mode is not None else 'NO STATE RESPONSE'}")
                if changed:
                    print(f"  *** MODE CHANGED TO {hex(mode)} ***")
                    found_animation.append((0x38, bytes([0x38, second, 0x02, 0x0e]), mode))

            await reset(cl)

        # -------------------------------------------------------
        print("\n=== PHASE 3: 38-long-form with extended wait ===\n")
        # The 10-byte command that previously returned no state response
        long_38 = bytes([0x38, 0x01, 0x02, 0x0e, 0xff, 0x00, 0x00, 0x00, 0xf0, 0xaa])
        for wait_sec in [2, 5, 10]:
            notifs.clear()
            await write(cl, long_38)
            print(f"  Sent 38-long-form, waiting {wait_sec}s...")
            await asyncio.sleep(wait_sec)
            during = list(notifs)
            mode = await query_mode(cl)
            print(f"  wait={wait_sec}s  during-notifs={[n.hex() for n in during]}  "
                  f"mode={hex(mode) if mode is not None else 'NO RESPONSE'}")
            if mode and mode != 0x32:
                print(f"  *** ANIMATION at wait={wait_sec}s! ***")
                found_animation.append(('38-long', long_38, mode))
            await reset(cl)

        # -------------------------------------------------------
        print("\n=== SUMMARY ===")
        if found_animation:
            for item in found_animation:
                print(f"  ANIMATION CMD: prefix={hex(item[0])} bytes={item[1].hex()} mode={hex(item[2])}")
        else:
            print("  No animation command found in this sweep.")

        await cl.stop_notify(N_UUID)
        print("Done.")

asyncio.run(main())
