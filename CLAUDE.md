# GoGoVan Dashboard — Project Context

## What This Is
A custom web dashboard for a **2024 Entegra Launch camper van (Mercedes Sprinter chassis)**, served from a Raspberry Pi 4 and accessible on the local Wi-Fi and remotely via Tailscale. It controls lights, awning, water pump, tank heater, and AC via the van's RV-C CAN bus, and displays live power/battery data from the Victron Cerbo GX.

---

## System Architecture

```
iPhone / Browser
     │  WebSocket :9001
     ▼
Pi mosquitto broker (192.168.8.106 :1883 / :9001)
     │  MQTT bridge
     ▼
Victron Cerbo GX (192.168.12.140 :1883)   ← Victron telemetry only (read)
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
| Raspberry Pi 4 | 192.168.8.106 (Apple Pi LAN) / 100.98.52.107 (Tailscale) | Dashboard host, SSH: sgordon1024 / windows |
| GL.iNet GL-MT3000 (Beryl AX) | 192.168.8.1 | Travel router, SSID: Apple Pi |
| Victron Cerbo GX | 192.168.12.140 | VRM Portal ID: 48e7da875e6c |
| Firefly G12 controller | SA=0x9B | Controls lights, HVAC, awning, pump, tank heater |
| G12 LCD ("Bed Wall") | SA=0x9F | Touchscreen panel, Bluetooth to VegaTouch Mira |
| Lithionics Battery | SA=0x46 | |
| MultiPlus-II inverter | SA=0xE1 | |
| SmartSolar MPPT | SA=0x24 | |
| Tuya X5P smart plug | Local IP via GL.iNet | Controls Starlink power outlet |
| vGate iCar Pro BT3 | Bluetooth → /dev/rfcomm0 | OBD-II adapter, plugged into Sprinter's OBD port |

**Pi CAN HAT wiring:** Red=DC+, Black=DC−, White=CAN_H, Yellow=CAN_L into CAN_0 physical terminals. Physical CAN_0 = Linux `can1` (kernel assigns in reverse).

**Pi services (all auto-start on boot):**
- `can1-setup` — brings up can1 at 250kbps
- `can-bridge` — `can-bridge.py` MQTT↔CAN bridge
- `rope-light` — `rope-light.py` BLE↔MQTT bridge for interior rope lights
- `starlink-bridge` — `starlink-bridge.py` Starlink smart plug + GL.iNet repeater auto-switch
- `obd-bridge` — `obd-bridge.py` OBD-II data via vGate iCar Pro BT3
- `gogovan-web` — `python3 -m http.server 80` (port 80, runs as root)
- `nginx` — serves HTTPS on port 443 via Tailscale cert; proxies `/mqtt` WebSocket to mosquitto:9001

---

## Pi Network / Routing

**The Pi does NOT act as a hotspot.** A **GL.iNet GL-MT3000 (Beryl AX)** travel router handles the Wi-Fi network and upstream WAN. The Pi connects to the GL.iNet via ethernet (`eth0`).

| Interface | IP | Purpose |
|---|---|---|
| `eth0` | 192.168.8.106/24 (DHCP from GL.iNet) | LAN: connected to GL.iNet router via cable |
| `wlan0` | 192.168.12.122/24 (T-Mobile subnet) | Used only to reach Cerbo GX for MQTT bridge |

**GL.iNet router (Apple Pi network):**
- SSID: `Apple Pi` — this is the main network for phones, MacBook, and all van clients
- Admin panel: `http://192.168.8.1` (from any device on Apple Pi Wi-Fi)
- SSH: `ssh root@192.168.8.1` (from Pi, for starlink-bridge.py to control upstream switching)
- Handles DHCP, NAT, and WAN upstream between T-Mobile ("tmobile" SSID) and Starlink ("WiFi Blaster" SSID)
- Pi's ethernet MAC gets a stable DHCP lease at `192.168.8.106`

**Disabled on Pi (no longer used):**
- `hostapd` — masked (`systemctl mask hostapd`); GoGoVan SSID is gone
- `dnsmasq` — disabled; GL.iNet handles all DHCP/DNS for clients
- `wifi-watchdog` — disabled; was installed to restart brcmfmac but removed after root cause (loose power connector) was found

**Avahi mDNS** restricted to `allow-interfaces=eth0` in `/etc/avahi/avahi-daemon.conf` — `vanpi.local` resolves to `192.168.8.106` on the Apple Pi network.

**Cerbo GX MQTT access — important:** The Cerbo GX does **not** expose port 1883 on the Apple Pi network. It only exposes MQTT on the T-Mobile subnet where both the Pi and Cerbo connect as clients of the T-Mobile MiFi. On T-Mobile: Pi is `192.168.12.122` (wlan0), Cerbo is `192.168.12.140`. The mosquitto bridge must use `192.168.12.140:1883`. If upstream switches to Starlink or campground Wi-Fi, the Cerbo may get a different IP and the bridge will drop — check `mosquitto_sub -h localhost -t 'N/c0619ab5dcfb/#' -C 1 -W 5` to confirm data is flowing.

---

## Dashboard Access URLs

| Context | URL |
|---|---|
| On Apple Pi network (local) | http://vanpi.local or http://192.168.8.106 |
| Via Tailscale (HTTP) | http://100.98.52.107 |
| Via Tailscale (HTTPS) | https://vanpi.tail27a0b4.ts.net |

**Use the HTTPS URL whenever GPS/speedometer is needed** — iOS Safari blocks the Geolocation API on plain HTTP pages (reports as "permission denied" regardless of what the user taps). The HTTPS URL uses a Tailscale-issued Let's Encrypt cert served by nginx on the Pi.

**Arc browser cannot access local HTTP (http://vanpi.local or http://192.168.8.106) — Arc blocks private IP HTTP requests internally.** Use Safari, or the Tailscale URL in any browser.

Adding the dashboard to iPhone home screen (Safari → Share → Add to Home Screen) is the recommended approach — it opens in a full-screen Safari webview.

---

## Tailscale

| Device | Tailscale IP |
|---|---|
| Pi (vanpi) | 100.98.52.107 |
| MacBook (wt-mbp-steve-gordon) | 100.93.110.117 |
| iPhone (iphone-15-pro-max) | 100.102.31.31 |

- Key expiry disabled on Pi in Tailscale admin panel (no re-auth needed)
- Tailscale is **not installed on the MacBook** — use SSH via Apple Pi LAN (`ssh sgordon1024@192.168.8.106`) or via Tailscale if installed
- iPhone has Tailscale installed; must be connected to use the HTTPS URL

---

## Files

| File | Location | Purpose |
|---|---|---|
| `index.html` | Pi: `/home/sgordon1024/index.html` | Dashboard UI (single-file, ~627KB incl. bundled mqtt.js) |
| `can-bridge.py` | Pi: `/home/sgordon1024/can-bridge.py` | MQTT subscriber → CAN sender + CAN listener → MQTT publisher |
| `rope-light.py` | Pi: `/home/sgordon1024/rope-light.py` | BLE↔MQTT bridge for rope lights (bleak + paho-mqtt) |
| `starlink-bridge.py` | Pi: `/home/sgordon1024/starlink-bridge.py` | Starlink smart plug (tinytuya) + GL.iNet repeater switching |
| `obd-bridge.py` | Pi: `/home/sgordon1024/obd-bridge.py` | OBD-II data bridge (python-obd via /dev/rfcomm0) |
| `run-speedtest.py` | Pi: `/home/sgordon1024/run-speedtest.py` | Runs `speedtest-cli --json --secure`, publishes result to MQTT |
| `deploy-to-pi.sh` | Dev: project root | Main deploy script — auto-detects Tailscale or Apple Pi LAN |
| `deploy-local.sh` | Dev: project root | Deploy when offline (tries 192.168.8.106, 192.168.4.1, Tailscale) |
| `fix-hostapd.sh` | Dev: project root | One-time: adds auto-restart + country code to hostapd (legacy) |
| `ble-sweep2.py` | Dev: project root | BLE sweep script for rope light command discovery |
| `ble-sweep3.py` | Dev: project root | BLE sweep script for rope light animation commands |
| `pi-setup/setup-speedtest.sh` | Dev only | Deploys `run-speedtest.py` + systemd timer to Pi; run once |
| `pi-setup/setup-travel-router.sh` | Dev only | Historical: set up Pi as travel router (now replaced by GL.iNet) |
| `pi-setup/setup-obd.sh` | Dev only | One-time: pairs vGate adapter, creates rfcomm binding, installs obd-bridge service |

**Deploy command (from project root):**
```bash
./deploy-to-pi.sh
```
Auto-detects connection: tries Tailscale first (`100.98.52.107`), then `vanpi.local`, then `192.168.8.106`. Deploys: index.html, can-bridge.py, rope-light.py, starlink-bridge.py, obd-bridge.py, run-speedtest.py. Restarts all services after deploy.

**IMPORTANT: Always run `./deploy-to-pi.sh` immediately after every change to any Pi file.** The user reviews changes live on the dashboard — if you don't deploy right away, they can't see what you did.

---

## Dashboard Tabs (Normal Mode)

The dashboard has 5 tabs in the bottom nav bar (Apple HIG style):

| Tab | Contents |
|---|---|
| **Power** | Battery SOC/voltage, solar input, grid/shore power, inverter mode, Victron data, charging lightning animation |
| **Lights** | All G12 lights with on/off/dim, Light Themes (scenes), rope lights (BLE) |
| **Climate** | AC mode (cool/off), fan speed (high/low/auto), setpoint, ambient temperature |
| **Controls** | Water pump, tank heater, awning (extend/retract/stop) |
| **Internet** | T-Mobile/Starlink switching, Starlink power/auto toggle, speed test, all-time stats overlay |

---

## Drive Mode

Drive mode activates automatically when GPS speed stays above 5 mph for 4 consecutive seconds. Can also be toggled manually via the Drive Mode card.

### What happens on enter
1. All G12 lights turned off (state saved to `preDriveLights`)
2. Water pump turned off (`pumpWasOn` saved)
3. AC turned off via `setAcMode('off')`
4. Rope lights turned off (state saved to `predriveRope`: color, effect, brightness, speed)
5. Awning retracted (G12 stops at limit switch if already retracted)
6. UI locked to drive layout: Speed, Internet, and Climate tabs via bottom drive nav

### What happens on exit (parked — speed drops below 2 mph)
1. Water pump always restored to ON
2. Rope lights restored to exact pre-drive state: color or effect re-published, brightness/speed re-applied
3. "Arrived?" toast shown if any G12 lights were on before driving — user taps to restore them

### Manual override
- Tapping Drive Mode toggle while driving calls `exitDrivingMode()` and sets `driveModeManuallyPaused = true`
- Subtitle shows "Paused · tap to resume" while moving with auto paused
- Tapping again clears the flag and re-enables auto-detection
- Flag is session-only (not persisted)

### Drive nav tabs
- **Speed** → speedometer + battery/power panels + rest stop finder + engine panel (OBD data)
- **Internet** → carrier selector + speed test
- **Climate** → full climate card (AC mode, fan, setpoint)

### Key constants
```javascript
DRIVE_SPEED_MPH  = 5     // enter threshold
STOP_SPEED_MPH   = 2     // exit threshold
DRIVE_CONFIRM_MS = 4000  // must hold above threshold before activating
```

### Rest Stop Finder
Displayed in the top-left box of the drive Speed tab. Uses Overpass API (OpenStreetMap) to find nearby rest stops, gas stations, and amenities based on current GPS coordinates. Updates every time GPS updates significantly. Shows name, type, distance/direction.

### Engine Panel (OBD-II)
Displayed in the drive Speed tab below the speedometer. Shows live data from the Sprinter's OBD-II port via the vGate iCar Pro BT3 adapter:
- RPM, speed (mph), coolant temperature (°F), fuel level (%), throttle position (%), battery voltage (V)
- MIL (check engine light) indicator and DTC fault code list
- Data published by `obd-bridge.py` via MQTT to `van/status/obd/*`

---

## Light Themes (Scenes)

A "Light Themes" section at the top of the Lights tab lets the user save and recall named light configurations.

- Each scene stores the on/off/dim state of all G12 lights at the time it was saved
- Scenes are saved to `localStorage` key `scenes` and also published retained to `van/status/scenes` MQTT topic
- Default scenes are pre-loaded on first launch
- Scene grid: 2-column tile layout
- Edit mode (tap "Edit" button or long-press any tile): tiles jiggle (iOS-style), can be dragged to reorder, trash icon to delete
- Tap a tile to activate that scene (sends light commands for all lights in the scene)
- "+" tile to save current light state as a new named scene

---

## Themes

The dashboard has 26 visual color themes (Night, Day, Desert, Ocean, Forest, Neon, etc.). A theme button (palette icon) opens a bottom sheet picker. Themes are saved to `localStorage` key `theme`.

---

## MQTT Library — Bundled Inline

`index.html` includes `mqtt.min.js` (~369KB) **inlined directly** inside a `<script>` tag — NOT loaded from a CDN. This means the dashboard loads and operates fully without any internet access (critical when first connecting to Apple Pi before it acquires an upstream connection).

Do not change this back to a CDN `<script src="https://unpkg.com/mqtt/...">` tag — that was the root cause of the persistent "Connecting…" spinner.

MQTT connection auto-detects protocol: `ws://${hostname}:9001` on HTTP, `wss://${hostname}/mqtt` on HTTPS. The `/mqtt` path is proxied by nginx to `localhost:9001` — required because browsers block mixed-content WebSocket on HTTPS pages.

---

## Remote Access

- Pi Tailscale IP: `100.98.52.107` (key expiry disabled — no re-auth)
- Dashboard URL (HTTP): `http://100.98.52.107`
- Dashboard URL (HTTPS + GPS): `https://vanpi.tail27a0b4.ts.net`
- SSH: `ssh sgordon1024@100.98.52.107` (password: `windows`)
- SSH (local): `ssh sgordon1024@192.168.8.106` (when on Apple Pi network)

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
| `van/rope-light/power` | Dashboard → rope-light.py | `on`, `off` |
| `van/rope-light/color` | Dashboard → rope-light.py | `red`, `orange`, `amber`, `yellow`, `lime`, `green`, `teal`, `cyan`, `sky`, `blue`, `navy`, `purple`, `pink`, `white` |
| `van/rope-light/brightness` | Dashboard → rope-light.py | `1`–`100` |
| `van/rope-light/effect` | Dashboard → rope-light.py | `cycle` (software color cycling) |
| `van/rope-light/speed` | Dashboard → rope-light.py | `1`–`10` (cycle speed) |
| `van/starlink/power` | Dashboard → starlink-bridge.py | `on`, `off` |
| `van/starlink/auto` | Dashboard → starlink-bridge.py | `on`, `off` |
| `van/starlink/threshold` | Dashboard → starlink-bridge.py | Mbps value (default 5) |
| `van/network/speedtest` | Dashboard → run-speedtest.py | `run` (triggers manual test) |

| Topic (publish, retained) | Direction | Payload |
|---|---|---|
| `van/status/light/{name}` | Bridge → Dashboard | `off`, `1`–`100` |
| `van/status/ac/mode` | Bridge → Dashboard | `cool`, `off` |
| `van/status/ac/fan` | Bridge → Dashboard | `high`, `low`, `auto` |
| `van/status/ac/setpoint` | Bridge → Dashboard | integer °F |
| `van/status/ac/temp` | Bridge → Dashboard | integer °F |
| `van/status/starlink/power` | starlink-bridge → Dashboard | `on`, `off`, `unknown` |
| `van/status/starlink/auto` | starlink-bridge → Dashboard | `on`, `off` |
| `van/status/starlink/threshold` | starlink-bridge → Dashboard | Mbps value |
| `van/status/starlink/quality` | starlink-bridge → Dashboard | `good`, `poor`, `unknown` |
| `van/status/network/upstream` | starlink-bridge → Dashboard | `tmobile`, `starlink` |
| `van/status/network/speedtest` | run-speedtest → Dashboard | JSON: `{download, upload, ping, server, upstream, timestamp, error}` |
| `van/status/network/speedtest/running` | run-speedtest → Dashboard | `true` / `false` |
| `van/status/obd/connected` | obd-bridge → Dashboard | `ok`, `searching`, `error` |
| `van/status/obd/rpm` | obd-bridge → Dashboard | integer |
| `van/status/obd/speed` | obd-bridge → Dashboard | integer mph |
| `van/status/obd/coolant-temp` | obd-bridge → Dashboard | integer °F |
| `van/status/obd/fuel-level` | obd-bridge → Dashboard | integer % |
| `van/status/obd/throttle-pos` | obd-bridge → Dashboard | integer % |
| `van/status/obd/voltage` | obd-bridge → Dashboard | float V |
| `van/status/obd/mil` | obd-bridge → Dashboard | `on`, `off` |
| `van/status/obd/dtcs` | obd-bridge → Dashboard | JSON array of strings |
| `van/status/scenes` | Dashboard → Dashboard | JSON array of scene objects (retained) |

All status topics use `retain=True` so the dashboard gets current state immediately on page load.

### Internet / Speed Test topics

`upstream` values: `tmobile` (GL.iNet on T-Mobile "tmobile" SSID), `starlink` (GL.iNet on "WiFi Blaster" SSID), `unknown`.

Speed test results are stored in **`localStorage` key `gogovan-speed-history`** as a JSON array. Each entry: `{ts, isoTs, upstream, down, up, ping, server, lat, lng}`. Max 500 entries (oldest pruned on save). Automatic tests run every 30 minutes via systemd timer.

GPS is captured with `navigator.geolocation.getCurrentPosition()` (8s timeout, 2min cache) at the time of each test result and stored as `{lat, lng}` in the history entry. Each result in the stats list links to `maps.apple.com/?ll=lat,lng`.

---

## Stats Overlay (All-Time Internet Stats)

Opened via "View All-Time Stats" button on the Internet tab. Renders as a full-screen overlay with:
- **Carrier filter**: All / T-Mobile / Starlink
- **SVG polyline chart**: amber=T-Mobile (solid=download, dashed=upload), blue=Starlink. Plots daily averages (aggregated by `aggregateDailyStats()`) to keep the DOM lean.
- **Test Results list**: Sorted newest-first, 50 entries per page with "Load more". Shows carrier badge, date, speeds, ping, and GPS link.

---

## Rope Lights (BLE)

Interior accent LED rope lights, controlled via Bluetooth LE. The `rope-light` systemd service runs `rope-light.py` on the Pi.

**BLE controller:**
- MAC address: `92:18:11:00:F7:24`
- GATT write characteristic: `0000ffd9`
- Protocol:
  - ON: `cc 23 33`
  - OFF: `cc 24 33`
  - Color: `56 [B] [R] [G] [W] f0 aa` (byte order is B-R-G-W, not R-G-B)
  - State query: `ef 01 77` → 12-byte response: `66 10 [power:23=on/24=off] 00 [mode] [speed] [B] [G] [R] [W] 03 99`
  - Solid color mode byte = 0x32, animation mode = 0x02

**BLE connectivity note:** 
- Must run `bluetoothctl remove 92:18:11:00:F7:24` before bleak can scan (cached connection blocks BLE advertising) — the service's `ExecStartPre` handles this
- Bluetooth is soft-blocked by rfkill on boot — service runs `rfkill unblock bluetooth` before connecting
- Force-quit Sun Home app on iPhone or it will hold the BLE connection and the Pi can't connect

**Dashboard UI:** Located at the bottom of the Lights tab. Controls: power toggle, 14-color palette, brightness slider (1–100%), speed slider (1–10, for cycle), Color Cycle effect button.

**Color cycle** is implemented in software in `rope-light.py` (not a native device animation) — asyncio task cycles through 8 colors at `cycle_speed` interval. Native hardware animation commands are still being reverse-engineered; sweep scripts `ble-sweep2.py` and `ble-sweep3.py` are in the project root.

---

## Starlink Automation (starlink-bridge.py)

`starlink-bridge.py` runs as `starlink-bridge.service` on the Pi. It:
1. Controls a Tuya X5P smart plug (Starlink power outlet) via **tinytuya** (local control, no cloud)
2. Controls the GL.iNet router's upstream Wi-Fi connection via SSH + GL.iNet UCI commands

**Tuya smart plug:**
- Device ID: `eb21e6caef01e8582972u9`
- Local key: `knGT9!<jN3jA~npU`
- Version: 3.3
- tinytuya installed: `pip3 install tinytuya`

**GL.iNet router control (from Pi via SSH):**
- `ssh root@192.168.8.1` (no password — Pi's SSH key is authorized)
- Switches upstream SSID via UCI: `uci set wireless.@wifi-iface[1].ssid=...`
- T-Mobile: SSID `tmobile`, Starlink: SSID `WiFi Blaster`

**Auto-switch logic:**
- Default state: GL.iNet on T-Mobile, Starlink plug OFF
- On startup: if on T-Mobile, immediately run a speed test
  - If < threshold (default 5 Mbps) → switch to Starlink automatically
  - If >= threshold → stay on T-Mobile
- While on Starlink: test T-Mobile via wlan0 every 30 min (no Starlink data used)
  - If T-Mobile >= threshold → switch GL.iNet back to T-Mobile, power off Starlink

**History: Why tuya-convert was abandoned:**
- tuya-convert hijacks `wlan0` entirely (creates a hostapd AP), losing Tailscale connectivity
- All Pi ports (80, 443, 53, 1883) are occupied by the dashboard stack
- Recovery required physical access to power-cycle the Pi
- `rm -rf ~/tuya-convert` — do NOT attempt this approach again

---

## OBD-II Integration (obd-bridge.py)

`obd-bridge.py` connects to the vGate iCar Pro BT3 Bluetooth OBD adapter via `/dev/rfcomm0` (Bluetooth Classic SPP, bound by `rfcomm-obd.service`). Run `pi-setup/setup-obd.sh` once to pair the adapter and install the service.

**Data published (all retain=True):**
- `van/status/obd/connected` — `ok` / `searching` / `error`
- `van/status/obd/rpm`, `van/status/obd/speed` (mph)
- `van/status/obd/coolant-temp` (°F), `van/status/obd/fuel-level` (%)
- `van/status/obd/throttle-pos` (%), `van/status/obd/voltage` (V)
- `van/status/obd/mil` — `on` / `off` (check engine light)
- `van/status/obd/dtcs` — JSON array of fault codes

**Poll rates:** Fast gauges every 2 seconds, MIL + DTCs every 30 seconds.

The engine panel in drive mode reads these topics and displays them. OBD data only updates when the vehicle ignition is on.

---

## Pi System Service Files

### `/etc/systemd/system/gogovan-web.service`
```ini
[Unit]
Description=GoGoVan Web Dashboard
After=network.target

[Service]
User=root
WorkingDirectory=/home/sgordon1024
ExecStart=/usr/bin/python3 -m http.server 80
Restart=always

[Install]
WantedBy=multi-user.target
```
Runs as root (required for port 80). Serves `index.html` at `http://vanpi.local` and `http://192.168.8.106`.

### `/etc/nginx/sites-available/gogovan`
```nginx
server {
    listen 443 ssl;
    server_name vanpi.tail27a0b4.ts.net;

    ssl_certificate     /home/sgordon1024/vanpi.tail27a0b4.ts.net.crt;
    ssl_certificate_key /home/sgordon1024/vanpi.tail27a0b4.ts.net.key;

    root /home/sgordon1024;
    index index.html;

    location / {
        try_files $uri $uri/ =404;
    }

    location /mqtt {
        proxy_pass http://localhost:9001;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header Host $host;
    }
}
```
Symlinked to `/etc/nginx/sites-enabled/gogovan`. Default site removed so nginx doesn't conflict with the Python HTTP server on port 80.

### `/etc/avahi/avahi-daemon.conf` (relevant diff)
```ini
[server]
allow-interfaces=eth0
```
Restricts `vanpi.local` to resolve via ethernet (`eth0`) only, so it always returns `192.168.8.106` on the Apple Pi network.

### `/etc/mosquitto/conf.d/` (key settings)
```
per_listener_settings true
listener 9001 0.0.0.0   # WebSocket (dashboard)
listener 1883 0.0.0.0   # TCP (bridge → Cerbo)
```
With a bridge configured to forward Victron telemetry from Cerbo at 192.168.12.140:1883.

---

## Key Decisions & Why

**Why Pi controls CAN instead of Cerbo:**  
Cerbo's `can0` (VE.Can) is in listen-only mode. `cansend` runs silently but nothing is transmitted. Discovered after Node-RED exec nodes appeared to work but G12 never responded.

**Why mosquitto runs on Pi (not just Cerbo):**  
Enables Tailscale remote access. The dashboard connects to `window.location.hostname:9001`, which works both locally (Pi IP) and remotely (Tailscale). Cerbo's MQTT isn't reachable over Tailscale. Pi mosquitto bridges Victron telemetry from Cerbo.

**Why mqtt.js is inlined instead of loaded from CDN:**  
The dashboard first loads when connecting to Apple Pi, before the router has acquired an upstream internet connection. A CDN `<script src>` tag fails silently in this case, leaving `mqtt` undefined and the dashboard stuck on "Connecting…". Inlining makes the app fully self-contained — no internet required.

**Why there's a GL.iNet router instead of the Pi acting as a hotspot:**  
The Pi's brcmfmac Wi-Fi driver (wlan0) crashed intermittently under load, taking down the whole hotspot. A loose power connector was the root cause. The GL.iNet GL-MT3000 is a dedicated travel router with more reliable Wi-Fi hardware. The Pi connects to it via ethernet, which is stable. `hostapd` is masked on the Pi.

**Why deploy-to-pi.sh tries Tailscale first:**  
The Tailscale IP `100.98.52.107` is always stable regardless of what network the van is on. The Pi's local Apple Pi LAN IP (192.168.8.106) only works when on the Apple Pi network.

**Why Arc browser can't access the dashboard locally:**  
Arc has its own network stack that blocks HTTP requests to private IP ranges (192.168.x.x, 10.x.x.x). Even with macOS Local Network permission enabled, Arc refuses to connect. Safari and the Tailscale URL both work fine.

**Why `int()` not `round()` for temperature:**  
The Firefly LCD truncates fractional degrees. `round()` caused a 1°F discrepancy (e.g. 67.7°F → app showed 68°F, Firefly showed 67°F).

**Why `19FF9C9B` for ambient temp, not `19FFE29B`:**  
`19FFE29B` bytes[3–4] and bytes[5–6] are *both setpoints* (cool + heat) — they're identical and change with the arrows. Ambient temperature is on a separate proprietary frame `19FF9C9B` bytes[1–2]. Discovered by doing a broad candump and looking for the K×32 encoding of the known ambient temperature.

**Why tank heater controls 4 instances:**  
Instances 0x05–0x08 all activate simultaneously when the tank heater is switched on. They represent separate heating elements (fresh/grey/black/underbelly) but are controlled as a single system by the G12. A single button sends on/off to all four.

**Fan byte value for auto (0xCF in command, 0x00 in status):**  
The command byte for auto (`00CFFFFFFFFFFFFF`) was discovered by sniffing the Mira app via candump while pressing the auto button. Three other guesses failed first (0xDF, 0xDF+0x00, 0xD5). Status frame byte[2]=0x00 maps to auto.

**Why the speed test chart shows daily averages instead of individual points:**  
Automatic tests run every 30 min, so a year of history is ~17,500 entries. Rendering all of them as SVG nodes would make the chart unusably slow and visually unreadable. `aggregateDailyStats()` groups raw entries by day + carrier and plots the daily average, capping the chart at ~365 points regardless of how many tests were run.

**Why the speed test list paginates to 50 entries at a time:**  
Same scale problem — 17,500 DOM nodes at once would freeze the UI. The stats overlay loads 50 entries initially with a "Load more" button to append the next batch.

**Why HTTPS is required for GPS (drive mode speedometer):**
iOS Safari treats `GeolocationAPI.watchPosition()` as a secure-context-only feature. On plain HTTP, the permission dialog either doesn't appear or immediately returns error code 1 (PERMISSION_DENIED) regardless of user action. The Tailscale HTTPS URL with a valid Let's Encrypt cert is required. nginx on the Pi handles TLS termination and proxies the MQTT WebSocket (`/mqtt` → `localhost:9001`) so the `wss://` connection works from the HTTPS page.

**Renewing the Tailscale cert (expires periodically):**
SSH into Pi and run: `sudo tailscale cert vanpi.tail27a0b4.ts.net` — writes new `.crt`/`.key` to `/home/sgordon1024/`, then `sudo systemctl restart nginx`.

**Why the offline banner uses `env(safe-area-inset-top)` instead of `top: 20px`:**  
The Dynamic Island on iPhone 14 Pro and later sits ~59px from the top, so a fixed `20px` offset placed the banner behind it. `env(safe-area-inset-top)` is set by the browser to the exact inset height for the current device.

**Why Avahi is restricted to `eth0`:**  
Without this restriction, Avahi advertises `vanpi.local` on all interfaces. When the Pi's wlan0 is on the T-Mobile subnet (192.168.12.x), clients on Apple Pi could receive mDNS responses with the wlan0 IP (unreachable from Apple Pi subnet). Restricting to `eth0` ensures the advertised IP is always `192.168.8.106`.

**Why drive mode manual toggle sets a `driveModeManuallyPaused` flag instead of just calling exit:**  
Without the flag, `onGPSUpdate` would immediately re-trigger `enterDrivingMode` after 4 seconds since the van is still moving. The flag blocks auto-re-entry for the rest of the browser session. Only a manual tap to re-enable clears it.

**Why rope lights use a separate `rope-light.py` service instead of `can-bridge.py`:**  
The rope lights are BLE, not RV-C CAN. They use a completely different protocol stack (bleak for BLE vs. python-can). Keeping them in a separate service means a BLE reconnect loop doesn't affect CAN bus control, and the two services can restart independently.

**Why rope light state is saved/restored on drive mode enter/exit (unlike AC):**  
Rope lights are ambient accent lighting — if they were on when you started driving, you almost certainly want them back when you park. AC is different: arriving at camp doesn't mean you immediately want cooling. So AC is turned off on drive enter but not restored on exit, while rope lights are always silently restored.

**Why the rope light BLE byte order is B-R-G-W (not R-G-B):**  
Discovered by sniffing BLE commands while setting known colors. The controller's `56` color command places Blue in byte[1], Red in byte[2], Green in byte[3], White in byte[4] — unusual but confirmed empirically.

**Why tinytuya instead of Tuya cloud API for the Starlink plug:**  
tinytuya communicates directly with the plug on the local network — no internet required and no latency. Tuya cloud pairing is still required once to obtain the `local_key`, but after that all control is local. tuya-convert (OTA flash to Tasmota) was attempted and abandoned because it requires taking over `wlan0` and loses Tailscale connectivity.
