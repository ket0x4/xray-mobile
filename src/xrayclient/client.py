"""Thin D-Bus client for the Xray daemon."""

from __future__ import annotations

import json

import gi

gi.require_version("Gio", "2.0")
gi.require_version("GLib", "2.0")

from gi.repository import Gio, GLib, GObject

BUS_NAME = "io.github.xrayclient.Daemon"
OBJECT_PATH = "/io/github/xrayclient/Daemon"
IFACE = "io.github.xrayclient.Daemon"

PROPERTIES = {
    "State": "state", "ProfileName": "profile", "ProfileId": "profile_id",
    "Error": "error", "Since": "since", "Version": "version",
}


class DaemonClient(GObject.Object):
    __gsignals__ = {
        "ready": (GObject.SignalFlags.RUN_FIRST, None, ()),
        "changed": (GObject.SignalFlags.RUN_FIRST, None, ()),
        "unavailable": (GObject.SignalFlags.RUN_FIRST, None, (str,)),
    }

    def __init__(self) -> None:
        super().__init__()
        self.proxy: Gio.DBusProxy | None = None
        self.status = {
            "state": "off", "profile": "", "profile_id": "", "error": "",
            "since": 0, "version": "",
        }
        Gio.DBusProxy.new_for_bus(
            Gio.BusType.SYSTEM, Gio.DBusProxyFlags.NONE, None,
            BUS_NAME, OBJECT_PATH, IFACE, None, self._on_proxy,
        )

    def _on_proxy(self, source, result) -> None:
        try:
            self.proxy = Gio.DBusProxy.new_for_bus_finish(result)
        except Exception as exc:  # noqa: BLE001
            self.emit("unavailable", str(exc))
            return
        self.proxy.connect("g-properties-changed", self._on_properties)
        self.proxy.connect("notify::g-name-owner", self._on_name_owner)
        if self.proxy.get_name_owner() is None:
            self.status["state"] = "unavailable"
            self.emit("unavailable", "The Xray service is not running")
        self._read_properties()
        self.emit("ready")

    def _on_name_owner(self, proxy, _param) -> None:
        if proxy.get_name_owner() is None:
            self.status["state"] = "unavailable"
            self.emit("changed")
        else:
            self._read_properties()
            self.emit("ready")

    def _on_properties(self, proxy, changed, _invalidated) -> None:
        for name in changed.unpack().keys():
            self._read_property(name)
        self.emit("changed")

    def _read_property(self, name: str) -> None:
        if not self.proxy:
            return
        value = self.proxy.get_cached_property(name)
        if value is not None and name in PROPERTIES:
            self.status[PROPERTIES[name]] = value.unpack()

    def _read_properties(self) -> None:
        for name in PROPERTIES:
            self._read_property(name)
        self.emit("changed")

    def call(self, method: str, params=None, callback=None) -> None:
        if not self.proxy:
            if callback:
                callback(False, "The Xray service is not running")
            return
        self.proxy.call(
            method, params, Gio.DBusCallFlags.NONE, 120000, None,
            lambda proxy, result: self._finish(proxy, result, callback),
        )

    def _finish(self, proxy, result, callback) -> None:
        try:
            values = proxy.call_finish(result).unpack()
        except Exception as exc:  # noqa: BLE001
            if callback:
                callback(False, str(exc).splitlines()[-1])
            return
        if callback:
            if len(values) == 2:
                callback(bool(values[0]), str(values[1]))
            else:
                callback(True, values[0] if values else "")

    def _on_action(self, ok: bool, message: str) -> None:
        if not ok and message:
            self.emit("unavailable", message)

    def connect_proxy(self) -> None:
        self.call("Connect", None, self._on_action)

    def disconnect_proxy(self) -> None:
        self.call("Disconnect", None, self._on_action)

    def get_config(self, callback) -> None:
        self.call("GetConfig", None,
                  lambda ok, text: callback(json.loads(text)) if ok else None)

    def save_config(self, config: dict, callback=None) -> None:
        self.call("SaveConfig", GLib.Variant("(s)", (json.dumps(config),)),
                  lambda ok, message: callback(ok, message) if callback else None)
