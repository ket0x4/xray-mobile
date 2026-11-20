#!/bin/sh
set -eu

PREFIX=${PREFIX:-/usr/local}
DESTDIR=${DESTDIR:-}
DATADIR=${DATADIR:-$PREFIX/share}
DBUSDIR=${DBUSDIR:-/usr/share/dbus-1}
UNITDIR=${UNITDIR:-$PREFIX/lib/systemd/system}
PLUGIN_ICONS=${PLUGIN_ICONS:-/usr/share/phosh/plugins/icons}
LIBDIR="$PREFIX/lib/xray-client"
BINDIR="$PREFIX/bin"
XRAY_VERSION=${XRAY_VERSION:-26.11.9}
NO_PLUGIN=${NO_PLUGIN:-}

ROOT=$DESTDIR

run() {
    if [ -n "$ROOT" ] || [ "$(id -u)" -eq 0 ]; then
        "$@"
    else
        sudo -- "$@"
    fi
}

info() {
    printf '%s\n' "$*"
}

BUILD_AS=
if [ -n "${BUILD_DIR:-}" ]; then
    mkdir -p "$BUILD_DIR"
elif [ "$(id -u)" -eq 0 ] && [ -n "${SUDO_USER:-}" ]; then
    BUILD_DIR=/var/tmp/xray-mobile-build
    install -d -o "$SUDO_USER" -g "$(id -gn "$SUDO_USER")" "$BUILD_DIR"
    BUILD_AS="runuser -u $SUDO_USER --"
else
    BUILD_DIR="$PWD/build"
    mkdir -p "$BUILD_DIR"
fi

info "Installing Xray Client into $ROOT$PREFIX"

run install -d "$ROOT$LIBDIR/xrayclient"
for file in src/xrayclient/*.py; do
    run install -m644 "$file" "$ROOT$LIBDIR/xrayclient/$(basename "$file")"
done
run install -m755 data/xray-clientd "$ROOT$LIBDIR/xray-clientd"

run install -d "$ROOT$BINDIR"
run install -m755 data/xray-client "$ROOT$BINDIR/xray-client"

if [ ! -x "$ROOT$LIBDIR/xray" ]; then
    case "$(uname -m)" in
        aarch64|arm64) XRAY_ASSET=arm64-v8a ;;
        x86_64|amd64) XRAY_ASSET=64 ;;
        armv7l|armv8l) XRAY_ASSET=arm32-v7a ;;
        *) info "Unsupported architecture: $(uname -m)"; exit 1 ;;
    esac
    XRAY_URL="https://github.com/XTLS/Xray-core/releases/download/v$XRAY_VERSION/Xray-linux-$XRAY_ASSET.zip"
    info "Downloading Xray core v$XRAY_VERSION"
    TMP=$(mktemp -d)
    trap 'rm -rf "$TMP"' EXIT
    curl -fsSL -o "$TMP/xray.zip" "$XRAY_URL"
    unzip -q -o "$TMP/xray.zip" xray -d "$TMP"
    run install -m755 "$TMP/xray" "$ROOT$LIBDIR/xray"
    rm -rf "$TMP"
    trap - EXIT
fi

run install -d "$ROOT$DATADIR/applications"
run install -m644 data/io.github.xrayclient.Xray.desktop \
    "$ROOT$DATADIR/applications/io.github.xrayclient.Xray.desktop"
run install -d "$ROOT$DATADIR/icons/hicolor/scalable/apps"
run install -m644 data/io.github.xrayclient.Xray.svg \
    "$ROOT$DATADIR/icons/hicolor/scalable/apps/io.github.xrayclient.Xray.svg"
run install -m644 phosh-plugin/xray-quick-setting-symbolic.svg \
    "$ROOT$DATADIR/icons/hicolor/scalable/apps/xray-quick-setting-symbolic.svg"

info "Installing the background service"

run install -d "$ROOT$DBUSDIR/system.d" "$ROOT$DBUSDIR/system-services"
run install -m644 data/io.github.xrayclient.Daemon.conf \
    "$ROOT$DBUSDIR/system.d/io.github.xrayclient.Daemon.conf"
sed "s|@LIBDIR@|$LIBDIR|g" data/io.github.xrayclient.Daemon.service.in \
    > "$BUILD_DIR/io.github.xrayclient.Daemon.service"
run install -m644 "$BUILD_DIR/io.github.xrayclient.Daemon.service" \
    "$ROOT$DBUSDIR/system-services/io.github.xrayclient.Daemon.service"

run install -d "$ROOT$UNITDIR"
sed "s|@LIBDIR@|$LIBDIR|g" data/xray-clientd.service.in > "$BUILD_DIR/xray-clientd.service"
run install -m644 "$BUILD_DIR/xray-clientd.service" "$ROOT$UNITDIR/xray-clientd.service"

if [ -z "$NO_PLUGIN" ] && pkg-config --exists phosh-plugins && command -v meson >/dev/null; then
    info "Building the Phosh quick setting"
    PLUGIN_DIR=$(pkg-config --variable=quick_setting_plugins_dir phosh-plugins)
    PHOSH_VERSION=$(pkg-config --modversion phosh-plugins)
    PHOSH_SRC=${PHOSH_SRC:-}

    if [ -z "$PHOSH_SRC" ]; then
        PHOSH_SRC="$BUILD_DIR/phosh-$PHOSH_VERSION"
        if [ ! -d "$PHOSH_SRC" ]; then
            info "Downloading phosh $PHOSH_VERSION sources"
            curl -fsSL -o "$BUILD_DIR/phosh-$PHOSH_VERSION.tar.xz" \
                "https://sources.phosh.mobi/releases/phosh/phosh-$PHOSH_VERSION.tar.xz"
            tar -xJf "$BUILD_DIR/phosh-$PHOSH_VERSION.tar.xz" -C "$BUILD_DIR"
        fi
    fi

    rm -rf "$BUILD_DIR/plugin"
    $BUILD_AS meson setup "$BUILD_DIR/plugin" phosh-plugin -Dphosh_src="$PHOSH_SRC" >/dev/null
    $BUILD_AS meson compile -C "$BUILD_DIR/plugin" >/dev/null

    PLUGIN_SO=$(find "$BUILD_DIR/plugin" -maxdepth 1 -name 'libphosh-plugin-xray-quick-setting.so' | head -n1)
    if [ -z "$PLUGIN_SO" ]; then
        info "Quick setting plugin build failed"
        exit 1
    fi
    sed "s|@plugins_dir@|$PLUGIN_DIR|g" phosh-plugin/xray-quick-setting.plugin.in \
        > "$BUILD_DIR/xray-quick-setting.plugin"

    run install -d "$ROOT$PLUGIN_DIR" "$ROOT$PLUGIN_ICONS"
    run install -m755 "$PLUGIN_SO" "$ROOT$PLUGIN_DIR/libphosh-plugin-xray-quick-setting.so"
    run install -m644 "$BUILD_DIR/xray-quick-setting.plugin" "$ROOT$PLUGIN_DIR/xray-quick-setting.plugin"
    run install -m644 phosh-plugin/xray-quick-setting-symbolic.svg \
        "$ROOT$PLUGIN_ICONS/xray-quick-setting-symbolic.svg"
else
    info "Skipping the Phosh quick setting plugin"
fi

if [ -z "$ROOT" ] || [ "$ROOT" = "/" ]; then
    for cache_tool in gtk-update-icon-cache gtk4-update-icon-cache; do
        if command -v "$cache_tool" >/dev/null 2>&1; then
            run "$cache_tool" -q -t -f "$DATADIR/icons/hicolor" || true
        fi
    done
    if command -v update-desktop-database >/dev/null 2>&1; then
        run update-desktop-database "$DATADIR/applications" || true
    fi
    if command -v systemctl >/dev/null 2>&1; then
        run systemctl daemon-reload || true
        run systemctl enable xray-clientd.service || true
        run systemctl restart xray-clientd.service || true
        systemctl reload dbus 2>/dev/null || true
    fi
fi

info ""
info "Done. Launch Xray from the app grid or run xray-client."
info "Reboot (or log out and back in) to add the quick setting tile."
