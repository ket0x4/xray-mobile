"""Profiles, settings and Xray configuration generation."""

from __future__ import annotations

import base64
import json
import socket
import uuid
from urllib.parse import parse_qs, quote, unquote, urlparse

PROTOCOLS = ["vless", "vmess", "trojan", "shadowsocks"]
NETWORKS = ["tcp", "ws", "grpc"]
SECURITIES = ["none", "tls", "reality"]
SS_METHODS = [
    "aes-128-gcm",
    "aes-256-gcm",
    "chacha20-poly1305",
    "2022-blake3-aes-128-gcm",
    "2022-blake3-aes-256-gcm",
    "2022-blake3-chacha20-poly1305",
]

TUN_NAME = "xray0"
TUN_GATEWAY = "10.18.0.1/16"
TUN_GATEWAY6 = "fc00::1/64"
TUN_DNS = "10.18.0.2"

DEFAULT_SETTINGS = {
    "mtu": 1500,
    "route_ipv6": False,
    "dns": "1.1.1.1, 8.8.8.8",
    "connect_on_boot": False,
    "loglevel": "warning",
}


def new_profile() -> dict:
    return {
        "id": uuid.uuid4().hex,
        "name": "",
        "protocol": "vless",
        "server": "",
        "port": 443,
        "uuid": "",
        "password": "",
        "flow": "",
        "method": "aes-256-gcm",
        "network": "tcp",
        "security": "reality",
        "server_name": "",
        "alpn": "",
        "allow_insecure": False,
        "fingerprint": "chrome",
        "public_key": "",
        "short_id": "",
        "spider_x": "",
        "path": "/",
        "host": "",
        "service_name": "",
    }


def new_config() -> dict:
    return {"profiles": [], "active": "", "settings": dict(DEFAULT_SETTINGS)}


def _bool(value) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).lower() in ("1", "true", "yes", "on")


def _int(value, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def normalize_profile(profile: dict) -> dict:
    base = new_profile()
    base.update({k: v for k, v in profile.items() if k in base})
    base["id"] = str(base["id"] or uuid.uuid4().hex)
    base["protocol"] = base["protocol"] if base["protocol"] in PROTOCOLS else "vless"
    base["network"] = base["network"] if base["network"] in NETWORKS else "tcp"
    base["security"] = base["security"] if base["security"] in SECURITIES else "none"
    base["port"] = min(65535, max(1, _int(base["port"], 443)))
    base["allow_insecure"] = _bool(base["allow_insecure"])
    for key in ("name", "server", "uuid", "password", "flow", "method", "server_name",
                "alpn", "fingerprint", "public_key", "short_id", "spider_x", "path",
                "host", "service_name"):
        base[key] = "" if base[key] is None else str(base[key])
    return base


def normalize_config(config: dict) -> dict:
    config = config or {}
    profiles = [normalize_profile(p) for p in config.get("profiles", []) if isinstance(p, dict)]
    ids = {p["id"] for p in profiles}
    active = str(config.get("active") or "")
    if active not in ids:
        active = ""
    settings = dict(DEFAULT_SETTINGS)
    settings.update(config.get("settings") or {})
    settings["mtu"] = min(9000, max(1280, _int(settings.get("mtu"), 1500)))
    settings["route_ipv6"] = _bool(settings.get("route_ipv6"))
    settings["connect_on_boot"] = _bool(settings.get("connect_on_boot"))
    settings["dns"] = str(settings.get("dns") or DEFAULT_SETTINGS["dns"])
    level = str(settings.get("loglevel") or "warning")
    settings["loglevel"] = level if level in ("debug", "info", "warning", "error") else "warning"
    return {"profiles": profiles, "active": active, "settings": settings}


def find_profile(config: dict, profile_id: str | None = None) -> dict | None:
    wanted = profile_id or config.get("active")
    for profile in config.get("profiles", []):
        if profile["id"] == wanted:
            return profile
    return None


def display_name(profile: dict) -> str:
    return profile.get("name") or profile.get("server") or "Unnamed server"


def subtitle(profile: dict) -> str:
    proto = "SS" if profile["protocol"] == "shadowsocks" else profile["protocol"].upper()
    return f"{proto} · {profile['server']}:{profile['port']}"


def _sni(profile: dict, server: str) -> str:
    return profile.get("server_name") or ("" if _is_ip(server) else server)


def _is_ip(value: str) -> bool:
    for family in (socket.AF_INET, socket.AF_INET6):
        try:
            socket.inet_pton(family, value)
            return True
        except OSError:
            continue
    return False


class ResolveError(ValueError):
    """The proxy server address could not be turned into an IP address."""


def resolve_server(profile: dict) -> dict:
    """Replace the server name with an address so Xray never needs DNS to reach it.

    Xray resolves its DNS outbound through the proxy itself, so leaving a domain
    name here makes the first connection depend on a tunnel that needs that very
    name to come up. Failing loudly beats silently handing Xray a domain.
    """
    server = profile.get("server", "").strip()
    if not server or _is_ip(server):
        return profile
    try:
        infos = socket.getaddrinfo(server, profile["port"], type=socket.SOCK_STREAM)
    except OSError as exc:
        raise ResolveError(f"Cannot resolve {server}: {exc}") from exc
    for family in (socket.AF_INET, socket.AF_INET6):
        for info in infos:
            if info[0] == family:
                resolved = dict(profile)
                resolved["server"] = info[4][0]
                if not resolved.get("server_name"):
                    resolved["server_name"] = server
                return resolved
    raise ResolveError(f"Cannot resolve {server}: no IPv4 or IPv6 address")


def _dns_url(server: str) -> str:
    server = server.strip()
    if not server:
        return ""
    if "://" in server:
        return server
    if _is_ip(server):
        host = f"[{server}]" if ":" in server else server
        return f"https://{host}/dns-query"
    return server


def _dns_servers(value: str) -> list[str]:
    result = []
    for item in value.replace(";", ",").split(","):
        url = _dns_url(item)
        if url:
            result.append(url)
    return result


def _stream_settings(profile: dict, address: str) -> dict:
    network = profile["network"]
    security = profile["security"]
    if profile["protocol"] == "trojan" and security == "none":
        security = "tls"
    stream: dict = {"network": network, "security": security}
    sni = _sni(profile, address)

    if security == "tls":
        tls = {"allowInsecure": profile["allow_insecure"]}
        if sni:
            tls["serverName"] = sni
        alpn = [item.strip() for item in profile["alpn"].split(",") if item.strip()]
        if alpn:
            tls["alpn"] = alpn
        if profile["fingerprint"]:
            tls["fingerprint"] = profile["fingerprint"]
        stream["tlsSettings"] = tls
    elif security == "reality":
        reality = {
            "fingerprint": profile["fingerprint"] or "chrome",
            "publicKey": profile["public_key"],
            "shortId": profile["short_id"],
        }
        if sni:
            reality["serverName"] = sni
        if profile["spider_x"]:
            reality["spiderX"] = profile["spider_x"]
        stream["realitySettings"] = reality

    if network == "ws":
        ws = {"path": profile["path"] or "/"}
        if profile["host"]:
            ws["headers"] = {"Host": profile["host"]}
        stream["wsSettings"] = ws
    elif network == "grpc":
        stream["grpcSettings"] = {"serviceName": profile["service_name"]}
    return stream


def build_outbound(profile: dict, tag: str = "proxy", resolve: bool = True) -> dict:
    if resolve:
        profile = resolve_server(profile)
    address = profile["server"]
    port = profile["port"]
    protocol = profile["protocol"]
    outbound = {"tag": tag, "protocol": protocol, "streamSettings": _stream_settings(profile, address)}

    if protocol == "vless":
        user = {"id": profile["uuid"], "encryption": "none"}
        if profile["flow"]:
            user["flow"] = profile["flow"]
        outbound["settings"] = {"vnext": [{"address": address, "port": port, "users": [user]}]}
    elif protocol == "vmess":
        outbound["settings"] = {"vnext": [{
            "address": address, "port": port,
            "users": [{"id": profile["uuid"], "security": "auto", "alterId": 0}],
        }]}
    elif protocol == "trojan":
        outbound["settings"] = {"servers": [{
            "address": address, "port": port, "password": profile["password"],
        }]}
    elif protocol == "shadowsocks":
        outbound["settings"] = {"servers": [{
            "address": address, "port": port,
            "method": profile["method"], "password": profile["password"],
        }]}
    return outbound


def build_xray_config(config: dict, profile: dict | None = None, resolve: bool = True) -> dict:
    config = normalize_config(config)
    profile = profile or find_profile(config)
    if profile is None:
        raise ValueError("No server selected")
    if not profile["server"]:
        raise ValueError("Server address is missing")
    if profile["protocol"] in ("vless", "vmess") and not profile["uuid"]:
        raise ValueError("UUID is missing")
    if profile["protocol"] in ("trojan", "shadowsocks") and not profile["password"]:
        raise ValueError("Password is missing")
    if profile["security"] == "reality" and not profile["public_key"]:
        raise ValueError("Reality public key is missing")

    settings = config["settings"]
    tun = {
        "name": TUN_NAME,
        "mtu": settings["mtu"],
        "gateway": [TUN_GATEWAY],
        "autoSystemRoutingTable": ["0.0.0.0/0"],
        "autoOutboundsInterface": "auto",
        "autoSystemDnsToGateway": False,
    }
    if settings["route_ipv6"]:
        tun["gateway"].append(TUN_GATEWAY6)
        tun["autoSystemRoutingTable"].append("::/0")

    dns = _dns_servers(settings["dns"])
    return {
        "log": {"loglevel": settings["loglevel"], "access": "none"},
        "dns": {"servers": dns} if dns else {},
        "inbounds": [{"tag": "tun-in", "protocol": "tun", "settings": tun}],
        "outbounds": [build_outbound(profile, resolve=resolve),
                      {"tag": "dns-out", "protocol": "dns"}],
        "routing": {
            "domainStrategy": "AsIs",
            "rules": [{"type": "field", "inboundTag": ["tun-in"], "port": 53,
                       "outboundTag": "dns-out"}],
        },
    }


def _query_value(query: dict, *names: str) -> str:
    for name in names:
        if name in query and query[name]:
            return unquote(query[name][-1])
    return ""


def parse_share_link(text: str) -> dict:
    text = (text or "").strip()
    if not text:
        raise ValueError("Empty link")
    if text.startswith("vless://"):
        return _parse_vless(text)
    if text.startswith("vmess://"):
        return _parse_vmess(text)
    if text.startswith("trojan://"):
        return _parse_trojan(text)
    if text.startswith("ss://"):
        return _parse_ss(text)
    raise ValueError("Unsupported link. Use vless://, vmess://, trojan:// or ss://")


def _parse_vless(text: str) -> dict:
    url = urlparse(text)
    query = parse_qs(url.query)
    profile = new_profile()
    profile["protocol"] = "vless"
    profile["uuid"] = unquote(url.username or "")
    profile["server"] = url.hostname or ""
    profile["port"] = url.port or 443
    profile["name"] = unquote(url.fragment)
    profile["flow"] = _query_value(query, "flow")
    security = _query_value(query, "security") or "none"
    profile["security"] = security if security in SECURITIES else "none"
    profile["server_name"] = _query_value(query, "sni", "peer")
    profile["fingerprint"] = _query_value(query, "fp")
    profile["public_key"] = _query_value(query, "pbk")
    profile["short_id"] = _query_value(query, "sid")
    profile["spider_x"] = _query_value(query, "spx")
    profile["alpn"] = _query_value(query, "alpn")
    profile["allow_insecure"] = _query_value(query, "allowInsecure") in ("1", "true")
    profile["network"] = _query_value(query, "type") or "tcp"
    if profile["network"] not in NETWORKS:
        profile["network"] = "tcp"
    profile["path"] = _query_value(query, "path") or "/"
    profile["host"] = _query_value(query, "host")
    profile["service_name"] = _query_value(query, "serviceName")
    return normalize_profile(profile)


def _parse_vmess(text: str) -> dict:
    payload = text[8:].split("#", 1)[0]
    payload += "=" * (-len(payload) % 4)
    data = json.loads(base64.b64decode(payload).decode("utf-8"))
    profile = new_profile()
    profile["protocol"] = "vmess"
    profile["name"] = data.get("ps") or ""
    profile["server"] = data.get("add") or ""
    profile["port"] = _int(data.get("port"), 443)
    profile["uuid"] = data.get("id") or ""
    profile["network"] = data.get("net") or "tcp"
    if profile["network"] not in NETWORKS:
        profile["network"] = "tcp"
    profile["security"] = "tls" if str(data.get("tls") or "") == "tls" else "none"
    profile["server_name"] = data.get("sni") or ""
    profile["alpn"] = data.get("alpn") or ""
    profile["fingerprint"] = data.get("fp") or ""
    profile["path"] = data.get("path") or "/"
    profile["host"] = data.get("host") or ""
    if profile["network"] == "grpc":
        profile["service_name"] = data.get("path") or ""
    return normalize_profile(profile)


def _parse_trojan(text: str) -> dict:
    url = urlparse(text)
    query = parse_qs(url.query)
    profile = new_profile()
    profile["protocol"] = "trojan"
    profile["password"] = unquote(url.username or "")
    profile["server"] = url.hostname or ""
    profile["port"] = url.port or 443
    profile["name"] = unquote(url.fragment)
    profile["security"] = "tls"
    profile["server_name"] = _query_value(query, "sni", "peer")
    profile["fingerprint"] = _query_value(query, "fp")
    profile["alpn"] = _query_value(query, "alpn")
    profile["allow_insecure"] = _query_value(query, "allowInsecure") in ("1", "true")
    profile["network"] = _query_value(query, "type") or "tcp"
    if profile["network"] not in NETWORKS:
        profile["network"] = "tcp"
    profile["path"] = _query_value(query, "path") or "/"
    profile["host"] = _query_value(query, "host")
    profile["service_name"] = _query_value(query, "serviceName")
    return normalize_profile(profile)


def _parse_ss(text: str) -> dict:
    payload = text[5:]
    name = ""
    if "#" in payload:
        payload, name = payload.split("#", 1)
        name = unquote(name)
    query = {}
    if "?" in payload:
        payload, raw = payload.split("?", 1)
        query = {k: v[-1] for k, v in parse_qs(raw).items()}
    if "@" in payload:
        userinfo, hostpart = payload.rsplit("@", 1)
        userinfo = unquote(userinfo)
        if ":" not in userinfo:
            userinfo = base64.b64decode(userinfo + "=" * (-len(userinfo) % 4)).decode()
    else:
        decoded = base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)).decode()
        userinfo, hostpart = decoded.rsplit("@", 1)
    method, _, password = userinfo.partition(":")
    hostpart = hostpart.split("/")[0]
    host, _, port = hostpart.rpartition(":")
    profile = new_profile()
    profile["protocol"] = "shadowsocks"
    profile["name"] = name
    profile["server"] = host or hostpart
    profile["port"] = _int(port, 8388)
    profile["method"] = method
    profile["password"] = password
    profile["security"] = "none"
    profile["network"] = "tcp"
    return normalize_profile(profile)


def share_link(profile: dict) -> str:
    profile = normalize_profile(profile)
    if profile["protocol"] == "vless":
        query = [("encryption", "none"), ("security", profile["security"]),
                 ("type", profile["network"])]
        if profile["flow"]:
            query.append(("flow", profile["flow"]))
        for key, value in (("sni", profile["server_name"]), ("fp", profile["fingerprint"]),
                           ("pbk", profile["public_key"]), ("sid", profile["short_id"]),
                           ("spx", profile["spider_x"]), ("alpn", profile["alpn"])):
            if value:
                query.append((key, value))
        if profile["allow_insecure"]:
            query.append(("allowInsecure", "1"))
        if profile["network"] == "ws":
            query += [("path", profile["path"]), ("host", profile["host"])]
        if profile["network"] == "grpc":
            query.append(("serviceName", profile["service_name"]))
        params = "&".join(f"{k}={quote(str(v), safe='')}" for k, v in query if str(v))
        return (f"vless://{quote(profile['uuid'], safe='')}@{profile['server']}:"
                f"{profile['port']}?{params}#{quote(display_name(profile))}")

    if profile["protocol"] == "vmess":
        data = {
            "v": "2", "ps": display_name(profile), "add": profile["server"],
            "port": str(profile["port"]), "id": profile["uuid"], "aid": "0",
            "scy": "auto", "net": profile["network"], "type": "none",
            "host": profile["host"], "path": profile["path"],
            "tls": profile["security"] if profile["security"] == "tls" else "",
            "sni": profile["server_name"], "alpn": profile["alpn"],
            "fp": profile["fingerprint"],
        }
        if profile["network"] == "grpc":
            data["path"] = profile["service_name"]
        blob = base64.b64encode(json.dumps(data).encode()).decode()
        return "vmess://" + blob

    if profile["protocol"] == "trojan":
        query = [("security", "tls"), ("type", profile["network"])]
        for key, value in (("sni", profile["server_name"]), ("fp", profile["fingerprint"]),
                           ("alpn", profile["alpn"])):
            if value:
                query.append((key, value))
        if profile["allow_insecure"]:
            query.append(("allowInsecure", "1"))
        if profile["network"] == "ws":
            query += [("path", profile["path"]), ("host", profile["host"])]
        if profile["network"] == "grpc":
            query.append(("serviceName", profile["service_name"]))
        params = "&".join(f"{k}={quote(str(v), safe='')}" for k, v in query if str(v))
        return (f"trojan://{quote(profile['password'], safe='')}@{profile['server']}:"
                f"{profile['port']}?{params}#{quote(display_name(profile))}")

    userinfo = base64.urlsafe_b64encode(
        f"{profile['method']}:{profile['password']}".encode()).decode().rstrip("=")
    return (f"ss://{userinfo}@{profile['server']}:{profile['port']}"
            f"#{quote(display_name(profile))}")
