# GoGoVan Dashboard — Project Context

## What This Is
A custom web dashboard for a **2024 Entegra Launch camper van (Mercedes Sprinter chassis)**, served from a Raspberry Pi 4 and accessible on the local Wi-Fi and remotely via Tailscale. It controls lights, awning, water pump, tank heater, and AC via the van's RV-C CAN bus, and displays live power/battery data from the Victron Cerbo GX.

---

## System Architecture

```
iPhone / Browser
     │  WebSocket :9001
     ▼
Pi mosquitto broker (YOUR_PI_IP :1883 / :9001)
     │  MQTT bridge
     ▼
Victron Cerbo GX (YOUR_CERBO_IP :1883)   ← Victron telemetry only (read)
     │
     └── CAN bus (VE.Can / can0, RV-C 250kbps) ← LISTEN ONLY on Cerbo

Pi CAN HAT (Waveshare 2-CH CAN HAT+)
     │  can1, 250kbps
     └── G12 CAN bus  ← Pi can READ and WRITE here
```

**Key insight:** The Cerbo's `can0` (VE.Can) is in listen-only mode — `cansend` runs without error but frames are never transmitted. All G12 control must go through the Pi's CAN HAT on `can1`.

---

## Hardware

| Device | Address | Notes |
|---|---|---|
| Raspberry Pi 4 | YOUR_PI_IP | Dashboard host, SSH: YOUR_PI_USER/YOUR_PI_PASSWORD |
| Victron Cerbo GX | YOUR_CERBO_IP | VRM Portal ID: YOUR_VRM_PORTAL_ID |
| Firefly G12 controller | SA=0x9B | Controls lights, HVAC, awning, pump, tank heater |
| G12 LCD ("Bed Wall") | SA=0x9F | Touchscreen panel, Bluetooth to VegaTouch Mira |
| Lithionics Battery | SA=0x46 | |
| MultiPlus-II inverter | SA=0xE1 | |
| SmartSolar MPPT | SA=0x24 | |

**Pi CAN HAT wiring:** Red=DC+, Black=DC−, White=CAN_H, Yellow=CAN_L into CAN_0 physical terminals. Physical CAN_0 = Linux `can1` (kernel assigns in reverse).

**Pi services (all auto-start on boot):**
- `can1-setup` — brings up can1 at 250kbps
- `can-bridge` — `can-bridge.py` MQTT↔CAN bridge
- `gogovan-web` — `python3 -m http.server 8080`

---

## Files

| File | Location | Purpose |
|---|---|---|
| `index.html` | Pi: `/home/YOUR_PI_USER/index.html` | Dashboard UI, served at :8080 |
| `can-bridge.py` | Pi: `/home/YOUR_PI_USER/can-bridge.py` | MQTT subscriber → CAN sender + CAN listener → MQTT publisher |
| `pi-setup/setup-speedtest.sh` | Dev only | Deploys `run-speedtest.py` + systemd timer to Pi; run once |
| `run-speedtest.py` | Pi: `/home/sgordon1024/run-speedtest.py` | Created by setup script; runs `speedtest-cli --json --secure`, publishes result to MQTT |

**Deploy command:**
```bash
sshpass -p "YOUR_PI_PASSWORD" scp index.html can-bridge.py YOUR_PI_USER@YOUR_PI_IP:/home/YOUR_PI_USER/
sshpass -p "YOUR_PI_PASSWORD" ssh YOUR_PI_USER@YOUR_PI_IP "echo 'YOUR_PI_PASSWORD' | sudo -S systemctl restart can-bridge"
```

---

## Remote Access
- Tailscale installed on Pi and iPhone
- Pi Tailscale IP: `YOUR_TAILSCALE_IP`
- MagicDNS: `YOUR_TAILSCALE_HOSTNAME`
- Dashboard URL (remote): `http://YOUR_TAILSCALE_HOSTNAME:8080`
- **Important:** Disable Tailscale key expiry on the Pi in admin panel to avoid 5-month re-auth

---

## RV-C CAN Bus Protocol

All control uses **DC_DIMMER_COMMAND_2 (PGN 0x1FEDB)**, not 0x1FEDA (which is status-only broadcasts from G12).

**Address claim (run on bridge startup):**
```
cansend can1 18EEFF44#0000000000008000
```

**Light/switch command format:**
```
19FEDB44#[INST]FF[LEVEL][CMD]FF00FFFF
```
- Turn ON:  `[INST]FFFA05FF00FFFF`  (cmd=05 ramp up, level=FA)
- Turn OFF: `[INST]FF0006FF00FFFF`  (cmd=06 ramp down, level=00)
- Set dim:  `[INST]FF[PCT*2]00FF00FFFF`  (cmd=00 set level, 0xC8=100%)

**Awning motor format:**
- Extend:  stop inst 04 first → start inst 03 (`03FFC8010200FFFF`)
- Retract: stop inst 03 first → start inst 04 (`04FFC8010200FFFF`)
- Stop: `[INST]FF0003FF00FFFF` (cmd=03)

---

## G12 Instance Map

| Instance (hex) | Instance (dec) | Output |
|---|---|---|
| 0x05–0x08 | 5–8 | Tank Heater (4 outputs, all switched together) |
| 0x15 | 21 | Awning Light |
| 0x16 | 22 | Kitchen OHC Lights |
| 0x17 | 23 | Step Light |
| 0x18 | 24 | Bed Lights |
| 0x19 | 25 | Cargo Lights |
| 0x20 | 32 | Main Ceiling Lights |
| 0x22 | 34 | Bunk Accent Lights |
| 0x23 | 35 | Bench OHC Lights |
| 0x2C | 44 | Water Pump |
| 0x03 | 3 | Awning Extend motor |
| 0x04 | 4 | Awning Retract motor |

**Tank heater note:** Instances 05–08 all activate together as one "tank heater" system (fresh/grey/black tanks + underbelly). Discovered via candump — all four broadcast simultaneously at 0xC8 (100%) when enabled.

---

## AC / Thermostat

### Command PGN
`19FEF944` (PGN 0x1FEF9, proprietary Firefly, SA=0x44)  
Discovered by sniffing the G12 LCD (SA=0x9F) controlling the thermostat.

| Command | Bytes |
|---|---|
| Cool ON | `00F1FFFFFFFFFFFF` |
| System OFF | `00C0FFFFFFFFFFFF` |
| Fan HIGH | `00D5C8FFFFFFFFFF` |
| Fan LOW | `00DF64FFFFFFFFFF` |
| Fan AUTO | `00CFFFFFFFFFFFFF` |
| Setpoint +1°F | `00FFFFFFFFFAFFFF` *(hypothesized)* |
| Setpoint −1°F | `00FFFFFFFFF9FFFF` *(confirmed)* |

### Status PGNs (all proprietary Firefly)

**`19FFE29B`** — G12 thermostat status (broadcasts continuously)
- byte[1]: mode — `0x00`=off, `0x01`=cool
- byte[2]: fan speed — `0x00`=auto, `0x64`=low, `0xC8`=high
- bytes[3–4]: cool setpoint, K×32 little-endian
- bytes[5–6]: heat setpoint (same value as cool, not used)

**`19FF9C9B`** — G12 ambient temperature (broadcasts continuously)
- bytes[1–2]: ambient temp, K×32 little-endian

**`19FFCAE1`** — THERMOSTAT_STATUS_1 from MultiPlus (SA=0xE1)
- byte[1]: active cool setpoint in °F — only valid when AC is actively cooling, 0x00 when off
- Used as secondary setpoint source (filtered to 55–95°F range)

### Temperature Encoding
```python
temp_f = int((raw / 32.0 - 273.15) * 9.0 / 5.0 + 32)
```
Use `int()` (truncate), **not** `round()` — the Firefly LCD truncates fractional degrees, and using `round()` causes a 1°F discrepancy.

### Fan / Mode Sync Decision
When AC mode is **off**, the Firefly LCD always displays fan as "Auto" regardless of the stored fan speed. The G12 still broadcasts the stored speed in byte[2] of `19FFE29B`. To keep the dashboard in sync with what the Firefly shows, the bridge publishes fan = "auto" whenever mode = "off".

---

## MQTT Topic Map

| Topic (subscribe) | Direction | Payload |
|---|---|---|
| `van/light/{name}` | Dashboard → Bridge | `on`, `off`, `1`–`100` |
| `van/motor/{name}` | Dashboard → Bridge | `on`, `off` |
| `van/ac/mode` | Dashboard → Bridge | `cool`, `off` |
| `van/ac/fan` | Dashboard → Bridge | `high`, `low`, `auto` |
| `van/ac/setpoint` | Dashboard → Bridge | `up`, `down` |

| Topic (publish, retained) | Direction | Payload |
|---|---|---|
| `van/status/light/{name}` | Bridge → Dashboard | `off`, `1`–`100` |
| `van/status/ac/mode` | Bridge → Dashboard | `cool`, `off` |
| `van/status/ac/fan` | Bridge → Dashboard | `high`, `low`, `auto` |
| `van/status/ac/setpoint` | Bridge → Dashboard | integer °F |
| `van/status/ac/temp` | Bridge → Dashboard | integer °F |

All status topics use `retain=True` so the dashboard gets current state immediately on page load (no waiting for the G12 to broadcast again).

### Internet / Speed Test topics

| Topic | Direction | Payload |
|---|---|---|
| `van/network/speedtest` | Dashboard → Bridge | `run` (triggers manual test) |
| `van/status/network/speedtest` | Bridge → Dashboard | JSON: `{download, upload, ping, server, upstream, timestamp, error}` |
| `van/status/network/speedtest/running` | Bridge → Dashboard | `true` / `false` |

`upstream` values: `tmobile` (NetworkManager connection `preconfigured`), `starlink` (connection `wifi-blaster`), `unknown`.

Speed test results are stored in **`localStorage` key `gogovan-speed-history`** as a JSON array. Each entry: `{ts, isoTs, upstream, down, up, ping, server, lat, lng}`. Entries older than 1 year are pruned on every save. At 48 auto-tests/day (systemd timer every 30 min) this retains ~17,500 entries/year.

---

## Key Decisions & Why

**Why Pi controls CAN instead of Cerbo:**  
Cerbo's `can0` (VE.Can) is in listen-only mode. `cansend` runs silently but nothing is transmitted. Discovered after Node-RED exec nodes appeared to work but G12 never responded.

**Why mosquitto runs on Pi (not just Cerbo):**  
Enables Tailscale remote access. The dashboard connects to `window.location.hostname:9001`, which works both locally (Pi IP) and remotely (Tailscale). Cerbo's MQTT isn't reachable over Tailscale. Pi mosquitto bridges Victron telemetry from Cerbo.

**Why `int()` not `round()` for temperature:**  
The Firefly LCD truncates fractional degrees. `round()` caused a 1°F discrepancy (e.g. 67.7°F → app showed 68°F, Firefly showed 67°F).

**Why `19FF9C9B` for ambient temp, not `19FFE29B`:**  
`19FFE29B` bytes[3–4] and bytes[5–6] are *both setpoints* (cool + heat) — they're identical and change with the arrows. Ambient temperature is on a separate proprietary frame `19FF9C9B` bytes[1–2]. Discovered by doing a broad candump and looking for the K×32 encoding of the known ambient temperature.

**Why tank heater controls 4 instances:**  
Instances 0x05–0x08 all activate simultaneously when the tank heater is switched on. They represent separate heating elements (fresh/grey/black/underbelly) but are controlled as a single system by the G12. A single button sends on/off to all four.

**Fan byte value for auto (0xCF in command, 0x00 in status):**  
The command byte for auto (`00CFFFFFFFFFFFFF`) was discovered by sniffing the Mira app via candump while pressing the auto button. Three other guesses failed first (0xDF, 0xDF+0x00, 0xD5). Status frame byte[2]=0x00 maps to auto.

**Why the speed test list paginates to 50 entries at a time:**  
Automatic tests run every 30 min, so a year of history is ~17,500 entries. Rendering all of them as DOM nodes at once would freeze the UI. The stats overlay loads 50 entries initially with a "Load more" button to append the next batch.

**Why the speed test chart shows daily averages instead of individual points:**  
Same scale problem — 17,500 SVG nodes in a polyline would make the chart unusably slow and visually unreadable. `aggregateDailyStats()` groups raw entries by day + carrier and plots the daily average, capping the chart at ~365 points regardless of how many tests were run.

**Why the offline banner uses `env(safe-area-inset-top)` instead of `top: 20px`:**  
The Dynamic Island on iPhone 14 Pro and later sits ~59px from the top, so a fixed `20px` offset placed the banner behind it. `env(safe-area-inset-top)` is a CSS variable the browser sets to the exact inset height for the current device (Dynamic Island, notch, or 0 on older models), so it works correctly on all iPhones.
