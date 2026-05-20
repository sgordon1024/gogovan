#!/usr/bin/env python3
"""
Targeted follow-up sweep:
  Phase A: bb [0x00..0xff] 0e 44  — full bb mode sweep (never tested all codes)
  Phase B: 61 [0x25..0x38] 0e 0f  — MagicHome standard animation format
  Phase C: 38 [4a..68] 02 0e with 3-second state query wait  — previous no-response zone
  Phase D: f2 / f3 full payload sweep
"""
import asyncio
from bleak import BleakClient, BleakScanner
import subprocess

MAC        = "92:18:11:00:F7:24"
W_UUID     = "0000ffd9-0000-1000-8000-00805f9b34fb"
N_UUID     = "0000ffd4-0000-1000-8000-00805f9b34fb"
QUERY      = bytes([0xef, 0x01, 0x77])
CMD_ON     = bytes([0xcc, 0x23, 0x33])
SOLID_RED  = bytes([0x56, 0x00, 0xff, 0x00, 0x00, 0xf0, 0xaa])

notifs = []
def on_notify(sender, data):
    notifs.append(bytes(data))

async def write(cl, data):
    await cl.write_gatt_char(W_UUID, data, response=False)

async def reset(cl):
    await write(cl, CMD_ON)
    await asyncio.sleep(0.15)
    await write(cl, SOLID_RED)
    await asyncio.sleep(0.25)

async def query_mode(cl, wait=0.5):
    notifs.clear()
    await write(cl, QUERY)
    await asyncio.sleep(wait)
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
        print("Connected.")
        await reset(cl)
        found = []

        # -------------------------------------------------------
        print("\n=== PHASE A: bb [xx] 0e 44  (all 256 mode codes) ===\n")
        for xx in range(256):
            cmd = bytes([0xbb, xx, 0x0e, 0x44])
            notifs.clear()
            await write(cl, cmd)
            await asyncio.sleep(0.4)
            cmd_notifs = list(notifs)

            mode = await query_mode(cl, wait=0.5)
            changed       = mode is not None and mode != 0x32
            no_resp       = mode is None
            has_notif     = len(cmd_notifs) > 0

            if changed or no_resp or has_notif or xx % 32 == 0:
                print(f"[bb {xx:02x}] notifs={[n.hex() for n in cmd_notifs]}  "
                      f"mode={hex(mode) if mode is not None else 'NO-RESP'}")
                if changed:
                    print(f"  *** MODE CHANGED: {hex(mode)} ***")
                    found.append(('bb', cmd, mode))

            await reset(cl)

        # -------------------------------------------------------
        print("\n=== PHASE B: 61 [mode] 0e 0f  (MagicHome animation format) ===\n")
        # Standard MagicHome built-in patterns use 61 [0x25..0x38] [speed] 0f
        # Try both 0f and 44 terminators, speeds 0x01 0x0e 0x1f
        for mode_b in list(range(0x25, 0x39)) + [0x01, 0x02, 0x05, 0x10, 0x20]:
            for term in [0x0f, 0x44]:
                cmd = bytes([0x61, mode_b, 0x0e, term])
                notifs.clear()
                await write(cl, cmd)
                await asyncio.sleep(0.4)
                cmd_notifs = list(notifs)

                mode = await query_mode(cl, wait=0.5)
                changed = mode is not None and mode != 0x32
                no_resp = mode is None
                has_notif = len(cmd_notifs) > 0

                if changed or no_resp or has_notif:
                    print(f"[61 {mode_b:02x} 0e {term:02x}] notifs={[n.hex() for n in cmd_notifs]}  "
                          f"mode={hex(mode) if mode is not None else 'NO-RESP'}")
                    if changed:
                        print(f"  *** MODE CHANGED: {hex(mode)} ***")
                        found.append(('61', cmd, mode))

                await reset(cl)
        print("  (done — no output = all mode=0x32)")

        # -------------------------------------------------------
        print("\n=== PHASE C: 38 [4a..68] 02 0e  with 3-second state query ===\n")
        for xx in range(0x4a, 0x69):
            cmd = bytes([0x38, xx, 0x02, 0x0e])
            notifs.clear()
            await write(cl, cmd)
            await asyncio.sleep(3.0)        # long wait — device may be busy animating
            during = list(notifs)

            mode = await query_mode(cl, wait=1.0)
            changed = mode is not None and mode != 0x32
            no_resp = mode is None

            print(f"[38 {xx:02x}] during={[n.hex() for n in during]}  "
                  f"mode={hex(mode) if mode is not None else 'NO-RESP'}")
            if changed:
                print(f"  *** MODE CHANGED: {hex(mode)} ***")
                found.append(('38', cmd, mode))

            await reset(cl)

        # -------------------------------------------------------
        print("\n=== PHASE D: f2 / f3  all second-bytes ===\n")
        for prefix in [0xf2, 0xf3]:
            for xx in range(256):
                cmd = bytes([prefix, xx, 0x0e, 0x44])
                notifs.clear()
                await write(cl, cmd)
                await asyncio.sleep(0.4)
                cmd_notifs = list(notifs)

                mode = await query_mode(cl, wait=0.5)
                changed       = mode is not None and mode != 0x32
                has_notif     = len(cmd_notifs) > 0
                no_resp       = mode is None

                if changed or has_notif or no_resp or xx % 64 == 0:
                    print(f"[{prefix:02x} {xx:02x}] notifs={[n.hex() for n in cmd_notifs]}  "
                          f"mode={hex(mode) if mode is not None else 'NO-RESP'}")
                    if changed:
                        print(f"  *** MODE CHANGED: {hex(mode)} ***")
                        found.append((f'{prefix:02x}', cmd, mode))

                await reset(cl)

        # -------------------------------------------------------
        print("\n=== SUMMARY ===")
        if found:
            for item in found:
                print(f"  ANIMATION: prefix={item[0]} bytes={item[1].hex()} mode={hex(item[2])}")
        else:
            print("  Nothing changed mode from 0x32 in this sweep.")

        await cl.stop_notify(N_UUID)
        print("Done.")

asyncio.run(main())
