# Stephen's Umbrel Community App Store

Add this repository's URL in umbrelOS: App Store → ⋯ → Community App Stores.

## Apps

- **qBittorrent VPN** (`stephen-qbittorrent-vpn`): qBittorrent inside Gluetun's network namespace, tunnelled through Private Internet Access (OpenVPN, port forwarding, CA Montreal/Toronto). Gluetun's firewall is the kill switch; extra iptables rules block new connections from the tunnel namespace to private address ranges.

No secrets live in this repository. PIA credentials are files created on the server outside app-data at `/home/umbrel/umbrel/secrets/pia/` (`openvpn_user`, `openvpn_password`, root-owned, mode 600).
