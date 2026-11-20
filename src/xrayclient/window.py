"""Main window and pages."""

from __future__ import annotations

import copy
import time

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gdk, Gio, GLib, Gtk

from . import model
from .client import DaemonClient

CSS = """
.hero {
  padding: 26px 18px 22px 18px;
  border-radius: 18px;
}
.hero-icon-off {
  color: alpha(currentColor, 0.45);
}
.hero-icon-on {
  color: @accent_color;
}
.hero-title {
  font-size: 1.55rem;
  font-weight: 800;
}
.hero-subtitle {
  font-size: 0.95rem;
}
.hero-button {
  padding: 10px 34px;
  border-radius: 99px;
}
.status-error {
  color: @error_color;
}
.version-label {
  opacity: 0.65;
}
"""

PROTOCOL_LABELS = ["VLESS", "VMess", "Trojan", "Shadowsocks"]
NETWORK_LABELS = ["TCP", "WebSocket", "gRPC"]
SECURITY_LABELS = ["None", "TLS", "Reality"]
FLOW_LABELS = ["None", "xtls-rprx-vision"]


def _index(options: list, value, default: int = 0) -> int:
    try:
        return options.index(value)
    except ValueError:
        return default


def _human_duration(seconds: int) -> str:
    if seconds < 60:
        return "less than a minute"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes} min"
    hours, minutes = divmod(minutes, 60)
    if minutes:
        return f"{hours} h {minutes} min"
    return f"{hours} h"


class XrayWindow(Adw.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app, title="Xray")
        self.set_default_size(400, 820)

        self.client = DaemonClient()
        self.config = model.new_config()
        self._settings_page = None
        self._updating = False
        self._settings_source = 0
        self._server_rows = []

        self._build_ui()
        self.client.connect("ready", self._on_ready)
        self.client.connect("changed", self._on_status)
        self.client.connect("unavailable", self._on_unavailable)
        GLib.timeout_add_seconds(30, self._tick)

    def _build_ui(self) -> None:
        self.toasts = Adw.ToastOverlay()

        self.nav = Adw.NavigationView()
        self.nav.connect("popped", self._on_popped)
        self.toasts.set_child(self.nav)
        self.set_content(self.toasts)

        for name, callback in (("settings", self._show_settings),
                               ("about", self._show_about)):
            action = Gio.SimpleAction.new(name, None)
            action.connect("activate", lambda *_, cb=callback: cb())
            self.add_action(action)

        self.nav.push(self._build_main_page())

    # -- main page -------------------------------------------------------

    def _build_main_page(self) -> Adw.NavigationPage:
        page = Adw.NavigationPage(title="Xray", tag="main")

        self.header_title = Adw.WindowTitle(title="Xray", subtitle="")
        header = Adw.HeaderBar()
        header.set_title_widget(self.header_title)

        menu = Gio.Menu()
        menu.append("Preferences", "win.settings")
        menu.append("About Xray", "win.about")
        menu_button = Gtk.MenuButton(icon_name="open-menu-symbolic", menu_model=menu)
        header.pack_end(menu_button)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)
        box.set_margin_top(12)
        box.set_margin_bottom(24)
        box.set_margin_start(12)
        box.set_margin_end(12)

        self.hero = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self.hero.add_css_class("card")
        self.hero.add_css_class("hero")
        self.hero.set_valign(Gtk.Align.START)

        self.hero_icon = Gtk.Image(pixel_size=56)
        self.hero_icon.set_margin_bottom(4)
        self.hero.append(self.hero_icon)

        self.hero_title = Gtk.Label()
        self.hero_title.add_css_class("hero-title")
        self.hero.append(self.hero_title)

        self.hero_subtitle = Gtk.Label()
        self.hero_subtitle.add_css_class("hero-subtitle")
        self.hero_subtitle.add_css_class("dim-label")
        self.hero_subtitle.set_wrap(True)
        self.hero_subtitle.set_justify(Gtk.Justification.CENTER)
        self.hero.append(self.hero_subtitle)

        self.hero_error = Gtk.Label()
        self.hero_error.add_css_class("status-error")
        self.hero_error.set_wrap(True)
        self.hero_error.set_justify(Gtk.Justification.CENTER)
        self.hero_error.set_visible(False)
        self.hero.append(self.hero_error)

        self.hero_button = Gtk.Button()
        self.hero_button.add_css_class("hero-button")
        self.hero_button.set_halign(Gtk.Align.CENTER)
        self.hero_button.set_margin_top(10)
        self.hero_button.connect("clicked", self._on_hero_clicked)
        self.hero.append(self.hero_button)

        box.append(self.hero)

        self.servers_group = Adw.PreferencesGroup(title="Servers")
        box.append(self.servers_group)

        prefs_group = Adw.PreferencesGroup()
        prefs_row = Adw.ActionRow(title="TUN and routing",
                                  subtitle="MTU, DNS, IPv6 and startup")
        prefs_row.add_prefix(Gtk.Image(icon_name="emblem-system-symbolic"))
        prefs_row.add_suffix(Gtk.Image(icon_name="go-next-symbolic"))
        prefs_row.set_activatable(True)
        prefs_row.connect("activated", lambda *_: self._show_settings())
        prefs_group.add(prefs_row)
        box.append(prefs_group)

        clamp = Adw.Clamp(maximum_size=560)
        clamp.set_child(box)
        scrolled = Gtk.ScrolledWindow()
        scrolled.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scrolled.set_child(clamp)

        toolbar = Adw.ToolbarView()
        toolbar.add_top_bar(header)
        toolbar.set_content(scrolled)
        page.set_child(toolbar)

        self._rebuild_servers()
        self._update_hero()
        return page

    def _rebuild_servers(self) -> None:
        for row in self._server_rows:
            self.servers_group.remove(row)
        self._server_rows = []

        self._updating = True
        radio = None
        for profile in self.config["profiles"]:
            row = Adw.ActionRow(title=model.display_name(profile),
                                subtitle=model.subtitle(profile))
            row.set_activatable(True)
            check = Gtk.CheckButton(valign=Gtk.Align.CENTER)
            if radio is None:
                radio = check
            else:
                check.set_group(radio)
            check.set_active(profile["id"] == self.config["active"])
            check.connect("toggled", self._on_profile_selected, profile["id"])
            row.add_prefix(check)
            row.add_suffix(Gtk.Image(icon_name="go-next-symbolic"))
            row.connect("activated", lambda *_, pid=profile["id"]: self._edit_profile(pid))
            self.servers_group.add(row)
            self._server_rows.append(row)
        self._updating = False

        add = Adw.ActionRow(title="Add server")
        add.add_prefix(Gtk.Image(icon_name="list-add-symbolic"))
        add.set_activatable(True)
        add.connect("activated", lambda *_: self._add_server())
        self.servers_group.add(add)
        self._server_rows.append(add)

    def _on_profile_selected(self, check, profile_id: str) -> None:
        if self._updating:
            return
        if check.get_active():
            self.config["active"] = profile_id
            self._save_config(self.config)

    def _edit_profile(self, profile_id: str) -> None:
        profile = model.find_profile(self.config, profile_id)
        if profile is None:
            return
        self.nav.push(ProfileEditorPage(self, profile))

    def _add_server(self) -> None:
        dialog = AddServerDialog(self)
        dialog.present(self)

    # -- status ----------------------------------------------------------

    def _on_ready(self, _client) -> None:
        self.client.get_config(self._on_config_loaded)

    def _on_config_loaded(self, config) -> None:
        self.config = model.normalize_config(config)
        self._rebuild_servers()
        self._update_hero()
        if self._settings_page is not None:
            self._settings_page.load(self.config)

    def _on_status(self, _client) -> None:
        self._update_hero()
        if self._settings_page is not None:
            self._settings_page.set_version(self.client.status.get("version", ""))

    def _on_unavailable(self, _client, message: str) -> None:
        if message:
            self.toast(message)

    def _update_hero(self) -> None:
        status = self.client.status
        state = status.get("state", "off")
        profile = model.find_profile(self.config, status.get("profile_id"))
        name = status.get("profile") or (model.display_name(profile) if profile else "")

        for css in ("hero-icon-on", "hero-icon-off"):
            self.hero_icon.remove_css_class(css)

        if state == "on":
            self.hero_icon.set_from_icon_name("xray-quick-setting-symbolic")
            self.hero_icon.add_css_class("hero-icon-on")
            self.hero_title.set_text("Connected")
            since = status.get("since") or 0
            extra = ""
            if since:
                extra = " · connected for " + _human_duration(max(0, int(time.time()) - since))
            self.hero_subtitle.set_text((name or "Xray") + extra)
            self.hero_button.set_label("Disconnect")
            self.hero_button.remove_css_class("suggested-action")
            self.hero_button.set_sensitive(True)
            self.hero_error.set_visible(False)
            self.header_title.set_subtitle(name)
        elif state == "connecting":
            self.hero_icon.set_from_icon_name("network-vpn-acquiring-symbolic")
            self.hero_icon.add_css_class("hero-icon-on")
            self.hero_title.set_text("Connecting…")
            self.hero_subtitle.set_text(name or "Starting Xray")
            self.hero_button.set_label("Disconnect")
            self.hero_button.remove_css_class("suggested-action")
            self.hero_button.set_sensitive(False)
            self.hero_error.set_visible(False)
            self.header_title.set_subtitle("Connecting…")
        elif state == "unavailable":
            self.hero_icon.set_from_icon_name("xray-quick-setting-symbolic")
            self.hero_icon.add_css_class("hero-icon-off")
            self.hero_title.set_text("Service unavailable")
            self.hero_subtitle.set_text("The Xray background service is not running")
            self.hero_button.set_label("Connect")
            self.hero_button.add_css_class("suggested-action")
            self.hero_button.set_sensitive(False)
            self.hero_error.set_visible(False)
            self.header_title.set_subtitle("")
        else:
            self.hero_icon.set_from_icon_name("xray-quick-setting-symbolic")
            self.hero_icon.add_css_class("hero-icon-off")
            self.hero_title.set_text("Disconnected" if state != "error" else "Connection failed")
            if profile is not None:
                self.hero_subtitle.set_text("Tap connect to use " + model.display_name(profile))
            else:
                self.hero_subtitle.set_text("Add a server to get started")
            self.hero_button.set_label("Connect")
            self.hero_button.add_css_class("suggested-action")
            self.hero_button.set_sensitive(profile is not None)
            error = status.get("error", "")
            self.hero_error.set_text(error)
            self.hero_error.set_visible(bool(error))
            self.header_title.set_subtitle("")

    def _on_hero_clicked(self, _button) -> None:
        if self.client.status.get("state") in ("on", "connecting"):
            self.client.disconnect_proxy()
        else:
            if not self.config["active"]:
                self.toast("Select a server first")
                return
            self.client.connect_proxy()

    def _tick(self) -> bool:
        if self.client.status.get("state") == "on":
            self._update_hero()
        return True

    # -- pages -----------------------------------------------------------

    def _show_settings(self) -> None:
        if self._settings_page is None:
            self._settings_page = SettingsPage(self)
        self.client.get_config(self._on_config_loaded)
        self._settings_page.load(self.config)
        self.nav.push(self._settings_page)

    def _show_about(self) -> None:
        dialog = Adw.AboutDialog(
            application_name="Xray",
            application_icon="io.github.xrayclient.Xray",
            developer_name="xray-mobile",
            version="1.0",
            comments="A small Xray client for GNOME Mobile and Phosh. "
                     "It routes all traffic through a TUN interface.",
        )
        dialog.set_developers(["xray-mobile"])
        dialog.add_credit_section(
            "Xray core",
            ["Project X (XTLS) — https://github.com/XTLS/Xray-core"])
        dialog.add_credit_section(
            "Phosh",
            ["Phosh contributors — https://gitlab.gnome.org/World/Phosh/phosh"])
        dialog.add_credit_section(
            "GTK and libadwaita",
            ["GNOME contributors — https://www.gnome.org"])
        dialog.present(self)

    def _save_config(self, config: dict, callback=None) -> None:
        def done(ok: bool, message: str) -> None:
            if not ok and message:
                self.toast(message)
            elif callback:
                callback()
        self.client.save_config(model.normalize_config(config), done)

    def _on_popped(self, _nav, _page) -> None:
        self.client.get_config(self._on_config_loaded)

    def toast(self, message: str) -> None:
        self.toasts.add_toast(Adw.Toast.new(message))


class AddServerDialog(Adw.Dialog):
    def __init__(self, window: XrayWindow):
        super().__init__()
        self.window = window
        self.set_title("Add server")
        self.set_content_width(360)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
        box.set_margin_top(20)
        box.set_margin_bottom(18)
        box.set_margin_start(16)
        box.set_margin_end(16)

        title = Gtk.Label(label="Add server", xalign=0)
        title.add_css_class("title-2")
        box.append(title)

        subtitle = Gtk.Label(
            label="Paste a VLESS, VMess, Trojan or Shadowsocks share link, "
                  "or continue with an empty link to enter the details yourself.",
            xalign=0, wrap=True)
        subtitle.add_css_class("dim-label")
        box.append(subtitle)

        group = Adw.PreferencesGroup()
        self.link_row = Adw.EntryRow(title="Share link")
        group.add(self.link_row)
        box.append(group)

        self.error = Gtk.Label(xalign=0, wrap=True)
        self.error.add_css_class("status-error")
        self.error.set_visible(False)
        box.append(self.error)

        buttons = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        buttons.set_halign(Gtk.Align.END)
        cancel = Gtk.Button(label="Cancel")
        cancel.connect("clicked", lambda *_: self.close())
        continue_button = Gtk.Button(label="Continue")
        continue_button.add_css_class("suggested-action")
        continue_button.connect("clicked", lambda *_: self._continue())
        buttons.append(cancel)
        buttons.append(continue_button)
        box.append(buttons)

        self.set_child(box)
        self.link_row.connect("activate", lambda *_: self._continue())

    def _continue(self) -> None:
        text = self.link_row.get_text().strip()
        if not text:
            profile = model.new_profile()
        else:
            try:
                profile = model.parse_share_link(text)
            except (ValueError, KeyError, TypeError) as exc:
                self.error.set_text(f"Could not read that link: {exc}")
                self.error.set_visible(True)
                return
            if not profile["name"]:
                profile["name"] = profile["server"]
        self.close()
        self.window.nav.push(ProfileEditorPage(self.window, profile, is_new=True))


class ProfileEditorPage(Adw.NavigationPage):
    def __init__(self, window: XrayWindow, profile: dict, is_new: bool = False):
        super().__init__()
        self.window = window
        self.profile = model.normalize_profile(profile)
        self.set_title("New server" if is_new else "Server")
        self.set_tag("editor")

        self.header = Adw.HeaderBar()
        save = Gtk.Button(label="Save")
        save.add_css_class("suggested-action")
        save.connect("clicked", self._save)
        self.header.pack_end(save)

        page = Adw.PreferencesPage()

        general = Adw.PreferencesGroup(title="General")
        self.name_row = Adw.EntryRow(title="Name")
        self.name_row.set_text(self.profile["name"])
        self.name_row.set_show_apply_button(False)
        general.add(self.name_row)

        self.protocol_row = Adw.ComboRow(
            title="Protocol", model=Gtk.StringList.new(PROTOCOL_LABELS))
        self.protocol_row.set_selected(_index(model.PROTOCOLS, self.profile["protocol"]))
        self.protocol_row.connect("notify::selected", lambda *_: self._sync_rows())
        general.add(self.protocol_row)

        self.server_row = Adw.EntryRow(title="Address")
        self.server_row.set_text(self.profile["server"])
        general.add(self.server_row)

        self.port_row = Adw.SpinRow.new(
            Gtk.Adjustment(value=self.profile["port"], lower=1, upper=65535, step_increment=1),
            1, 0)
        self.port_row.set_title("Port")
        general.add(self.port_row)
        page.add(general)

        self.auth_group = Adw.PreferencesGroup(title="Authentication")
        self.uuid_row = Adw.EntryRow(title="UUID")
        self.uuid_row.set_text(self.profile["uuid"])
        self.auth_group.add(self.uuid_row)

        self.flow_row = Adw.ComboRow(
            title="Flow", model=Gtk.StringList.new(FLOW_LABELS))
        self.flow_row.set_selected(1 if self.profile["flow"] else 0)
        self.auth_group.add(self.flow_row)

        self.password_row = Adw.PasswordEntryRow(title="Password")
        self.password_row.set_text(self.profile["password"])
        self.auth_group.add(self.password_row)

        self.method_row = Adw.ComboRow(
            title="Method", model=Gtk.StringList.new(model.SS_METHODS))
        self.method_row.set_selected(_index(model.SS_METHODS, self.profile["method"]))
        self.auth_group.add(self.method_row)
        page.add(self.auth_group)

        self.transport_group = Adw.PreferencesGroup(title="Transport")
        self.network_row = Adw.ComboRow(
            title="Network", model=Gtk.StringList.new(NETWORK_LABELS))
        self.network_row.set_selected(_index(model.NETWORKS, self.profile["network"]))
        self.network_row.connect("notify::selected", lambda *_: self._sync_rows())
        self.transport_group.add(self.network_row)

        self.path_row = Adw.EntryRow(title="Path")
        self.path_row.set_text(self.profile["path"])
        self.transport_group.add(self.path_row)

        self.host_row = Adw.EntryRow(title="Host header")
        self.host_row.set_text(self.profile["host"])
        self.transport_group.add(self.host_row)

        self.service_row = Adw.EntryRow(title="Service name")
        self.service_row.set_text(self.profile["service_name"])
        self.transport_group.add(self.service_row)
        page.add(self.transport_group)

        self.security_group = Adw.PreferencesGroup(title="Security")
        self.security_row = Adw.ComboRow(
            title="Encryption", model=Gtk.StringList.new(SECURITY_LABELS))
        self.security_row.set_selected(_index(model.SECURITIES, self.profile["security"]))
        self.security_row.connect("notify::selected", lambda *_: self._sync_rows())
        self.security_group.add(self.security_row)

        self.sni_row = Adw.EntryRow(title="Server name (SNI)")
        self.sni_row.set_text(self.profile["server_name"])
        self.security_group.add(self.sni_row)

        self.alpn_row = Adw.EntryRow(title="ALPN")
        self.alpn_row.set_text(self.profile["alpn"])
        self.security_group.add(self.alpn_row)

        self.insecure_row = Adw.SwitchRow(title="Allow insecure certificates")
        self.insecure_row.set_active(self.profile["allow_insecure"])
        self.security_group.add(self.insecure_row)

        self.fingerprint_row = Adw.EntryRow(title="Fingerprint")
        self.fingerprint_row.set_text(self.profile["fingerprint"])
        self.security_group.add(self.fingerprint_row)

        self.public_key_row = Adw.EntryRow(title="Reality public key")
        self.public_key_row.set_text(self.profile["public_key"])
        self.security_group.add(self.public_key_row)

        self.short_id_row = Adw.EntryRow(title="Reality short ID")
        self.short_id_row.set_text(self.profile["short_id"])
        self.security_group.add(self.short_id_row)
        page.add(self.security_group)

        actions = Adw.PreferencesGroup()
        link_row = Adw.ActionRow(title="Copy share link")
        link_row.add_prefix(Gtk.Image(icon_name="edit-copy-symbolic"))
        link_row.set_activatable(True)
        link_row.connect("activated", lambda *_: self._copy_link())
        actions.add(link_row)

        if not is_new:
            delete_row = Adw.ActionRow(title="Delete server")
            delete_row.add_css_class("error")
            delete_row.add_prefix(Gtk.Image(icon_name="user-trash-symbolic"))
            delete_row.set_activatable(True)
            delete_row.connect("activated", lambda *_: self._delete())
            actions.add(delete_row)
        page.add(actions)

        toolbar = Adw.ToolbarView()
        toolbar.add_top_bar(self.header)
        toolbar.set_content(page)
        self.set_child(toolbar)

        self._sync_rows()

    def _sync_rows(self) -> None:
        protocol = model.PROTOCOLS[self.protocol_row.get_selected()]
        network = model.NETWORKS[self.network_row.get_selected()]
        security = model.SECURITIES[self.security_row.get_selected()]
        is_ss = protocol == "shadowsocks"
        is_vless = protocol == "vless"
        is_vmess = protocol == "vmess"
        uses_uuid = is_vless or is_vmess

        self.uuid_row.set_visible(uses_uuid)
        self.flow_row.set_visible(is_vless)
        self.password_row.set_visible(not uses_uuid)
        self.method_row.set_visible(is_ss)
        self.auth_group.set_visible(uses_uuid or is_ss or protocol == "trojan")

        self.transport_group.set_visible(True)
        self.path_row.set_visible(network == "ws")
        self.host_row.set_visible(network == "ws")
        self.service_row.set_visible(network == "grpc")

        if is_ss:
            self.security_row.set_selected(0)
        elif protocol == "trojan":
            self.security_row.set_selected(1)
        security = model.SECURITIES[self.security_row.get_selected()]
        self.security_row.set_visible(not is_ss)
        self.security_group.set_visible(not is_ss)
        self.sni_row.set_visible(security in ("tls", "reality"))
        self.alpn_row.set_visible(security == "tls")
        self.insecure_row.set_visible(security == "tls")
        self.fingerprint_row.set_visible(security in ("tls", "reality"))
        self.public_key_row.set_visible(security == "reality")
        self.short_id_row.set_visible(security == "reality")

    def _collect(self) -> dict:
        profile = dict(self.profile)
        profile["name"] = self.name_row.get_text().strip()
        profile["protocol"] = model.PROTOCOLS[self.protocol_row.get_selected()]
        profile["server"] = self.server_row.get_text().strip()
        profile["port"] = int(self.port_row.get_value())
        profile["uuid"] = self.uuid_row.get_text().strip()
        profile["flow"] = FLOW_LABELS[self.flow_row.get_selected()] \
            if self.flow_row.get_selected() == 1 else ""
        profile["password"] = self.password_row.get_text()
        profile["method"] = model.SS_METHODS[self.method_row.get_selected()]
        profile["network"] = model.NETWORKS[self.network_row.get_selected()]
        profile["security"] = model.SECURITIES[self.security_row.get_selected()]
        profile["server_name"] = self.sni_row.get_text().strip()
        profile["alpn"] = self.alpn_row.get_text().strip()
        profile["allow_insecure"] = self.insecure_row.get_active()
        profile["fingerprint"] = self.fingerprint_row.get_text().strip()
        profile["public_key"] = self.public_key_row.get_text().strip()
        profile["short_id"] = self.short_id_row.get_text().strip()
        profile["path"] = self.path_row.get_text().strip() or "/"
        profile["host"] = self.host_row.get_text().strip()
        profile["service_name"] = self.service_row.get_text().strip()
        if not profile["name"]:
            profile["name"] = profile["server"]
        return model.normalize_profile(profile)

    def _save(self, _button=None) -> None:
        profile = self._collect()
        if not profile["server"]:
            self.window.toast("Server address is required")
            return
        if profile["protocol"] in ("vless", "vmess") and not profile["uuid"]:
            self.window.toast("UUID is required")
            return
        if profile["protocol"] in ("trojan", "shadowsocks") and not profile["password"]:
            self.window.toast("Password is required")
            return

        config = copy.deepcopy(self.window.config)
        found = False
        for index, existing in enumerate(config["profiles"]):
            if existing["id"] == profile["id"]:
                config["profiles"][index] = profile
                found = True
                break
        if not found:
            config["profiles"].append(profile)
        if not config["active"]:
            config["active"] = profile["id"]

        def done() -> None:
            self.window.config = model.normalize_config(config)
            self.window._rebuild_servers()
            self.window.toast("Server saved")
            self.window.nav.pop()

        self.window._save_config(config, done)

    def _delete(self) -> None:
        dialog = Adw.AlertDialog(
            heading="Delete server?",
            body=f"{model.display_name(self.profile)} will be removed.",
        )
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("delete", "Delete")
        dialog.set_response_appearance("delete", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_default_response("cancel")
        dialog.connect("response", self._on_delete_response)
        dialog.present(self.window)

    def _on_delete_response(self, _dialog, response) -> None:
        if response != "delete":
            return
        config = copy.deepcopy(self.window.config)
        config["profiles"] = [p for p in config["profiles"] if p["id"] != self.profile["id"]]
        if config["active"] == self.profile["id"]:
            config["active"] = ""
        self.window.config = model.normalize_config(config)
        self.window._save_config(config)
        self.window._rebuild_servers()
        self.window.nav.pop()
        self.window.toast("Server deleted")

    def _copy_link(self) -> None:
        display = Gdk.Display.get_default()
        if display is None:
            return
        display.get_clipboard().set(model.share_link(self._collect()))
        self.window.toast("Share link copied")


class SettingsPage(Adw.NavigationPage):
    def __init__(self, window: XrayWindow):
        super().__init__()
        self.window = window
        self.set_title("Preferences")
        self.set_tag("settings")

        page = Adw.PreferencesPage()

        connection = Adw.PreferencesGroup(title="Connection")
        self.boot_row = Adw.SwitchRow(
            title="Connect on boot", subtitle="Start Xray when the phone boots")
        self.boot_row.connect("notify::active", lambda *_: self._schedule_save())
        connection.add(self.boot_row)

        self.ipv6_row = Adw.SwitchRow(
            title="Route IPv6", subtitle="Send IPv6 traffic through the proxy")
        self.ipv6_row.connect("notify::active", lambda *_: self._schedule_save())
        connection.add(self.ipv6_row)
        page.add(connection)

        tun = Adw.PreferencesGroup(
            title="TUN interface",
            description="The interface every app is routed through while connected.")
        self.mtu_row = Adw.SpinRow.new(
            Gtk.Adjustment(value=1500, lower=1280, upper=9000, step_increment=10), 10, 0)
        self.mtu_row.set_title("MTU")
        self.mtu_row.connect("notify::value", lambda *_: self._schedule_save())
        tun.add(self.mtu_row)

        self.dns_row = Adw.EntryRow(title="DNS servers (comma separated)")
        self.dns_row.set_text(model.DEFAULT_SETTINGS["dns"])
        self.dns_row.connect("changed", lambda *_: self._schedule_save())
        tun.add(self.dns_row)

        self.log_row = Adw.ComboRow(
            title="Log level",
            model=Gtk.StringList.new(["debug", "info", "warning", "error"]))
        self.log_row.connect("notify::selected", lambda *_: self._schedule_save())
        tun.add(self.log_row)
        page.add(tun)

        about = Adw.PreferencesGroup(title="About")
        self.core_row = Adw.ActionRow(title="Xray core", subtitle="Not installed")
        about.add(self.core_row)
        version_row = Adw.ActionRow(title="Xray Client", subtitle="1.0")
        about.add(version_row)
        page.add(about)

        header = Adw.HeaderBar()
        header.set_title_widget(Adw.WindowTitle(title="Preferences"))
        toolbar = Adw.ToolbarView()
        toolbar.add_top_bar(header)
        toolbar.set_content(page)
        self.set_child(toolbar)
        self._loading = False

    def load(self, config: dict) -> None:
        self._loading = True
        settings = config["settings"]
        self.boot_row.set_active(settings["connect_on_boot"])
        self.ipv6_row.set_active(settings["route_ipv6"])
        self.mtu_row.set_value(settings["mtu"])
        self.dns_row.set_text(settings["dns"])
        self.log_row.set_selected(_index(["debug", "info", "warning", "error"],
                                         settings["loglevel"], 2))
        self.set_version(self.window.client.status.get("version", ""))
        self._loading = False

    def set_version(self, version: str) -> None:
        self.core_row.set_subtitle(version or "Not installed")

    def _schedule_save(self) -> None:
        if self._loading:
            return
        if self.window._settings_source:
            GLib.source_remove(self.window._settings_source)
        self.window._settings_source = GLib.timeout_add(400, self._save)

    def _save(self) -> bool:
        self.window._settings_source = 0
        config = copy.deepcopy(self.window.config)
        settings = config["settings"]
        settings["connect_on_boot"] = self.boot_row.get_active()
        settings["route_ipv6"] = self.ipv6_row.get_active()
        settings["mtu"] = int(self.mtu_row.get_value())
        settings["dns"] = self.dns_row.get_text().strip() or model.DEFAULT_SETTINGS["dns"]
        settings["loglevel"] = ["debug", "info", "warning", "error"][
            self.log_row.get_selected()]
        self.window.config = model.normalize_config(config)
        self.window._save_config(config)
        return False
