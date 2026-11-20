# Xray

A small Xray client for GNOME Mobile and Phosh. It runs Xray with its native TUN
inbound, so the whole phone is routed through the proxy, and a hotspot shared by
NetworkManager goes through it too.

## Features

- GTK4 and libadwaita, made for a phone screen
- One big connect switch, live state and connection time
- Servers: VLESS, VMess, Trojan and Shadowsocks with TCP, WebSocket or gRPC transport
- TLS and Reality, with SNI, ALPN, uTLS fingerprint, public key and short ID
- Paste a `vless://`, `vmess://`, `trojan://` or `ss://` share link, or fill in the fields
- Copy the current server back out as a share link
- Phosh quick settings toggle: tap to connect or disconnect, long press opens the app
- TUN settings: MTU, DNS servers, IPv6 routing, connect on boot
- Root daemon owns the Xray process and the system DNS; the app and the quick
  setting only talk to it over D-Bus

## Install on the phone

```sh
scp -r xray-mobile alarm@10.151.90.30:~/xray-mobile
ssh alarm@10.151.90.30
cd ~/xray-mobile
./install.sh
```

Phosh loads plugins when the session starts, so reboot (or log out and back in)
to get the quick setting. The phosh unit on this image refuses manual restarts.
The installer needs `meson`, `ninja` and the phosh sources to build the quick
setting. It downloads the matching phosh source tarball if `PHOSH_SRC` is not set,
and downloads the Xray core release for the phone's architecture into
`/usr/local/lib/xray-client/xray`.

To skip the quick setting build, run `NO_PLUGIN=1 ./install.sh`.

## How it works

- `xray-clientd` runs as root as a system service and owns `/etc/xray-client/config.json`
- Connecting writes a generated config to `/run/xray-client/xray.json`, starts
  `xray run`, sets `/etc/resolv.conf` to the TUN gateway and restores it on exit
- Xray routes `0.0.0.0/0` (and optionally `::/0`) into the TUN interface, binds its
  own outbound connections to the physical interface, and sends DNS into its DNS outbound
- The quick setting calls `Connect`/`Disconnect` and watches the daemon's D-Bus
  properties, so it stays in sync with the app

## Notes

- The phone's DNS is pointed at `10.18.0.2` while connected. If another tool rewrites
  `/etc/resolv.conf`, DNS can leak until the next reconnect.
- If the network changes, Xray rebinds to the new interface automatically; there is
  no need to reconnect for that.
- Share the connection with the separate hotspot app. The hotspot's clients are NATed
  through the phone and therefore through the tunnel.

## Credits

- Xray core: [Project X (XTLS)](https://github.com/XTLS/Xray-core)
- Phosh: [Phosh contributors](https://gitlab.gnome.org/World/Phosh/phosh)
- GTK and libadwaita: [GNOME contributors](https://www.gnome.org)
