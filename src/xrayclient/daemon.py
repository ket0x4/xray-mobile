"""Root daemon: owns the Xray process, the TUN interface and the system DNS."""

from __future__ import annotations

import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import time

import gi

gi.require_version("Gio", "2.0")
gi.require_version("GLib", "2.0")

from gi.repository import Gio, GLib

from . import model

BUS_NAME = "io.github.xrayclient.Daemon"
OBJECT_PATH = "/io/github/xrayclient/Daemon"
IFACE = "io.github.xrayclient.Daemon"

STATE_DIR = os.environ.get("XRAYCLIENT_STATE_DIR", "/etc/xray-client")
RUN_DIR = os.environ.get("XRAYCLIENT_RUN_DIR", "/run/xray-client")
CONFIG_PATH = os.path.join(STATE_DIR, "config.json")
RUNTIME_CONFIG = os.path.join(RUN_DIR, "xray.json")
LOG_PATH = os.path.join(RUN_DIR, "xray.log")
RESOLV_BACKUP = os.path.join(RUN_DIR, "resolv.conf.bak")
RESOLV_MARKER = os.path.join(RUN_DIR, "resolv.written")
RESOLV_PATH = "/etc/resolv.conf"
SYSCTL_NET = "/proc/sys/net"

RESOLVE_NAME = "org.freedesktop.resolve1"
RESOLVE_PATH = "/org/freedesktop/resolve1"
RESOLVE_IFACE = "org.freedesktop.resolve1.Manager"

AUTOCONNECT_INTERVAL = 10
AUTOCONNECT_ATTEMPTS = 30

XML = f"""
<node>
  <interface name="{IFACE}">
    <method name="Connect">
      <arg type="b" direction="out" name="ok"/>
      <arg type="s" direction="out" name="message"/>
    </method>
    <method name="Disconnect">
      <arg type="b" direction="out" name="ok"/>
      <arg type="s" direction="out" name="message"/>
    </method>
    <method name="SaveConfig">
      <arg type="s" direction="in" name="config"/>
      <arg type="b" direction="out" name="ok"/>
      <arg type="s" direction="out" name="message"/>
    </method>
    <method name="GetConfig">
      <arg type="s" direction="out" name="config"/>
    </method>
    <property name="State" type="s" access="read"/>
    <property name="ProfileName" type="s" access="read"/>
    <property name="ProfileId" type="s" access="read"/>
    <property name="Error" type="s" access="read"/>
    <property name="Since" type="t" access="read"/>
    <property name="Version" type="s" access="read"/>
  </interface>
</node>
"""


def find_xray() -> str:
    candidates = [
        os.environ.get("XRAY_BIN", ""),
        "/usr/local/lib/xray-client/xray",
        "/usr/local/bin/xray",
        "/usr/bin/xray",
        shutil.which("xray") or "",
    ]
    for path in candidates:
        if path and os.access(path, os.X_OK):
            return path
    return ""


def read_tail(path: str, limit: int = 4000) -> str:
    try:
        with open(path, "rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            handle.seek(max(0, size - limit))
            return handle.read().decode("utf-8", "replace").strip()
    except OSError:
        return ""


class Daemon:
    def __init__(self) -> None:
        self.conn: Gio.DBusConnection | None = None
        self.loop = GLib.MainLoop()
        self.xray = find_xray()
        self.version = self._xray_version() if self.xray else ""
        self.process: subprocess.Popen | None = None
        self.log_handle = None
        self.config = self._load_config()
        self.state = "off"
        self.profile_name = ""
        self.profile_id = ""
        self.error = ""
        self.since = 0
        self._dns_written = False
        self._poll_timer = 0

    # -- state ------------------------------------------------------------

    def _load_config(self) -> dict:
        os.makedirs(STATE_DIR, exist_ok=True)
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as handle:
                return model.normalize_config(json.load(handle))
        except (OSError, ValueError):
            return model.new_config()

    def _save_config(self, config: dict) -> None:
        os.makedirs(STATE_DIR, exist_ok=True)
        tmp = CONFIG_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(config, handle, indent=2)
            handle.write("\n")
        os.replace(tmp, CONFIG_PATH)
        os.chmod(CONFIG_PATH, 0o600)

    def _set_state(self, state: str, profile: dict | None = None, error: str = "") -> None:
        self.state = state
        self.error = error
        if profile is not None:
            self.profile_name = model.display_name(profile)
            self.profile_id = profile["id"]
        if state == "on":
            self.since = int(time.time())
        elif state == "off":
            self.since = 0
        self._emit_changed()

    def _emit_changed(self) -> None:
        if not self.conn:
            return
        props = {
            "State": GLib.Variant("s", self.state),
            "ProfileName": GLib.Variant("s", self.profile_name),
            "ProfileId": GLib.Variant("s", self.profile_id),
            "Error": GLib.Variant("s", self.error),
            "Since": GLib.Variant("t", self.since),
            "Version": GLib.Variant("s", self.version),
        }
        self.conn.emit_signal(
            None, OBJECT_PATH, "org.freedesktop.DBus.Properties", "PropertiesChanged",
            GLib.Variant("(sa{sv}as)", (IFACE, props, [])),
        )

    # -- system DNS -------------------------------------------------------

    def _apply_dns(self) -> None:
        try:
            if os.path.islink(RESOLV_PATH):
                backup = {"kind": "symlink", "target": os.readlink(RESOLV_PATH)}
            else:
                with open(RESOLV_PATH, "r", encoding="utf-8") as handle:
                    backup = {"kind": "file", "content": handle.read()}
        except OSError:
            backup = {"kind": "missing"}
        content = f"nameserver {model.TUN_DNS}\noptions edns0 trust-ad\n"
        try:
            with open(RESOLV_BACKUP, "w", encoding="utf-8") as handle:
                json.dump(backup, handle)
            with open(RESOLV_MARKER, "w", encoding="utf-8") as handle:
                handle.write(content)
            if os.path.islink(RESOLV_PATH):
                os.unlink(RESOLV_PATH)
            with open(RESOLV_PATH, "w", encoding="utf-8") as handle:
                handle.write(content)
            self._dns_written = True
        except OSError as exc:
            print(f"xray-client: cannot set DNS: {exc}", file=sys.stderr, flush=True)

    def _restore_dns(self) -> None:
        if not os.path.exists(RESOLV_MARKER) and not self._dns_written:
            return
        try:
            with open(RESOLV_MARKER, "r", encoding="utf-8") as handle:
                written = handle.read()
            with open(RESOLV_BACKUP, "r", encoding="utf-8") as handle:
                backup = json.load(handle)
        except (OSError, ValueError):
            written, backup = "", {}
        try:
            if os.path.islink(RESOLV_PATH):
                current = None
            else:
                with open(RESOLV_PATH, "r", encoding="utf-8") as handle:
                    current = handle.read()
        except OSError:
            current = None

        if written and current == written:
            try:
                os.unlink(RESOLV_PATH)
                if backup.get("kind") == "symlink":
                    os.symlink(backup["target"], RESOLV_PATH)
                elif backup.get("kind") == "file":
                    with open(RESOLV_PATH, "w", encoding="utf-8") as handle:
                        handle.write(backup.get("content", ""))
            except OSError as exc:
                print(f"xray-client: cannot restore DNS: {exc}", file=sys.stderr, flush=True)
        for path in (RESOLV_BACKUP, RESOLV_MARKER):
            try:
                os.unlink(path)
            except OSError:
                pass
        self._dns_written = False

    def _recover_dns(self) -> None:
        try:
            with open(RESOLV_MARKER, "r", encoding="utf-8") as handle:
                written = handle.read()
            with open(RESOLV_PATH, "r", encoding="utf-8") as handle:
                current = handle.read()
            if current != written:
                for path in (RESOLV_BACKUP, RESOLV_MARKER):
                    try:
                        os.unlink(path)
                    except OSError:
                        pass
                return
        except OSError:
            pass
        self._restore_dns()

    def _revert_tunnel_dns(self) -> None:
        """Revert the resolver settings Xray attached to the TUN link.

        Xray points resolvectl at the tunnel gateway while it runs. If it is
        killed before reverting (SIGKILL, crash, power loss) the resolver keeps
        that gateway in its global scope, so lookups stall against an address
        that no longer answers. The link has to be reverted while it still
        exists: once xray0 is gone there is no link left to revert.
        """
        try:
            ifindex = socket.if_nametoindex(model.TUN_NAME)
        except OSError:
            return
        if not ifindex:
            return
        try:
            bus = Gio.bus_get_sync(Gio.BusType.SYSTEM, None)
            bus.call_sync(
                RESOLVE_NAME, RESOLVE_PATH, RESOLVE_IFACE, "RevertLink",
                GLib.Variant("(i)", (ifindex,)),
                GLib.VariantType.new("()"), Gio.DBusCallFlags.NONE, 2000, None,
            )
        except GLib.Error as exc:
            print(f"xray-client: cannot revert tunnel DNS: {exc}", file=sys.stderr, flush=True)

    def _ensure_tunnel_forwarding(self) -> None:
        """Keep the forwarding gate open on the tunnel interface.

        The kernel only forwards a packet when the interface it arrived on has
        forwarding enabled. Replies for hotspot clients arrive on the Xray TUN,
        which nothing else knows about, so with that gate shut clients get an
        address and DNS but never reach the internet. NetworkManager opens the
        gate on the AP interface itself; opening the one on the tunnel is this
        daemon's job.
        """
        path = f"{SYSCTL_NET}/ipv4/conf/{model.TUN_NAME}/forwarding"
        try:
            with open(path, "r", encoding="utf-8") as handle:
                if handle.read().strip() == "1":
                    return
            with open(path, "w", encoding="utf-8") as handle:
                handle.write("1")
        except FileNotFoundError:
            return
        except OSError as exc:
            print(f"xray-client: cannot enable forwarding on {model.TUN_NAME}: {exc}",
                  file=sys.stderr, flush=True)
            return

    def _network_ready(self) -> bool:
        """True once any NetworkManager connection is fully activated.

        Deliberately not based on the Connectivity property: NetworkManager runs
        its own connectivity check over DNS and HTTP, so a resolver still pointing
        at the dead tunnel gateway would report "none" forever and autoconnect
        would never fire.
        """
        try:
            bus = Gio.bus_get_sync(Gio.BusType.SYSTEM, None)
            reply = bus.call_sync(
                "org.freedesktop.NetworkManager", "/org/freedesktop/NetworkManager",
                "org.freedesktop.DBus.Properties", "Get",
                GLib.Variant("(ss)", ("org.freedesktop.NetworkManager", "ActiveConnections")),
                GLib.VariantType.new("(v)"), Gio.DBusCallFlags.NONE, 2000, None,
            )
            for path in reply.unpack()[0]:
                if not path or path == "/":
                    continue
                state = bus.call_sync(
                    "org.freedesktop.NetworkManager", path,
                    "org.freedesktop.DBus.Properties", "Get",
                    GLib.Variant("(ss)", ("org.freedesktop.NetworkManager.Connection.Active",
                                          "State")),
                    GLib.VariantType.new("(v)"), Gio.DBusCallFlags.NONE, 2000, None,
                ).unpack()[0]
                if int(state) == 2:  # NM_ACTIVE_CONNECTION_STATE_ACTIVATED
                    return True
        except (GLib.Error, TypeError, ValueError):
            return False
        return False

    # -- xray -------------------------------------------------------------

    def _xray_version(self) -> str:
        try:
            output = subprocess.run(
                [self.xray, "version"], capture_output=True, text=True, timeout=10,
            ).stdout.strip()
            return output.splitlines()[0].strip() if output else ""
        except (OSError, subprocess.SubprocessError):
            return ""

    def _write_runtime_config(self, profile: dict) -> None:
        os.makedirs(RUN_DIR, exist_ok=True)
        config = model.build_xray_config(self.config, profile)
        tmp = RUNTIME_CONFIG + ".tmp"
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(config, handle, indent=2)
            handle.write("\n")
        os.replace(tmp, RUNTIME_CONFIG)

    def _kill_process(self) -> None:
        process = self.process
        self.process = None
        if process is None:
            return
        if process.poll() is None:
            self._revert_tunnel_dns()
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        if self.log_handle:
            try:
                self.log_handle.close()
            except OSError:
                pass
            self.log_handle = None

    def _spawn(self) -> None:
        os.makedirs(RUN_DIR, exist_ok=True)
        self.log_handle = open(LOG_PATH, "w", encoding="utf-8")
        self.process = subprocess.Popen(
            [self.xray, "run", "-c", RUNTIME_CONFIG],
            stdout=self.log_handle, stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL, start_new_session=True,
        )

    def connect(self) -> tuple[bool, str]:
        if self.state in ("on", "connecting"):
            return True, ""
        profile = model.find_profile(self.config)
        if profile is None:
            return False, "No server selected"
        if not self.xray:
            return False, "Xray core is not installed"
        try:
            self._write_runtime_config(profile)
        except (OSError, ValueError) as exc:
            self._set_state("error", profile, str(exc))
            return False, str(exc)

        self._kill_process()
        self._set_state("connecting", profile)
        try:
            self._spawn()
        except OSError as exc:
            self._set_state("error", profile, str(exc))
            return False, str(exc)

        GLib.timeout_add(1400, self._check_started)
        return True, ""

    def _check_started(self) -> bool:
        if self.process is None:
            return False
        code = self.process.poll()
        if code is None:
            self._ensure_tunnel_forwarding()
            self._apply_dns()
            self._set_state("on", model.find_profile(self.config))
            self._poll_timer = GLib.timeout_add_seconds(2, self._poll)
            return False
        self._fail_from_log(code)
        return False

    def _poll(self) -> bool:
        if self.process is None:
            self._poll_timer = 0
            return False
        code = self.process.poll()
        if code is None:
            self._ensure_tunnel_forwarding()
            return True
        self._poll_timer = 0
        self._fail_from_log(code)
        return False

    def _fail_from_log(self, code: int) -> None:
        tail = read_tail(LOG_PATH)
        lines = [line for line in tail.splitlines() if line.strip()]
        message = lines[-1] if lines else f"Xray exited with status {code}"
        self._kill_process()
        self._restore_dns()
        self._set_state("error", model.find_profile(self.config), message)

    def disconnect(self) -> tuple[bool, str]:
        if self._poll_timer:
            GLib.source_remove(self._poll_timer)
            self._poll_timer = 0
        self._kill_process()
        self._restore_dns()
        self._set_state("off", model.find_profile(self.config))
        return True, ""

    # -- D-Bus ------------------------------------------------------------

    def _on_method(self, conn, sender, path, iface, method, params, invocation) -> None:
        try:
            if method == "Connect":
                ok, message = self.connect()
                invocation.return_value(GLib.Variant("(bs)", (ok, message)))
            elif method == "Disconnect":
                ok, message = self.disconnect()
                invocation.return_value(GLib.Variant("(bs)", (ok, message)))
            elif method == "SaveConfig":
                ok, message = self.save_config(params.unpack()[0])
                invocation.return_value(GLib.Variant("(bs)", (ok, message)))
            elif method == "GetConfig":
                invocation.return_value(GLib.Variant("(s)", (json.dumps(self.config),)))
            else:
                invocation.return_dbus_error("org.freedesktop.DBus.Error.UnknownMethod", method)
        except Exception as exc:  # noqa: BLE001 - never kill the bus service
            invocation.return_dbus_error("io.github.xrayclient.Error", str(exc))

    def _on_get_property(self, conn, sender, path, iface, name):
        values = {
            "State": GLib.Variant("s", self.state),
            "ProfileName": GLib.Variant("s", self.profile_name),
            "ProfileId": GLib.Variant("s", self.profile_id),
            "Error": GLib.Variant("s", self.error),
            "Since": GLib.Variant("t", self.since),
            "Version": GLib.Variant("s", self.version),
        }
        return values.get(name)

    def save_config(self, text: str) -> tuple[bool, str]:
        try:
            incoming = model.normalize_config(json.loads(text))
        except (TypeError, ValueError) as exc:
            return False, f"Invalid configuration: {exc}"
        if incoming["active"]:
            profile = model.find_profile(incoming)
            try:
                model.build_xray_config(incoming, profile, resolve=False)
            except ValueError as exc:
                return False, str(exc)
            if self.xray:
                ok, message = self._test_config(incoming, profile)
                if not ok:
                    return False, message
        old_payload = self._payload(self.config)
        new_payload = self._payload(incoming)
        try:
            self._save_config(incoming)
        except OSError as exc:
            return False, f"Cannot save configuration: {exc}"
        was_connected = self.state in ("on", "connecting")
        self.config = incoming
        active = model.find_profile(incoming)
        if was_connected and active is not None:
            self.profile_name = model.display_name(active)
            self.profile_id = active["id"]
            self._emit_changed()
        elif self.state in ("off", "error"):
            self._set_state(self.state, active, self.error)
        if was_connected and old_payload != new_payload:
            self.disconnect()
            if incoming["active"]:
                GLib.timeout_add(300, self._reconnect)
        return True, ""

    def _payload(self, config: dict) -> str:
        profile = model.find_profile(config)
        if profile is None:
            return ""
        try:
            return json.dumps(model.build_xray_config(config, profile, resolve=False),
                              sort_keys=True)
        except ValueError:
            return ""

    def _reconnect(self) -> bool:
        if self.config["active"]:
            self.connect()
        return False

    def _test_config(self, config: dict, profile: dict) -> tuple[bool, str]:
        os.makedirs(RUN_DIR, exist_ok=True)
        test_path = os.path.join(RUN_DIR, "test.json")
        try:
            with open(test_path, "w", encoding="utf-8") as handle:
                json.dump(model.build_xray_config(config, profile, resolve=False), handle)
            result = subprocess.run(
                [self.xray, "run", "-test", "-c", test_path],
                capture_output=True, text=True, timeout=20,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            return False, str(exc)
        finally:
            try:
                os.unlink(test_path)
            except OSError:
                pass
        if result.returncode != 0:
            lines = (result.stderr or result.stdout or "").strip().splitlines()
            return False, lines[-1] if lines else "Xray rejected the configuration"
        return True, ""

    def _on_bus(self, conn, name) -> None:
        if not name:
            print("xray-client: cannot own the system bus name", file=sys.stderr, flush=True)
            self.loop.quit()
            return
        self.conn = conn
        info = Gio.DBusNodeInfo.new_for_xml(XML)
        conn.register_object(OBJECT_PATH, info.interfaces[0], self._on_method,
                             self._on_get_property, None)
        self._emit_changed()

    def run(self) -> int:
        os.makedirs(RUN_DIR, exist_ok=True)
        self._recover_dns()
        self.profile_name = model.display_name(model.find_profile(self.config) or {}) \
            if self.config.get("active") else ""
        GLib.unix_signal_add(GLib.PRIORITY_HIGH, signal.SIGTERM, self._on_quit_signal)
        GLib.unix_signal_add(GLib.PRIORITY_HIGH, signal.SIGINT, self._on_quit_signal)
        Gio.bus_own_name(Gio.BusType.SYSTEM, BUS_NAME, Gio.BusNameOwnerFlags.NONE,
                         self._on_bus, None, None)
        if self.config["settings"]["connect_on_boot"] and self.config["active"]:
            GLib.timeout_add_seconds(5, self._autoconnect, 0)
        self.loop.run()
        return 0

    def _on_quit_signal(self) -> bool:
        self.disconnect()
        self.loop.quit()
        return GLib.SOURCE_REMOVE

    def _autoconnect(self, attempt: int) -> bool:
        if self.state != "off" or not self.config["active"]:
            return GLib.SOURCE_REMOVE
        if self._network_ready():
            self.connect()
            return GLib.SOURCE_REMOVE
        if attempt >= AUTOCONNECT_ATTEMPTS:
            self._set_state(
                "error", model.find_profile(self.config),
                "Cannot resolve the server. Connect to a network first.")
            return GLib.SOURCE_REMOVE
        GLib.timeout_add_seconds(AUTOCONNECT_INTERVAL, self._autoconnect, attempt + 1)
        return GLib.SOURCE_REMOVE


def main() -> int:
    return Daemon().run()


if __name__ == "__main__":
    sys.exit(main())
