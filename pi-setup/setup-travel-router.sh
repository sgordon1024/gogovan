#!/bin/bash
# Sets up Pi as a travel router:
# - wlan0 connects to upstream (T-Mobile / Starlink) via NetworkManager
# - uap0 (virtual) broadcasts GoGoVan hotspot via hostapd
# - dnsmasq provides DHCP on uap0
# - iptables NAT routes GoGoVan clients through upstream
# Run on the Pi as: bash setup-travel-router.sh

set -e
PASS="windows"
AP_SSID="GoGoVan"
AP_PASS="1234567890"
AP_IP="192.168.4.1"
AP_IFACE="uap0"

echo "=== Step 1: Install hostapd and dnsmasq ==="
echo $PASS | sudo -S apt-get install -y hostapd dnsmasq

echo "=== Step 2: Create virtual uap0 interface from wlan0 ==="
echo $PASS | sudo -S iw dev wlan0 interface add $AP_IFACE type __ap 2>/dev/null || echo "(uap0 already exists)"

echo "=== Step 3: Tell NM to ignore uap0 ==="
echo $PASS | sudo -S tee /etc/NetworkManager/conf.d/unmanaged-uap0.conf > /dev/null << 'EOF'
[keyfile]
unmanaged-devices=interface-name:uap0
EOF

echo "=== Step 4: Write hostapd config ==="
echo $PASS | sudo -S tee /etc/hostapd/gogovan.conf > /dev/null << EOF
interface=$AP_IFACE
driver=nl80211
ssid=$AP_SSID
hw_mode=g
channel=6
wmm_enabled=0
macaddr_acl=0
auth_algs=1
ignore_broadcast_ssid=0
wpa=2
wpa_passphrase=$AP_PASS
wpa_key_mgmt=WPA-PSK
rsn_pairwise=CCMP
EOF

echo "=== Step 5: Point hostapd at our config ==="
echo $PASS | sudo -S tee /etc/default/hostapd > /dev/null << 'EOF'
DAEMON_CONF="/etc/hostapd/gogovan.conf"
EOF

echo "=== Step 6: Write dnsmasq config for AP clients ==="
echo $PASS | sudo -S tee /etc/dnsmasq.d/gogovan-ap.conf > /dev/null << EOF
interface=$AP_IFACE
bind-interfaces
dhcp-range=192.168.4.2,192.168.4.50,255.255.255.0,24h
dhcp-option=3,$AP_IP
dhcp-option=6,8.8.8.8,8.8.4.4
EOF

echo "=== Step 7: Create uap0-setup systemd service (recreate at boot) ==="
echo $PASS | sudo -S tee /etc/systemd/system/uap0-setup.service > /dev/null << 'EOF'
[Unit]
Description=Create virtual uap0 AP interface from wlan0
Before=hostapd.service
After=sys-subsystem-net-devices-wlan0.device

[Service]
Type=oneshot
ExecStart=/usr/sbin/iw dev wlan0 interface add uap0 type __ap
ExecStartPost=/sbin/ip addr add 192.168.4.1/24 dev uap0
ExecStartPost=/sbin/ip link set uap0 up
RemainAfterExit=yes

[Install]
WantedBy=multi-user.target
EOF

echo "=== Step 8: Enable NAT (IP forwarding + iptables) ==="
# Enable IP forwarding
echo $PASS | sudo -S sed -i 's/#net.ipv4.ip_forward=1/net.ipv4.ip_forward=1/' /etc/sysctl.conf
echo $PASS | sudo -S sysctl -w net.ipv4.ip_forward=1

# iptables NAT rule
echo $PASS | sudo -S iptables -t nat -A POSTROUTING -o wlan0 -j MASQUERADE
echo $PASS | sudo -S iptables -A FORWARD -i uap0 -o wlan0 -j ACCEPT
echo $PASS | sudo -S iptables -A FORWARD -i wlan0 -o uap0 -m state --state RELATED,ESTABLISHED -j ACCEPT

# Save iptables rules
echo $PASS | sudo -S apt-get install -y iptables-persistent -y
echo $PASS | sudo -S netfilter-persistent save

echo "=== Step 9: Enable and start everything ==="
echo $PASS | sudo -S systemctl daemon-reload
echo $PASS | sudo -S systemctl enable uap0-setup.service hostapd dnsmasq
echo $PASS | sudo -S systemctl restart NetworkManager
echo $PASS | sudo -S systemctl start uap0-setup.service
echo $PASS | sudo -S systemctl start hostapd
echo $PASS | sudo -S systemctl restart dnsmasq

echo "=== Step 10: Add upstream Wi-Fi networks ==="
# T-Mobile hotspot — primary (priority 10, already in preconfigured profile)
echo $PASS | sudo -S nmcli connection modify preconfigured connection.autoconnect-priority 10

# WiFi Blaster (Starlink) — fallback (priority 5)
echo $PASS | sudo -S nmcli connection add type wifi con-name 'wifi-blaster' ifname wlan0 ssid 'WiFi Blaster' connection.autoconnect yes connection.autoconnect-priority 5 2>/dev/null || true
echo $PASS | sudo -S nmcli connection modify 'wifi-blaster' wifi-sec.key-mgmt wpa-psk
echo $PASS | sudo -S nmcli connection modify 'wifi-blaster' wifi-sec.psk '1234567890'
echo $PASS | sudo -S nmcli connection modify 'wifi-blaster' connection.autoconnect-priority 5

echo ""
echo "=== Done! ==="
echo "GoGoVan hotspot should be broadcasting now."
echo "Clients connect to: $AP_SSID / $AP_PASS"
echo "Pi AP address: $AP_IP"
echo "Upstream networks: T-Mobile (priority 10), WiFi Blaster/Starlink (priority 5)"
echo "Pi T-Mobile address: check with 'ip addr show wlan0'"
