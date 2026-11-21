# Stephen's Umbrel Community App Store

Add this repository's URL in umbrelOS: App Store → ⋯ → Community App Stores.

## Apps

- **Sonarr / Radarr / Prowlarr** (`stephen-sonarr`, `stephen-radarr`, `stephen-prowlarr`): the official Umbrel images, repackaged without the Transmission dependency and Umbrel's media-app-configurator, so qBittorrent VPN is the only download client.
- **qBittorrent VPN** (`stephen-qbittorrent-vpn`): qBittorrent inside Gluetun's network namespace, tunnelled through Private Internet Access (OpenVPN, port forwarding, CA Montreal/Toronto). Gluetun's firewall is the kill switch; extra iptables rules block new connections from the tunnel namespace to private address ranges.

- **Media Archiver** (`stephen-media-archiver`): nightly job that moves idle movies and ended series from the internal drive to the WD USB archive via Radarr/Sonarr, fetches subtitles first (Bazarr) and restores Jellyfin watch status. Ships in dry-run mode (`DRY_RUN=true` in its compose). Reads API keys read-only from the apps' own configs; the Jellyfin key is the one named `media-archiver`. Tag a title `keep` to exclude it.

No secrets live in this repository. PIA credentials are files created on the server outside app-data at `/home/umbrel/umbrel/secrets/pia/` (`openvpn_user`, `openvpn_password`, root-owned, mode 600).
