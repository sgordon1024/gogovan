# GoGoVan Dashboard — Project Context

## What This Is
A custom web dashboard for a **2024 Entegra Launch camper van (Mercedes Sprinter chassis)**, served from a Raspberry Pi 4 and accessible on the local Wi-Fi and remotely via Tailscale. It controls lights, awning, water pump, tank heater, and AC via the van's RV-C CAN bus, and displays live power/battery data from the Victron Cerbo GX.

---

## System Architecture

```
iPhone / Browser
     │  WebSocket :9001
     ▼
Pi mosquitto broker (192.168.4.1 :1883 / :9001)
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
| Raspberry Pi 4 | 192.168.4.1 (GoGoVan) / 100.98.52.107 (Tailscale) | Dashboard host, SSH: sgordon1024 / windows |
| Victron Cerbo GX | 192.168.12.140 | VRM Portal ID: 48e7da875e6c |
| Firefly G12 controller | SA=0x9B | Controls lights, HVAC, awning, pump, tank heater |
| G12 LCD ("Bed Wall") | SA=0x9F | Touchscreen panel, Bluetooth to VegaTouch Mira |
| Lithionics Battery | SA=0x46 | |
| MultiPlus-II inverter | SA=0xE1 | |
| SmartSolar MPPT | SA=0x24 | |

**Pi CAN HAT wiring:** Red=DC+, Black=DC−, White=CAN_H, Yellow=CAN_L into CAN_0 physical terminals. Physical CAN_0 = Linux `can1` (kernel assigns in reverse).

**Pi services (all auto-start on boot):**
- `can1-setup` — brings up can1 at 250kbps
- `can-bridge` — `can-bridge.py` MQTT↔CAN bridge
- `gogovan-web` — `python3 -m http.server 80` (port 80, runs as root)

---

## Pi Network / Routing

The Pi acts as a Wi-Fi hotspot and travel router:

| Interface | IP | Purpose |
|---|---|---|
| `uap0` | 192.168.4.1/24 | GoGoVan hotspot (hostapd) |
| `wlan0` | DHCP (upstream) | WAN: T-Mobile / Starlink / campground Wi-Fi |

- **hostapd** creates the `GoGoVan` SSID on `uap0`
- **dnsmasq** provides DHCP (192.168.4.100–200) to clients on `uap0`
- **iptables NAT** (MASQUERADE on wlan0) routes client traffic through wlan0
- **IP forwarding** enabled persistently: `/etc/sysctl.d/99-ipforward.conf` → `net.ipv4.ip_forward=1`
- **Avahi mDNS** restricted to `allow-interfaces=uap0` in `/etc/avahi/avahi-daemon.conf` — prevents `vanpi.local` from resolving to the wlan0 IP (192.168.1.x) instead of 192.168.4.1

**GoGoVan Wi-Fi password:** `1234567890`

---

## Dashboard Access URLs

| Context | URL |
|---|---|
| On GoGoVan network | http://vanpi.local |
| Via Tailscale (any network) | http://100.98.52.107 |

**Arc browser cannot access local HTTP (http://vanpi.local or http://192.168.4.1) — Arc blocks private IP HTTP requests internally.** Use Safari, or the Tailscale URL in any browser.

Adding the dashboard to iPhone home screen (Safari → Share → Add to Home Screen) is the recommended approach — it opens in a full-screen Safari webview.

---

## Tailscale

| Device | Tailscale IP |
|---|---|
| Pi (vanpi) | 100.98.52.107 |
| MacBook (wt-mbp-steve-gordon) | 100.93.110.117 |
| iPhone (iphone-15-pro-max) | 100.102.31.31 |

- Key expiry disabled on Pi in Tailscale admin panel (no re-auth needed)
- Tailscale CLI on Mac: `/Applications/Tailscale.app/Contents/MacOS/Tailscale`

---

## Files

| File | Location | Purpose |
|---|---|---|
| `index.html` | Pi: `/home/sgordon1024/index.html` | Dashboard UI (single-file, ~479KB incl. bundled mqtt.js) |
| `can-bridge.py` | Pi: `/home/sgordon1024/can-bridge.py` | MQTT subscriber → CAN sender + CAN listener → MQTT publisher |
| `deploy-to-pi.sh` | Dev: project root | Deploys index.html + can-bridge.py to Pi via Tailscale |
| `pi-setup/setup-speedtest.sh` | Dev only | Deploys `run-speedtest.py` + systemd timer to Pi; run once |
| `run-speedtest.py` | Pi: `/home/sgordon1024/run-speedtest.py` | Runs `speedtest-cli --json --secure`, publishes result to MQTT |

**Deploy command (from project root):**
```bash
./deploy-to-pi.sh
```
Uses Tailscale IP (`100.98.52.107`) so it works from any network — GoGoVan, T-Mobile hotspot, home Wi-Fi, etc.

---

## MQTT Library — Bundled Inline

`index.html` includes `mqtt.min.js` (~369KB) **inlined directly** inside a `<script>` tag — NOT loaded from a CDN. This means the dashboard loads and operates fully without any internet access (critical when first connecting to GoGoVan before it acquires an upstream connection).

Do not change this back to a CDN `<script src="https://unpkg.com/mqtt/...">` tag — that was the root cause of the persistent "Connecting…" spinner on the GoGoVan network.

MQTT connection: `mqtt.connect(`ws://${window.location.hostname}:9001`, {...})` — the hostname dynamically resolves to either `vanpi.local` (local) or `100.98.52.107` (Tailscale).

---

## Remote Access

- Pi Tailscale IP: `100.98.52.107` (key expiry disabled — no re-auth)
- Dashboard URL (remote): `http://100.98.52.107`
- SSH: `ssh sgordon1024@100.98.52.107` (password: `windows`)

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

All status topics use `retain=True` so the dashboard gets current state immediately on page load.

### Internet / Speed Test topics

| Topic | Direction | Payload |
|---|---|---|
| `van/network/speedtest` | Dashboard → Bridge | `run` (triggers manual test) |
| `van/status/network/speedtest` | Bridge → Dashboard | JSON: `{download, upload, ping, server, upstream, timestamp, error}` |
| `van/status/network/speedtest/running` | Bridge → Dashboard | `true` / `false` |

`upstream` values: `tmobile` (NetworkManager connection `preconfigured`), `starlink` (connection `wifi-blaster`), `unknown`.

Speed test results are stored in **`localStorage` key `gogovan-speed-history`** as a JSON array. Each entry: `{ts, isoTs, upstream, down, up, ping, server, lat, lng}`. Max 500 entries (oldest pruned on save). Automatic tests run every 30 minutes via systemd timer.

GPS is captured with `navigator.geolocation.getCurrentPosition()` (8s timeout, 2min cache) at the time of each test result and stored as `{lat, lng}` in the history entry. Each result in the stats list links to `maps.apple.com/?ll=lat,lng`.

---

## Stats Overlay (All-Time Internet Stats)

Opened via "View All-Time Stats" button on the Internet tab. Renders as a full-screen overlay with:
- **Carrier filter**: All / T-Mobile / Starlink
- **SVG polyline chart**: amber=T-Mobile (solid=download, dashed=upload), blue=Starlink. Plots daily averages (aggregated by `aggregateDailyStats()`) to keep the DOM lean.
- **Test Results list**: Sorted newest-first, 50 entries per page with "Load more". Shows carrier badge, date, speeds, ping, and GPS link.

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
Runs as root (required for port 80). Serves `index.html` at `http://vanpi.local` and `http://192.168.4.1`.

### `/etc/avahi/avahi-daemon.conf` (relevant diff)
```ini
[server]
allow-interfaces=uap0
```
Without this, Avahi advertises `vanpi.local` on both `wlan0` and `uap0`. The wlan0 IP (e.g. 192.168.1.43 on Starlink) is unreachable from GoGoVan clients, so they'd get ERR_CONNECTION_REFUSED. Restricting to `uap0` ensures `vanpi.local` always resolves to `192.168.4.1`.

### `/etc/sysctl.d/99-ipforward.conf`
```
net.ipv4.ip_forward=1
```
Makes IP forwarding (required for NAT routing through the Pi) survive reboots.

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
The dashboard first loads when connecting to the GoGoVan hotspot, before the Pi has acquired an upstream internet connection. A CDN `<script src>` tag fails silently in this case, leaving `mqtt` undefined and the dashboard stuck on "Connecting…". Inlining makes the app fully self-contained — no internet required.

**Why deploy-to-pi.sh uses Tailscale IP (not Pi's local IP):**  
The Pi's local IP changes depending on which upstream network it's using (T-Mobile assigns 192.168.12.122, Starlink assigns something else, etc.). The Tailscale IP `100.98.52.107` is always stable regardless of what network the van is on.

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

**Why the offline banner uses `env(safe-area-inset-top)` instead of `top: 20px`:**  
The Dynamic Island on iPhone 14 Pro and later sits ~59px from the top, so a fixed `20px` offset placed the banner behind it. `env(safe-area-inset-top)` is set by the browser to the exact inset height for the current device (Dynamic Island, notch, or 0 on older models).

**Why Avahi is restricted to `uap0`:**  
Without this restriction, Avahi advertises `vanpi.local` on all interfaces. When the Pi is connected to Starlink via wlan0, clients on GoGoVan (uap0) receive mDNS responses with the wlan0 IP (e.g. 192.168.1.43), which is on a different subnet and unreachable. Restricting to uap0 ensures the advertised IP is always 192.168.4.1.
