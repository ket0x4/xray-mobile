"""Application entry point."""

from __future__ import annotations

import sys

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gdk, Gio, Gtk

from .window import CSS, XrayWindow


class XrayApplication(Adw.Application):
    def __init__(self) -> None:
        super().__init__(
            application_id="io.github.xrayclient.Xray",
            flags=Gio.ApplicationFlags.DEFAULT_FLAGS,
        )

    def do_startup(self) -> None:
        Adw.Application.do_startup(self)
        provider = Gtk.CssProvider()
        provider.load_from_string(CSS)
        Gtk.StyleContext.add_provider_for_display(
            Gdk.Display.get_default(), provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION,
        )

    def do_activate(self) -> None:
        window = self.props.active_window
        if window is None:
            window = XrayWindow(self)
        window.present()


def main() -> int:
    return XrayApplication().run(sys.argv)


if __name__ == "__main__":
    sys.exit(main())
