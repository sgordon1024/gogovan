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
Victron Cerbo GX (192.168.8.147 :1883, on Apple Pi)   ← Victron telemetry only (read)
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
| Victron Cerbo GX | 192.168.8.147 (Apple Pi, reserved; MAC 14:d4:24:06:86:8f) | MQTT broker :1883; VRM Portal 48e7da875e6c, MQTT portal c0619ab5dcfb |
| Firefly G12 controller | SA=0x9B | Controls lights, HVAC, awning, pump, tank heater |
| G12 LCD ("Bed Wall") | SA=0x9F | Touchscreen panel, Bluetooth to VegaTouch Mira |
| Lithionics Battery | SA=0x46 | |
| MultiPlus-II inverter | SA=0xE1 | |
| SmartSolar MPPT | SA=0x24 | |
| Tuya X5P smart plug | 192.168.8.248 (reserved, MAC fc:67:1f:dd:67:b2) | Controls Starlink dish power; id+key auto-refreshed from Tuya cloud on bridge start (cloud name "Smart Socket 3") |
| vGate iCar Pro BT3 | Bluetooth → /dev/rfcomm0 | OBD-II adapter, plugged into Sprinter's OBD port |

**Pi CAN HAT wiring:** Red=DC+, Black=DC−, White=CAN_H, Yellow=CAN_L into CAN_0 physical terminals. Physical CAN_0 = Linux `can1` (kernel assigns in reverse).

**Pi services (all auto-start on boot):**
- `can1-setup` — brings up can1 at 250kbps
- `can-bridge` — `can-bridge.py` MQTT↔CAN bridge
- `rope-light` — `rope-light.py` BLE↔MQTT bridge for interior rope lights
- `starlink-bridge` — `starlink-bridge.py` Starlink smart plug + GL.iNet repeater auto-switch
- `obd-bridge` — `obd-bridge.py` OBD-II data via vGate iCar Pro BT3
- `voice-bridge` — `voice-bridge.py` voice control via Claude API (key in `~/.anthropic_key`)
- `gogovan-web` — `python3 -m http.server 80` (port 80, runs as root)
- `nginx` — serves HTTPS on port 443 via Tailscale cert; proxies `/mqtt` WebSocket to mosquitto:9001
- `gogovan-watchdog.timer` — runs `/usr/local/bin/gogovan-watchdog.sh` every 2 minutes; auto-switches wlan0 between T-Mobile/Starlink on internet failure and restarts Tailscale if it drops

---

## Pi Network / Routing

**The Pi does NOT act as a hotspot.** A **GL.iNet GL-MT3000 (Beryl AX)** travel router handles the Wi-Fi network and upstream WAN. The Pi connects to the GL.iNet via ethernet (`eth0`).

| Interface | IP | Purpose |
|---|---|---|
| `eth0` | 192.168.8.106/24 (DHCP from GL.iNet) | LAN: connected to GL.iNet router via cable |
| `wlan0` | varies by upstream | Internet uplink + Cerbo GX MQTT bridge (see wlan0 profiles below) |

**wlan0 connection profiles (NM):**

| Profile | SSID | Subnet | Use |
|---|---|---|---|
| `preconfigured` | T-Mobile Home Internet | 192.168.12.x | Primary internet uplink |
| `PhiladelphiaCollins` | Starlink WiFi | 192.168.1.x | Fallback internet uplink when T-Mobile has no coverage |

**Critical wlan0 routing fix (applied):** The T-Mobile Home Internet DHCP server injects a default route at metric 50 via RFC 3442, which breaks Tailscale by creating duplicate routes. Fixed permanently:
```bash
sudo nmcli connection modify preconfigured ipv4.ignore-auto-routes yes ipv4.routes "0.0.0.0/0 192.168.12.1 600"
```
This tells NM to ignore DHCP-provided routes and use only the explicit static route at metric 600. Do not revert this.

**GL.iNet router (Apple Pi network):**
- SSID: `Apple Pi` — this is the main network for phones, MacBook, and all van clients
- Admin panel: `http://192.168.8.1` (from any device on Apple Pi Wi-Fi)
- **Admin / SSH password: `Windows1024`** (same for the web panel and `ssh root@192.168.8.1`; runs OpenWrt 21.02 / model GL-MT3000 "Beryl AX")
- SSH: `ssh root@192.168.8.1` (OpenWrt — use `uci` here, NOT on the Pi)
- Handles DHCP, NAT, and WAN upstream between T-Mobile ("tmobile" SSID) and Starlink ("PhiladelphiaCollins" SSID, formerly "WiFi Blaster")
- Pi's ethernet MAC gets a stable DHCP lease at `192.168.8.106`
- **DHCP hands out the Pi (`192.168.8.106`) as the gateway** so all client internet traffic routes through the Pi (which NATs out to the active wlan0 upstream). Set via:
  ```bash
  uci add_list dhcp.lan.dhcp_option='3,192.168.8.106'   # gateway = Pi
  uci add_list dhcp.lan.dhcp_option='6,8.8.8.8,1.1.1.1'  # DNS
  uci commit dhcp && /etc/init.d/dnsmasq restart
  ```

**Disabled on Pi (no longer used):**
- `hostapd` — masked (`systemctl mask hostapd`); GoGoVan SSID is gone
- `dnsmasq` — disabled; GL.iNet handles all DHCP/DNS for clients
- `wifi-watchdog` — disabled; was installed to restart brcmfmac but removed after root cause (loose power connector) was found

**Avahi mDNS** restricted to `allow-interfaces=eth0` in `/etc/avahi/avahi-daemon.conf` — `vanpi.local` resolves to `192.168.8.106` on the Apple Pi network.

**Cerbo GX MQTT access:** The Cerbo now lives on the **Apple Pi network at `192.168.8.147`** (hostname `cerbo`/`einstein`, MAC `14:d4:24:06:86:8f`, reserved via GL.iNet DHCP). The Pi reaches it over `eth0` regardless of which WAN `wlan0` is on, so **battery/power data works on both T-Mobile and Starlink** (previously it only worked on the T-Mobile subnet). The mosquitto bridge (`/etc/mosquitto/conf.d/gogovan.conf`) uses `address 192.168.8.147:1883`. Victron MQTT only publishes after a keepalive (the dashboard sends these); to test manually: `mosquitto_pub -h localhost -t R/c0619ab5dcfb/keepalive -m '' && mosquitto_sub -h localhost -t 'N/c0619ab5dcfb/#' -C 1 -W 5`.

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
| iPhone (iphone-15-pro-max) | 100.102.31.31 |

- Key expiry disabled on Pi in Tailscale admin panel (no re-auth needed)
- **MacBook CANNOT use Tailscale** — it was deleted and cannot be reinstalled (work computer). Never suggest `ssh sgordon1024@100.98.52.107` when helping from the Mac. The ONLY way to SSH from the Mac is `ssh sgordon1024@192.168.8.106` while on the Apple Pi network.
- iPhone has Tailscale installed; must be connected to use the HTTPS dashboard URL
- **NEVER run `sudo tailscale up --reset`** — this logs the Pi out of Tailscale entirely and requires opening a browser login URL to re-authenticate. If Tailscale needs a kick, use `sudo systemctl restart tailscaled && sudo tailscale up` (no --reset).

---

## Files

| File | Location | Purpose |
|---|---|---|
| `index.html` | Pi: `/home/sgordon1024/index.html` | Dashboard UI (single-file, ~627KB incl. bundled mqtt.js) |
| `can-bridge.py` | Pi: `/home/sgordon1024/can-bridge.py` | MQTT subscriber → CAN sender + CAN listener → MQTT publisher |
| `rope-light.py` | Pi: `/home/sgordon1024/rope-light.py` | BLE↔MQTT bridge for rope lights (bleak + paho-mqtt) |
| `starlink-bridge.py` | Pi: `/home/sgordon1024/starlink-bridge.py` | Starlink smart plug (tinytuya) + GL.iNet repeater switching |
| `obd-bridge.py` | Pi: `/home/sgordon1024/obd-bridge.py` | OBD-II data bridge (python-obd via /dev/rfcomm0) |
| `voice-bridge.py` | Pi: `/home/sgordon1024/voice-bridge.py` | Voice control: transcript → Claude API → structured actions (key in `~/.anthropic_key`) |
| `run-speedtest.py` | Pi: `/home/sgordon1024/run-speedtest.py` | Runs the official Ookla `speedtest --format=json`; used by BOTH the 4h timer and manual taps (deploy-to-pi.sh now copies it) |
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
Auto-detects connection: tries Tailscale first (`100.98.52.107`), then `vanpi.local`, then `192.168.8.106`. Deploys: index.html, can-bridge.py, rope-light.py, starlink-bridge.py, obd-bridge.py, run-speedtest.py, voice-bridge.py (installs its systemd unit). Restarts all services after deploy.

**IMPORTANT: Always run `./deploy-to-pi.sh` immediately after every change to any Pi file.** The user reviews changes live on the dashboard — if you don't deploy right away, they can't see what you did.

---

## Dashboard Tabs (Normal Mode)

The bottom nav bar has 5 slots (Apple HIG style): **Power · Controls · 🎤 Voice (raised center button) · Climate · More**.

| Tab / button | Contents |
|---|---|
| **Power** | Battery SOC/voltage, solar input, grid/shore power, inverter mode, Victron data |
| **Controls** | Merged Lights + Controls — shows **both** the lights panel (all G12 lights on/off/dim, Light Themes/scenes, rope lights) **and** the controls panel (water pump, tank heater, awning). |
| **🎤 Voice** | Center raised circular button (`.tab-voice` / `.tab-voice-circle`) — opens the full-screen voice overlay (`startVoiceListen()`). See **Voice Control** below. |
| **Climate** | AC mode (cool/off), fan speed (high/low/auto), setpoint, ambient temperature, sleep timer |
| **More** | Hamburger (`.tab-menu` → `openMenu()`) opening the menu sheet (`#menuOverlay`) with two items: **Internet & Speed** (→ `switchTab('internet')`) and **Settings & Themes** (→ `openThemePicker()`). |

**Tab plumbing:** `switchTab(tab)` uses `TAB_PANELS` to map a tab to one or more panels — `controls:['lights','controls']` is the merge; `internet:['internet']` has **no** bottom-tab button (reached only via the More menu). It hides every panel in `ALL_TAB_PANELS`, shows the selected one(s), marks the matching `.tab-btn[data-tab=…]` active (optional-chained — internet/voice/menu have no `data-tab`), and scrolls to top.

### Voice Control (drive-mode + normal-mode mic)
Tapping the center 🎤 (normal mode) or the drive-nav Voice button (`#driveNavVoice`) opens a **full-screen overlay** (`#voiceOverlay`) that **only closes via its X button** (`stopVoiceListen()`) — no tap-outside/auto-close. It uses the browser's `webkitSpeechRecognition` (needs the **HTTPS** dashboard) and shows live feedback: the heard transcript streams into `#voiceTranscript` (interim + final), status into `#voiceStatus`, mic state via `.voice-mic` (`pulsing`/`thinking`/`error`). On a result it publishes `van/voice/request` → **`voice-bridge.py`** (Claude) → `van/voice/response`; `handleVoiceResponse()` runs the returned actions (`applyVoiceAction`) and speaks the reply. After a reply **or** an error the overlay stays open and reveals the **"🎤 Speak again"** button (`#voiceAgain`) so the user can issue another command; it's hidden again at the start of each listen. (See "Voice Control Pipeline" further below for the bridge/key details.)

### Battery hero (Power tab)
- The big SOC % (`#battPct`) and fill-bar (`#battBar`) are **blue while charging** and **white otherwise**. Charging = net battery power `> 10 W` (`isBattCharging()`); `applyBattChargeStyle()` is called from both `updateSoc` and `updatePower`.
- The **lightning bolt animation only plays while charging** (any source — solar/shore/alternator), driven by `scheduleLightning()` off the same `isBattCharging()` check. No charging → no bolt, white text.

### Inverter (Power tab)
Mode buttons On/Charger/Inverter/Off → `setInverterMode()` writes `W/{PORTAL_ID}/vebus/276/Mode`. **Turning the inverter OFF (mode 4) while the Starlink dish is powered on first shows a confirm dialog** (`showConfirm` / `#confirmOverlay`) — cutting the inverter kills AC and would drop the internet. The other modes apply without a prompt.

---

## Drive Mode

Drive mode activates automatically when the van is actually moving. Detection (`sensorsSayDriving()` → `evaluateDriveState()`) uses three signals, fastest first:
1. **OBD throttle/load variability** (`throttleVariabilityDriving()`): while driving, the accelerator (`accel-pos`) and engine load swing around constantly; at a high idle or parked they're static. Once accel-spread ≥6% or load-spread ≥10% has persisted for **≥3s** (`DRIVE_VAR_SUSTAIN`), it's "driving" and enters **immediately** (no extra confirm) — this is the fast path that fixed "drive mode takes too long." `recordDriveSample()` (fed from the OBD `accel-pos`/`engine-load` updates) keeps a 5s rolling window. OBD is fast-polled at **1s** (`POLL_FAST`) for resolution.
2. **OBD vehicle speed** ≥ 5 mph (held `DRIVE_CONFIRM_MS`).
3. **GPS speed** ≥ 5 mph as backup.

Can also be toggled manually via the Drive Mode card. (Revving the engine in park for 3+ s could trip the variability path — rare, and a manual toggle-off overrides it.)

### What happens on enter
1. All G12 lights turned off (state saved to `preDriveLights`)
2. Water pump turned off (`pumpWasOn` saved)
3. AC turned off **unconditionally** (`setAcMode('off')` every drive entry — robust even if the dashboard's tracked AC state is stale); any server-side sleep timer / cycle is also cancelled so the AC can't switch back on while driving
4. Rope lights turned off (state saved to `predriveRope`: color, effect, brightness, speed)
5. Awning retracted (G12 stops at limit switch if already retracted)
6. UI locked to drive layout: Speed, Internet, and Climate tabs via bottom drive nav

### When it exits to "parked" (OBD-informed)
Exit is decided by **engine state first, GPS second** (`engineRunning()` reads `obdData`):
- **Engine running (`connected==='ok'` and `rpm>0`) + stopped** → this is a stop light / traffic. **Never auto-park.** Any pending exit timer is cancelled. This is the fix for "don't switch to parked at a red light."
- **Engine off (`connected==='ok'` and `rpm===0`) + stopped** → genuinely parked. **Exit immediately** (no 60s wait).
- **OBD unavailable / transient dropout (`connected!=='ok'`, so `engineRunning()` returns `null`)** → fall back to the GPS-only grace timer: must stay below `STOP_SPEED_MPH` for `STOP_EXIT_MS` (60s) before exiting. A Bluetooth hiccup returns `null` (not `false`), so a dropout won't false-park while driving.

`evaluateDriveState()` is the single enter/exit authority — it runs on every GPS tick **and** on OBD `speed`/`rpm`/`connected` changes (via `updateOBDUI`). So OBD genuinely drives detection: entry and exit both work even without GPS, and shutting the engine off while stopped parks promptly.

### What happens on exit
1. Water pump always restored to ON via `restorePump()` (publishes the `van/light/pump` command **and** the retained `van/status/light/pump` status so the bridge actuates it and the UI stays in sync). The "Arrived?" toast's Restore button also calls it, re-asserting the pump in case the park-time publish didn't land. Note: drive mode only **exits** (and restores the pump) once the van is genuinely parked — engine off, or 60s of GPS-confirmed stop when OBD is unavailable; sitting stopped with the engine running is treated as a red light (held), so the pump returns when you shut the engine off.
2. Rope lights restored to the **exact** pre-drive state via the shared `applyRopeRestore()` — color OR any effect (cycle/candle/…), plus brightness/speed. Both the auto-restore on park and the "Arrived?" toast's Restore button call it, so they can't diverge (the toast used to reset non-cycle effects to red)
3. "Arrived?" toast shown if any G12 lights were on before driving — user taps to restore them

### Manual override (`driveOverride`)
Toggling the Drive Mode card overrides auto-detection until a **natural state change**, then auto resumes on its own:
- **Toggle OFF while actually driving** → `driveOverride='off'`: stays out of drive mode (no auto re-entry) until the van naturally **stops/parks**, which clears the override.
- **Toggle ON while parked** → `driveOverride='on'`: stays in drive mode (no auto-exit) until the van naturally **starts driving**, which clears it (then normal auto-exit applies when you next park).
- Toggling off while already parked / on while already driving just sets `null` (plain auto) — no override needed.
- Subtitle reflects it ("On · manual — auto resumes once moving", "Off · manual — auto resumes once parked"). Session-only (not persisted).

### Drive nav tabs
- **Speed** → speedometer + battery/power panels + rest stop finder + engine panel (OBD data)
- **Internet** → carrier selector + speed test
- **Climate** → full climate card (AC mode, fan, setpoint)

### Key constants
```javascript
DRIVE_SPEED_MPH  = 5     // enter threshold (held DRIVE_CONFIRM_MS before activating)
STOP_SPEED_MPH   = 2     // below this = stopped
DRIVE_CONFIRM_MS = 4000  // must hold above threshold before activating
STOP_EXIT_MS     = 60000 // GPS-only grace before parking — used ONLY when OBD engine state is unknown
```
Entry triggers on **either** OBD vehicle speed or GPS speed ≥ `DRIVE_SPEED_MPH`, held `DRIVE_CONFIRM_MS`.
It does NOT trigger on engine-start alone (no road speed), so warming up in the driveway with
lights on won't kill them. `engineRunning()` (rpm>0 when connected) only refines **exit**
(red light vs. parked).

### Rest Stop Finder (preloaded POIs)
Top-left box of the drive Speed tab. Tapping the box cycles through `POI_CATEGORIES`
(Rest Areas → Cracker Barrel → Walmart → BLM Land), each an Overpass API (OpenStreetMap)
query around the current GPS fix. Shows the nearest-ahead match's name, distance, and exit.

**All categories are preloaded so cycling is instant:**
- `preloadAllPOIs()` fetches every category sequentially (current one first, then the rest — gentle on the public Overpass instance) and stores each result in `poiCache` (`{cat.id: {result, ts}}`).
- It fires once on the **first GPS fix** (`poiInitialPreloaded` guard in `onGPSUpdate`) so the cache is warm before drive mode even starts, and again from `startRestStopUpdates()` when drive mode begins.
- `handleRestBoxClick()` renders the cached result **immediately**, then background-refreshes only if the cache entry is older than `REST_STOP_CACHE_MS` (60s). Categories not yet cached fall back to a live fetch with a "Searching…" placeholder.
- `fetchPOICategory(cat)` does the fetch + nearest-ahead computation and caches; `renderPOIResult(result)` paints a cached/fresh result into the box. The 60s interval (`updateRestStop`) keeps the currently-viewed category fresh.

**Accuracy — distance & exit (`enrichCurrentPOI`):** the cached result first shows straight-line distance and **no exit**, then the *currently-viewed* category is upgraded in the background:
- **Distance → real road miles** via OSRM (`osrmRoadDistance`, `router.project-osrm.org`). Straight-line under-reports vs the road; on any OSRM failure it keeps the straight-line value (so it never regresses).
- **Exit → the real highway exit number** via the nearest `highway=motorway_junction` to the POI (`nearestExit`, Overpass, within ~3 km). Only the rest-area category has `exits: true` — Cracker Barrel / Walmart / BLM never show an exit. **The POI's own `ref` tag is NOT the exit number** — using it was the cause of the wrong exits; that's been removed.
- Enrichment is lazy (only the viewed category), guarded per-cache-entry (`_enriching` / `enriched` / `enrichTs`), and re-cached so the APIs aren't hit on every render.

### Engine Panel (OBD-II)
Displayed in the drive Speed tab below the speedometer. Shows live data from the Sprinter's OBD-II port via the vGate iCar Pro BT3 adapter:
- RPM, speed (mph), coolant temperature (°F), fuel level (%), **accelerator pedal (%)**, battery voltage (V)
- The "Accelerator" bar is fed from `accel-pos` (ACCELERATOR_POS_D / pedal), **not** `throttle-pos` — on this diesel THROTTLE_POS reads a stuck ~13%, so the pedal PID is the real driver input. `accel-pos` is fast-polled (2s) for responsiveness.
- MIL (check engine light) indicator and DTC fault code list
- Data published by `obd-bridge.py` via MQTT to `van/status/obd/*`

### Voice Control Pipeline (bridge + key)
Two entry points open the **same full-screen overlay** (`#voiceOverlay`, `startVoiceListen()`): the **center 🎤 button** in the normal-mode bottom tab bar (`.tab-voice`) and the **Voice button in the drive-mode nav** (`#driveNavVoice`).

- **Full-screen, manual-close UX:** the overlay takes the whole screen and **only the X button closes it** (`stopVoiceListen()`) — there is no tap-outside or timed auto-close (this was the "got stuck, couldn't close it" fix). Live feedback: interim+final transcript streams into `#voiceTranscript`, status into `#voiceStatus`, mic state via `.voice-mic` (`pulsing` listening / `thinking` / `error`). After a reply **or** an error the overlay stays open and shows the **"🎤 Speak again"** button (`#voiceAgain`), hidden again at the start of each listen.
- **Speech-to-text** is the iPhone's built-in recognition (`webkitSpeechRecognition`) — needs no key, but requires the **HTTPS** dashboard (mic is blocked on plain `http://`, same as GPS).
- The transcript + the list of controllable lights/scenes/colors is published to `van/voice/request`. **`voice-bridge.py`** on the Pi (holds the Anthropic key in `~/.anthropic_key`, **never in the webpage**) sends it to **Claude** (`claude-opus-4-8`, forced tool use `van_controls`) and publishes a structured `{actions, reply}` back on `van/voice/response`.
- The dashboard's `executeVoiceActions()` / `applyVoiceAction()` map each action to the existing control functions (lights, AC mode/fan/setpoint, pump, tank heater, awning, rope color/effect/brightness, scenes, drive mode, Starlink). It speaks the `reply` confirmation.
- **Setup (once):** `echo 'sk-ant-...' > ~/.anthropic_key && chmod 600 ~/.anthropic_key` on the Pi, then `sudo systemctl restart voice-bridge`. Without the key, the bridge returns `{error}` and the dashboard shows it. For faster/cheaper commands, set `MODEL = "claude-haiku-4-5"` in `voice-bridge.py`.

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

The dashboard has 26 visual color themes (Night, Day, Desert, Ocean, Forest, Neon, etc.). A theme button (palette icon) opens a bottom sheet picker. Themes are saved to `localStorage` key `theme`. The picker swatches **wrap** (don't cram into one nowrap row). The sheet is **capped at `max-height:85vh` with `overflow-y:auto`** so it never fills the whole screen (which would hide the tap-to-close backdrop). It closes by tapping the backdrop, tapping the grab handle, or **dragging the sheet down** (`sheetTouchStart/Move/End` — drag engages only when scrolled to the top; >110px dismisses, else snaps back).

**Secondary-text contrast:** `applyTheme()` nudges each theme's `--txt2` ~32% toward `--txt` (`_mixHex`) so dim labels are readable on every theme — done centrally instead of editing all 26 themes' `vars`.

### Drive-mode fun facts → CarPlay
The explore "fun facts" play through an **`<audio>` element** (TTS via Google translate_tts, chunked), because on iOS `<audio>` media routes to **CarPlay / car speakers** whereas `speechSynthesis` stays on the phone. Falls back to `speechSynthesis` if the audio source fails (e.g. no internet). `speakLocalFact()` → audio first, `speakFactViaSynthesis()` is the fallback. (Best-effort: true CarPlay routing is an iOS behavior; if Google TTS proves flaky, move TTS onto the Pi.)

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
- SSH from **iPhone** (via Tailscale): `ssh sgordon1024@100.98.52.107` (password: `windows`)
- SSH from **Mac** (Apple Pi LAN only): `ssh sgordon1024@192.168.8.106` — Mac CANNOT use Tailscale IP

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

### Sleep Timer (server-side)
The Climate tab has a stepped **Sleep Timer** slider (`AC_TIMER_STEPS`: Off, 5, 10, 15, 20, 30, 45, 60, 90, 120, 180, 240 min). It runs **server-side in `can-bridge.py`** (a `threading.Timer`) so the AC turns off at the scheduled time even when the phone is asleep / the dashboard is closed — a browser `setTimeout` would not fire then.
- Dashboard publishes minutes to `van/ac/timer` (`0` cancels). The bridge schedules the off, then publishes the **epoch-ms end time** to `van/status/ac/timer` (retained), or `""` when cancelled/fired.
- On expiry the bridge sends `System OFF` and publishes `van/status/ac/mode=off` + clears the timer status.
- Manually turning the AC off (`van/ac/mode=off`) cancels any pending timer. A bridge restart clears the timer (in-memory) and its retained status, so the dashboard won't show a countdown that can't fire.
- Dashboard shows a live `H:MM:SS` countdown computed from the retained end-time (`updateClimTimerCountdown`).

### Sleep Cycle (server-side duty cycling)
The Climate tab also has **Sleep Cycle** preset buttons (Off / Light `20·40` / Medium `30·30` / Strong `30·20`). The AC's auto/thermostat mode is useless when the bathroom door blocks the temp sensor (it never reaches setpoint, so it runs on high all night). The cycle is **sensor-independent**: `can-bridge.py` alternates `Cool ON` (on **Fan LOW**) for the on-minutes, then `System OFF` for the off-minutes, repeating, via chained `threading.Timer`s — so it keeps cycling while the phone is asleep.
- Dashboard publishes `van/ac/cycle` = `"ON/OFF"` minutes (e.g. `30/20`), or `off` to stop. Bridge publishes the active spec to `van/status/ac/cycle` (retained), `""` when off.
- Each on-phase sends Cool ON **and Fan LOW** (and publishes `van/status/ac/mode=cool` + `van/status/ac/fan=low`); each off-phase sends System OFF.
- A **manual mode change** (`van/ac/mode` cool/off) cancels the cycle. The **sleep timer firing** also cancels it. A bridge restart clears it (in-memory) and its retained status.

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
| `van/ac/timer` | Dashboard → Bridge | minutes until AC auto-off (`0` cancels) |
| `van/ac/cycle` | Dashboard → Bridge | duty cycle `"ON/OFF"` minutes (e.g. `30/20`); `off`/`0` cancels |
| `van/rope-light/power` | Dashboard → rope-light.py | `on`, `off` |
| `van/rope-light/color` | Dashboard → rope-light.py | `red`, `orange`, `amber`, `yellow`, `lime`, `green`, `teal`, `cyan`, `sky`, `blue`, `navy`, `purple`, `pink`, `white` |
| `van/rope-light/brightness` | Dashboard → rope-light.py | `1`–`100` |
| `van/rope-light/effect` | Dashboard → rope-light.py | `cycle` (software color cycling) |
| `van/rope-light/speed` | Dashboard → rope-light.py | `1`–`10` (cycle speed) |
| `van/starlink/power` | Dashboard → starlink-bridge.py | `on`, `off` |
| `van/starlink/auto` | Dashboard → starlink-bridge.py | `on`, `off` |
| `van/starlink/threshold` | Dashboard → starlink-bridge.py | `0`-`50` min T-Mobile **download Mbps** required to prefer/switch-back to T-Mobile (default 5; `0` = any working T-Mobile) |
| `van/network/speedtest` | Dashboard → run-speedtest.py | `run` (full manual test) or `lite` (small low-data test) |
| `van/network/driving` | Dashboard → starlink-bridge.py | `on`/`off` — drive mode (weights Starlink more) |
| `van/voice/request` | Dashboard → voice-bridge.py | JSON `{transcript, lights, scenes, colors}` — voice command |

| Topic (publish, retained) | Direction | Payload |
|---|---|---|
| `van/status/light/{name}` | Bridge → Dashboard | `off`, `1`–`100` |
| `van/status/ac/mode` | Bridge → Dashboard | `cool`, `off` |
| `van/status/ac/fan` | Bridge → Dashboard | `high`, `low`, `auto` |
| `van/status/ac/setpoint` | Bridge → Dashboard | integer °F |
| `van/status/ac/temp` | Bridge → Dashboard | integer °F |
| `van/status/ac/timer` | Bridge → Dashboard | epoch-ms when AC auto-off fires, or `""` if none |
| `van/status/ac/cycle` | Bridge → Dashboard | active duty cycle `"ON/OFF"` (e.g. `30/20`), or `""` if none |
| `van/status/starlink/power` | starlink-bridge → Dashboard | `on`, `off`, `unknown` |
| `van/status/starlink/auto` | starlink-bridge → Dashboard | `on`, `off` |
| `van/status/starlink/threshold` | starlink-bridge → Dashboard | min T-Mobile download Mbps to prefer T-Mobile (0–50) |
| `van/status/starlink/quality` | starlink-bridge → Dashboard | `good`, `poor`, `unknown` |
| `van/status/network/upstream` | starlink-bridge → Dashboard | `tmobile`, `starlink` |
| `van/status/network/speedtest` | run-speedtest → Dashboard | JSON: `{download, upload, ping, server, upstream, timestamp, error}` |
| `van/status/network/speedtest/running` | run-speedtest → Dashboard | `true` / `false` |
| `van/status/network/alert` | starlink-bridge → Dashboard | warning text (e.g. both WANs down) → red toast; empty string clears |
| `van/voice/response` | voice-bridge → Dashboard | JSON `{actions:[…], reply}` or `{error}` (not retained) |
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

Speed test results are stored in **`localStorage` key `gogovan-speed-history`** as a JSON array. Each entry: `{ts, isoTs, upstream, down, up, ping, server, lat, lng}`. Max 500 entries (oldest pruned on save). **Automatic tests run every 4 hours** via `speedtest.timer` (`OnUnitActiveSec=4h`) — ≈9 GB/month (~49 MB/test on T-Mobile, ~150 MB on the faster Starlink). Each test runs on whichever WAN is active and tags the result `upstream` (detected live via `nmcli`, recognizing `preconfigured`=tmobile and `PhiladelphiaCollins`=starlink).

**Manual speed test → failover:** when you tap "Test Now", `starlink-bridge.py` watches the result and switches sources if the current link is bad — i.e. an **error with a failed ping** (genuinely no internet, never a tooling hiccup) **or a download below `MANUAL_BAD_MBPS` (2 Mbps)**. If the other source ALSO has no usable internet, it publishes `van/status/network/alert` and the dashboard shows a red warning toast (`showNetworkAlert`). The bridge only reacts to *manual* tests (gated by a recent `van/network/speedtest=run`), not the periodic ones. Both the manual tap (via `can-bridge.py`) and the timer run the same `run-speedtest.py` (Ookla binary) — the old broken `speedtest-cli` path is gone.

**Manual speed test → T-Mobile recheck (prefer-default):** T-Mobile is the preferred source; Starlink is only the fallback. So when a manual test is run **while on Starlink and the Starlink link tests OK**, the bridge also fires `switch_to_tmobile("manual test: prefer T-Mobile", min_mbps=min_speed)` (if `auto_mode` and not `manual_override`). That connects to T-Mobile, pings, then **measures T-Mobile's real download** and switches back to it (powering the dish off) only if it meets `min_speed` Mbps — otherwise it reverts to Starlink on its own (never strands). Both the manual recheck and the automatic 20-min recheck now use this same speed gate; **neither depends on the T-Mobile signal scan anymore** (it reads `-1` on Starlink 5 GHz and used to block the auto-recheck entirely). Tapping "Test Now" forces an immediate T-Mobile speed recheck.

**Driving: small tests every 15 min + Starlink-weighted.** In drive mode the dashboard publishes `van/network/driving=on` and fires a **lite** speed test (`van/network/speedtest=lite`) ~30s in and every 15 min. `run-speedtest.py --lite` does a ~4 MB Cloudflare down/up (vs Ookla's ~150 MB) — small enough to run often while driving. starlink-bridge treats `driving=on` by **weighting Starlink more**: skips the periodic T-Mobile recheck, keeps the dish powered (no power-saving off), fails over to Starlink after **2** bad checks instead of 3, and won't switch back to T-Mobile on a lite-test-OK. Exiting drive mode publishes `driving=off` and stops the tests. Rationale: while driving, switching matters most and power doesn't.

GPS is captured with `navigator.geolocation.getCurrentPosition()` (8s timeout, 2min cache) at the time of each test result and stored as `{lat, lng}` in the history entry. Each result in the stats list links to `maps.apple.com/?ll=lat,lng`.

---

## Stats Overlay (All-Time Internet Stats)

Opened via "View All-Time Stats" button on the Internet tab. Renders as a full-screen overlay with:
- **Carrier filter**: All / T-Mobile / Starlink
- **SVG polyline chart**: amber=T-Mobile (solid=download, dashed=upload), blue=Starlink. Plots daily averages (aggregated by `aggregateDailyStats()`) to keep the DOM lean.
- **Test Results list**: Sorted newest-first, 50 entries per page with "Load more". Shows carrier badge, date, speeds, ping, and GPS link.

---

## Coverage Map (speed tests plotted by location)

Opened via the **"Coverage Map"** button on the Internet tab (`openCoverageMap()`), a full-screen overlay (`#mapOverlay`) that plots every GPS-tagged speed test on a map so you can see where you have good internet.

- **Leaflet, lazy-loaded from CDN** (`unpkg.com/leaflet@1.9.4`) only when the map is first opened — keeps initial page load fully offline-capable. If there's no internet, it shows "Map needs an internet connection to load." (Tiles inherently need internet.)
- **Theme-aware tiles** via `_isDarkMapTheme()` (luminance of `--bg`): CARTO `dark_all` on dark themes, `voyager` on light. Re-applied on every open (`_applyCovTheme`).
- **Clustering** (`clusterSpeedEntries`, greedy distance-based, `COV_CLUSTER_M = 250 m` using `haversineM`): co-located tests group into one pin so a place tested on **both** carriers shows a single pin with **two numbers** — amber (T-Mobile) + blue (Starlink), each the average download (Mbps) at that spot. Single-carrier spots show one colored pill.
- **Markers** are Leaflet `divIcon`s (`.cov-marker` / `.cov-seg.tmo|.sl|.unknown`); tapping one opens a popup (`coveragePopupHtml`) with per-carrier avg ↓/↑/ping, test count, and best download.
- **Carrier filter** (All / T-Mobile / Starlink) via `setCoverageFilter()`; `_covFilter` drives `renderCoverageMarkers()`.
- Entries without `lat`/`lng` are excluded (empty state prompts to run a test with location enabled). Map auto-fits to the plotted points (`fitBounds`).

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

`starlink-bridge.py` runs as `starlink-bridge.service` on the Pi. It is the **single
authority** for dual-WAN failover — it directly switches the Pi's `wlan0` between the
two NetworkManager profiles (`preconfigured` = T-Mobile, `PhiladelphiaCollins` =
Starlink) via `sudo nmcli connection up <name>`, and controls the Starlink dish power
via a Tuya plug.

**It does NOT touch the GL.iNet's config** (no UCI/SSH to the router). The GL.iNet just
provides the LAN and routes clients to the Pi (DHCP option 3 → 192.168.8.106). All WAN
switching is the Pi's `wlan0`.

**Tuya smart plug (dish power) — SELF-HEALING across re-pairs:**
- Cloud name **"Smart Socket 3"** (`PLUG_CLOUD_NAME`), version **3.3**, IP **192.168.8.248**
  (MAC `fc:67:1f:dd:67:b2`, reserved via GL.iNet DHCP; also auto-discovered via `tinytuya.deviceScan()`).
- Re-pairing the plug in the Smart Life app **rotates both its local id and key**. To handle this,
  `starlink-bridge.py` calls `refresh_plug_creds()` on startup: it pulls the current id+key from the
  **Tuya cloud** (matched by `PLUG_CLOUD_NAME`), caches them to `~/.starlink_plug_creds`, and falls
  back to that cache, then the hardcoded `PLUG_DEV_ID`/`PLUG_LOCAL_KEY`, if the cloud is unreachable.
- **So after a re-pair, just restart the bridge — no code change:**
  `ssh sgordon1024@192.168.8.106 'echo windows | sudo -S systemctl restart starlink-bridge'`
- Current (auto-managed) values: id `eb826ee30e0fd77018gwq2`, key `HlYX{/Y-Pv-M':)7`.
- If you **rename** the plug in the app, update `PLUG_CLOUD_NAME` to match.
- Cloud API creds live in `~/tinytuya.json` (apiKey/apiSecret/apiRegion). Re-run `python3 -m tinytuya wizard` if they ever expire.
- Plug control is **best-effort**: if the plug is unreachable, failover still happens (the dish may
  already be powered); only the power-saving toggle-off is lost.

**Failover logic (Peplink-style, INTERNET-based — never signal-bars alone):**
- **T-Mobile is the default.** When happily on T-Mobile, the Starlink dish is powered off. The
  control loop does this **proactively**: after T-Mobile internet is stable for ~3 checks (~60s),
  if the plug is on it powers the dish off (`tmobile_stable_count >= 3`) — not just on the
  switch-back transition. Gated by `auto_mode` and suppressed during manual override. To keep the
  dish on while on T-Mobile (e.g. pre-warming), turn auto off.
- Health check every **20s** = real ping to 8.8.8.8 / 1.1.1.1 through the active link.
- On T-Mobile, **3 consecutive failed checks (~60s)** → power dish on, wait for warmup
  (Starlink SSID to appear, up to 180s), switch routing to Starlink, verify.
- **Inverter auto-on for Starlink:** the dish runs off the inverter's AC, so `switch_to_starlink()`
  calls `ensure_inverter_on()` first — publishes `W/{PORTAL_ID}/vebus/276/Mode = {"value":3}` (On)
  before powering the Tuya plug, then retries the plug for ~40s while it boots/rejoins Wi-Fi. So if
  the user turned the inverter off, failover turns it back on to power Starlink. (PORTAL_ID `c0619ab5dcfb`,
  same as the dashboard.) The inverter is **not** auto-turned-off afterward.
- On Starlink, every **20 min** (`TMOBILE_RECHECK_INTERVAL`): briefly switch to T-Mobile and
  **measure its real download speed** (`measure_download_mbps()` — a ~3 MB Cloudflare download, same
  as the lite test). If T-Mobile delivers **≥ `min_speed` Mbps** (the `threshold` topic, default 5,
  range 0–50) → stay on T-Mobile + power dish off. If it's **slower** → fall back to Starlink (no
  flapping). The speed gate lives in `switch_to_tmobile(..., min_mbps=min_speed)`; an inconclusive
  measurement (`-1`) keeps T-Mobile since ping already passed (a tooling hiccup never strands us).
  **There is no signal pre-gate anymore** — the old `sig >= min_signal` check blocked this recheck
  because `get_tmobile_signal()` reads `-1` on Starlink 5 GHz. If Starlink itself fails → try
  T-Mobile right away with **no** speed gate (any working T-Mobile beats a dead Starlink).
  Note: the T-Mobile **signal** readout (`van/status/starlink/tmobile-signal`) is still scanned and
  shown on the dashboard — it's display-only now, not a switching input.
- **Manual override** (`van/network/upstream` = tmobile/starlink) forces a side and
  suspends auto for 30 min. Never powers the dish off unless the target's internet is
  confirmed (no stranding).
- `auto_mode` is persisted to `~/.starlink_auto` (survives restart); default ON.

**Why decisions are internet-based, not signal-based:** the original bug was switching
on signal bars. T-Mobile's MiFi shows full bars even when its upstream is dead, so the
old logic (and the old watchdog) kept dumping the van onto a T-Mobile with no internet.
Every switch decision now requires an actual ping test to pass.

**5 GHz lock (speed fix):** `PhiladelphiaCollins` is pinned to 5 GHz in its NM profile
(`802-11-wireless.band a`). The Pi was associating on Starlink's 2.4 GHz (~24 Mbps cap);
forcing 5 GHz raised the Pi→Starlink link to ~88 Mbps. Trade-off: 5 GHz has shorter range.
If the connection gets flaky after moving the van, revert with:
`sudo nmcli connection modify PhiladelphiaCollins 802-11-wireless.band "" && sudo nmcli connection up PhiladelphiaCollins`

**History: Why tuya-convert was abandoned:**
- tuya-convert hijacks `wlan0` entirely (creates a hostapd AP), losing Tailscale connectivity
- All Pi ports (80, 443, 53, 1883) are occupied by the dashboard stack
- Recovery required physical access to power-cycle the Pi
- `rm -rf ~/tuya-convert` — do NOT attempt this approach again

---

## OBD-II Integration (obd-bridge.py)

`obd-bridge.py` connects to the vGate iCar Pro BT3 Bluetooth OBD adapter via `/dev/rfcomm0` (Bluetooth Classic SPP, bound by `rfcomm-obd.service`).

**Connected & installed (Jun 2026):**
- Adapter Bluetooth name **`V-LINK`**, Classic MAC **`10:21:3E:4F:04:B4`**, pairing **PIN `1234`** (the nearby `10:21:3E:50:04:B4` "BLE Device" is the same unit's BLE side — not used).
- Paired + trusted in BlueZ; `rfcomm-obd.service` binds `/dev/rfcomm0` to that MAC on boot; `obd-bridge.service` (After/Requires rfcomm-obd) runs the bridge. Both **enabled** (start on boot).
- `python-obd` installed (`pip3 install --break-system-packages obd`); user `sgordon1024` is in the `dialout` group.
- Verified live at idle: rpm ~770, coolant 167°F, voltage 13.6V, fuel 76%, MIL off.
- **Re-pair note:** if the adapter is reset/re-paired, its MAC may change — re-run `pi-setup/setup-obd.sh` (it scans, pairs with PIN 1234, rebinds). To pair manually, scan + pair in ONE `bluetoothctl` session (the device goes "not available" once scanning stops) and feed `1234` when it asks for the PIN.
- `deploy-to-pi.sh` now copies `obd-bridge.py` and restarts the service.

**Data published (all retain=True), under `van/status/obd/`:**
- `connected` (ok/searching/error), `rpm`, `speed` (mph), `coolant-temp` (°F), `fuel-level` (%),
  `throttle-pos` (%), `voltage` (V), `mil` (on/off), `dtcs` (JSON array)
- **Derived/added (Jun 2026):** `engine-load` (%), `fuel-rate` (gph), `mpg` (instant = speed÷fuel-rate, 0 at idle),
  `avg-mpg` (rolling EMA, updates only while moving ≥10 mph), `range` (miles to empty), `fuel-remaining` (gal),
  `oil-temp` (°F), `ambient-temp` (°F), `run-time` (s), `barometric` (kPa), `accel-pos` (%),
  `distance-mil` (mi), `distance-since-clear` (mi)

**Range / distance-to-empty math** (`obd-bridge.py`): `fuel-remaining = fuel% × TANK_GALLONS (24.5)`;
`range = fuel-remaining × avg-mpg`. `avg-mpg` is an EMA (α=0.05) of instant MPG, seeded with `DEFAULT_MPG=18`
until it converges from real driving. The vehicle exposes 89 PIDs total (`conn.supported_commands`); we poll the
useful subset. **Poll rates:** fast gauges + accelerator + MPG/range every 2s; slow values + MIL/DTCs every 30s.
**Range smoothing:** `range` is an EMA (`_range_ema`, α=0.1) of `fuel × avg_mpg`, **rounded to the nearest 5 mi**, so the dashboard distance-to-empty stays steady instead of jumping every poll. The explore-tab number (`renderExploreRange`) is crisp — no glow/blur, tight letter-spacing.

**Dashboard:** the drive-mode **Engine panel** shows RPM/Fuel/Coolant/Alternator + **Engine Load + MPG**, with an
**"All engine data →"** button opening a full overlay (`#obd-all-overlay`, `renderObdEverything()` from the `obdData`
cache). The **Explore panel top-right shows distance-to-empty** (`renderExploreRange()`, color-ramped red→white by
miles left) instead of mph — `refreshModeChip()` no longer writes speed there. OBD data only updates with the ignition on.

---

## Network Resilience

The Pi has two internet paths via wlan0. Failover is handled entirely by
`starlink-bridge.py` (see "Starlink Automation" above).

**`gogovan-watchdog.timer` — DISABLED (do not re-enable).**
- It was the old failover mechanism: every 2 min, on ping failure it flipped wlan0 to
  the "other" connection. It still referenced the dead name `wifi-blaster`, so once
  Starlink was renamed to `PhiladelphiaCollins` it would dump the van onto dead T-Mobile
  and be unable to get back — a flapping loop. It also fought `starlink-bridge` for
  control of wlan0.
- `deploy-to-pi.sh` runs `systemctl disable --now gogovan-watchdog.timer` on every deploy.
- Its only other job (restart Tailscale if not Running) is minor; fold into starlink-bridge
  later if needed. Manual Tailscale recovery steps are below.
- Script still on disk at `/usr/local/bin/gogovan-watchdog.sh` for reference; inert.

**NM dispatcher scripts in `/etc/NetworkManager/dispatcher.d/`:**
- `99-clean-routes` — when any interface comes up, removes wlan0 default routes with metric < 200 (belt-and-suspenders backup for the ignore-auto-routes fix)
- `99-gogovan-nat` — when wlan0 comes up, installs iptables forwarding rules for uap0→wlan0 (legacy hotspot NAT, harmless to keep)

**If Tailscale goes offline (manual recovery):**
```bash
ssh sgordon1024@192.168.8.106   # must be on Apple Pi network
sudo systemctl restart tailscaled
sleep 5
sudo tailscale up               # NO --reset flag
tailscale status                # should show vanpi as connected
```

**If Pi has no internet (manual recovery):**
```bash
# Check which connection is active
nmcli -g NAME,DEVICE connection show --active | grep wlan0
# Switch to the other one
sudo nmcli connection up PhiladelphiaCollins   # or: preconfigured
ping -c 3 8.8.8.8
```

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
        # Force revalidation so the iOS home-screen app picks up deploys without
        # reinstalling. nginx returns 304 when unchanged, full file after a deploy.
        # If it ever still serves stale, change this to "no-store".
        add_header Cache-Control "no-cache, must-revalidate" always;
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
With a bridge configured to forward Victron telemetry from Cerbo at 192.168.8.147:1883 (Apple Pi).

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

**Why the bottom bars use `left:0; right:0` (NOT `left:50%; transform:translateX(-50%)`):**  
Both `.tab-bar` and `#drive-nav` are `position:fixed; bottom:0`. They used to also have `left:50%; transform:translateX(-50%)` (pointless centering — `width:100%` already spans full width). On iOS Safari, a `position:fixed` element that has **its own `transform`** is mis-positioned on the **first paint** and only snaps to `bottom:0` after a repaint is forced — so on launch the bar floated above the bottom edge, then jumped down the moment you tapped a tab (which reflows via `switchTab`). Removing the transform and pinning with `left:0; right:0` fixes it. Do NOT re-add a `transform` to these bars. (The `.starlink-quality-banner` keeps its `translateX(-50%)` because its width is `calc(100% - 32px)` and it genuinely needs centering — that one is fine since it's not the element fighting `bottom:0` on load.)

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
