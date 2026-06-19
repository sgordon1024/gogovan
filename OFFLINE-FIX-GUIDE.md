# Offline Fix Guide — Apple Pi Internet

Keep this open on a device that is NOT switching networks (or just on the Mac — it's a local file, works offline).

---

## What we know for sure

- The **Pi has internet** via Starlink (wlan0 → 192.168.1.43, ping to 8.8.8.8 works).
- The **Pi correctly forwards & NATs** traffic — PROVEN, because when the Mac's gateway was manually set to `192.168.8.106`, the Mac got online.
- The **only thing missing**: the GL.iNet router isn't telling devices to send internet traffic to the Pi. It points them at itself (192.168.8.1), which has no working upstream.
- Auto-switch is currently **OFF**, so it will stay on Starlink. Good.

**The whole fix = tell the GL.iNet to hand out the Pi (192.168.8.106) as the gateway.**

---

## STEP 1 — The main fix (do this first)

### Easiest: GL.iNet admin → LuCI

1. On any device on **apple pi**, open a browser to: **http://192.168.8.1**
2. Log in (your GL.iNet admin password).
3. Go to **System → Advanced Settings** (this opens "LuCI"). Log in again if asked — username `root`, same password.
4. In LuCI top menu: **Network → Interfaces**.
5. Find **LAN**, click **Edit**.
6. Click the **DHCP Server** tab, then the **Advanced Settings** sub-tab.
7. Find the **DHCP-Options** field. Click the **+** and add these two entries:
   - `3,192.168.8.106`   ← sets the gateway to the Pi
   - `6,8.8.8.8,1.1.1.1` ← sets DNS
8. Click **Save**, then **Save & Apply**.

### OR, by SSH (faster if you're comfortable)

⚠️ **These `uci` commands ONLY work on the GL.iNet router, NOT the Pi.**
The Pi runs Debian and has no `uci` command. You must connect to the router:

```bash
ssh root@192.168.8.1          # the GL.iNet ROUTER — password = your GL.iNet admin password
```
Then paste:
```bash
uci add_list dhcp.lan.dhcp_option='3,192.168.8.106'
uci add_list dhcp.lan.dhcp_option='6,8.8.8.8,1.1.1.1'
uci commit dhcp
/etc/init.d/dnsmasq restart
```

| Box | SSH command | Password | What runs here |
|---|---|---|---|
| **GL.iNet router** | `ssh root@192.168.8.1` | GL.iNet admin pw | `uci ...` DHCP options |
| **Raspberry Pi** | `ssh sgordon1024@192.168.8.106` | `windows` | `nmcli`, `mosquitto_pub`, speed test |
| **Your Mac** (on apple pi) | _(no ssh — local terminal)_ | — | the `route` stopgap |

---

## STEP 2 — Reconnect and test

The gateway only updates when a device gets a fresh DHCP lease, so:

1. On each device: turn **Wi-Fi off, then on** (or forget/rejoin apple pi).
2. Open any website.

Confirm the gateway changed (on Mac):
```bash
route -n get default | grep gateway
```
Should say `gateway: 192.168.8.106`.

---

## STEP 3 — If internet works but is SLOW (diagnose Starlink)

The 1 Mbps we saw might be the Pi's Wi-Fi link to Starlink, not the routing.
SSH into the **Pi** and paste this whole block:

```bash
echo "=== Starlink speed measured directly on the Pi ==="
curl -s -o /dev/null -w "%{speed_download}" "https://speed.cloudflare.com/__down?bytes=25000000" \
  | awk '{printf "Download: %.1f Mbps\n", $1/125000}'
echo "=== Pi Wi-Fi link to Starlink (signal + bitrate) ==="
iw dev wlan0 link | grep -E "signal|tx bitrate|SSID"
```

**How to read it:**
- If the **Pi's own download is also ~1 Mbps** → the bottleneck is the Pi↔Starlink Wi-Fi link (weak signal) or Starlink itself. Move the Pi closer to the Starlink router, or we wire the Pi to Starlink by ethernet later.
- `signal:` worse than about **-70 dBm** = weak. `tx bitrate:` under ~50 Mbps = poor link.
- If the **Pi is fast but devices are slow** → it's the GL.iNet Wi-Fi to your devices, not Starlink.

---

## Stopgap (if STEP 1 is fussy and you just need the Mac online now)

⚠️ **Run this on the MAC ONLY — never on the Pi.** (Running it on the Pi deletes the Pi's
own internet route. If that happens, fix it with: `sudo nmcli connection up PhiladelphiaCollins`)

On the **Mac**, on apple pi:
```bash
sudo route delete default && sudo route add default 192.168.8.106
```
(This is temporary — it resets when you reconnect Wi-Fi. The real fix is STEP 1.)

---

## Handy reference

**SSH into the Pi:**
```bash
ssh sgordon1024@192.168.8.106          # password: windows
```

**Force/keep it on Starlink (already done, here if you need it again):**
```bash
sudo nmcli connection up PhiladelphiaCollins
mosquitto_pub -h localhost -t van/starlink/auto -m off
```

**Re-enable auto-switching later:**
```bash
mosquitto_pub -h localhost -t van/starlink/auto -m on
```

**Deploy dashboard changes (from the Mac, on apple pi, NOT from the Pi):**
```bash
cd /Users/stephengordon/development/gogovan && ./deploy-to-pi.sh
```

---

## Note about the Starlink smart plug (lower priority)

The plug used to live on the old **GoGoVan hotspot** (192.168.4.x). That hotspot is now
**off** (hostapd masked), so the plug has no network to join — that's why the scan can't
find it. It does NOT matter right now: Starlink is already powered on and auto-switch is off.

When we want plug power control back, we'll either re-enable the GoGoVan hotspot or move the
plug onto the apple pi network with the Tuya/Smart Life app. We'll tackle that together later.

---

## When you're back online

Reconnect to a network that reaches me and tell me:
1. Did STEP 1 get all devices online? (yes/no)
2. The output of the STEP 3 speed/​signal block.

I'll take it from there.
