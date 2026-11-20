/*
 * Xray quick setting for Phosh.
 *
 * SPDX-License-Identifier: GPL-3.0-or-later
 */

#include "quick-setting.h"
#include "phosh-plugin.h"

#include <gio/gdesktopappinfo.h>
#include <gio/gio.h>

#define BUS_NAME     "io.github.xrayclient.Daemon"
#define OBJECT_PATH  "/io/github/xrayclient/Daemon"
#define IFACE        "io.github.xrayclient.Daemon"
#define DESKTOP_ID   "io.github.xrayclient.Xray.desktop"

typedef struct {
  PhoshQuickSetting parent;
  PhoshStatusIcon  *info;
  GDBusConnection  *bus;
  guint             subscription;
  guint             watcher;
  gboolean          pending;
  char             *state;
  char             *profile;
} PhoshXrayQuickSetting;

typedef struct {
  PhoshQuickSettingClass parent_class;
} PhoshXrayQuickSettingClass;

G_DEFINE_TYPE (PhoshXrayQuickSetting, phosh_xray_quick_setting, PHOSH_TYPE_QUICK_SETTING)


static void
update_ui (PhoshXrayQuickSetting *self)
{
  gboolean on = g_strcmp0 (self->state, "on") == 0;
  const char *info;

  phosh_quick_setting_set_active (PHOSH_QUICK_SETTING (self), on);

  if (self->pending) {
    info = on ? "Disconnecting…" : "Connecting…";
  } else if (on) {
    info = self->profile && *self->profile ? self->profile : "Xray connected";
  } else if (g_strcmp0 (self->state, "connecting") == 0) {
    info = "Connecting…";
  } else if (g_strcmp0 (self->state, "error") == 0) {
    info = "Xray: connection failed";
  } else if (!self->bus) {
    info = "Xray service unavailable";
  } else {
    info = "Xray off";
  }
  phosh_status_icon_set_info (self->info, info);
  gtk_widget_set_sensitive (GTK_WIDGET (self), self->bus != NULL);
}


static void
set_state (PhoshXrayQuickSetting *self, const char *state, const char *profile)
{
  g_free (self->state);
  self->state = g_strdup (state ? state : "off");
  if (profile) {
    g_free (self->profile);
    self->profile = g_strdup (profile);
  }
  update_ui (self);
}


static void
call_finished (GObject *source, GAsyncResult *result, gpointer data)
{
  PhoshXrayQuickSetting *self = data;
  g_autoptr (GVariant) reply = NULL;
  g_autoptr (GError) error = NULL;

  self->pending = FALSE;
  reply = g_dbus_connection_call_finish (G_DBUS_CONNECTION (source), result, &error);
  if (!reply) {
    g_warning ("Xray call failed: %s", error->message);
    update_ui (self);
    return;
  }

  gboolean ok;
  const char *message;
  g_variant_get (reply, "(b&s)", &ok, &message);
  if (!ok && *message)
    g_warning ("Xray: %s", message);
  update_ui (self);
}


static void
toggle (PhoshXrayQuickSetting *self)
{
  const char *method;
  gboolean on;

  if (!self->bus || self->pending)
    return;

  on = g_strcmp0 (self->state, "on") == 0 ||
       g_strcmp0 (self->state, "connecting") == 0;
  method = on ? "Disconnect" : "Connect";
  self->pending = TRUE;
  update_ui (self);

  g_dbus_connection_call (self->bus, BUS_NAME, OBJECT_PATH, IFACE, method,
                          NULL, NULL, G_DBUS_CALL_FLAGS_NONE, -1, NULL,
                          call_finished, self);
}


static void
on_properties_changed (GDBusConnection *connection,
                       const char *sender,
                       const char *path,
                       const char *iface,
                       const char *signal,
                       GVariant *params,
                       gpointer data)
{
  PhoshXrayQuickSetting *self = data;
  g_autoptr (GVariant) changed = NULL;
  g_autoptr (GVariant) invalidated = NULL;
  g_autoptr (GVariant) value = NULL;
  const char *iface_name;

  g_variant_get (params, "(&s@a{sv}@as)", &iface_name, &changed, &invalidated);

  value = g_variant_lookup_value (changed, "State", G_VARIANT_TYPE_STRING);
  if (value) {
    g_free (self->state);
    self->state = g_variant_dup_string (value, NULL);
    g_clear_pointer (&value, g_variant_unref);
  }

  value = g_variant_lookup_value (changed, "ProfileName", G_VARIANT_TYPE_STRING);
  if (value) {
    g_free (self->profile);
    self->profile = g_variant_dup_string (value, NULL);
  }

  update_ui (self);
}


static void
read_properties (PhoshXrayQuickSetting *self, GVariant *reply)
{
  g_autoptr (GVariant) props = NULL;
  g_autoptr (GVariant) value = NULL;

  if (!reply)
    return;
  g_variant_get (reply, "(@a{sv})", &props);

  value = g_variant_lookup_value (props, "State", G_VARIANT_TYPE_STRING);
  if (value) {
    g_free (self->state);
    self->state = g_variant_dup_string (value, NULL);
    g_clear_pointer (&value, g_variant_unref);
  }
  value = g_variant_lookup_value (props, "ProfileName", G_VARIANT_TYPE_STRING);
  if (value) {
    g_free (self->profile);
    self->profile = g_variant_dup_string (value, NULL);
  }
  update_ui (self);
}


static void
get_all_finished (GObject *source, GAsyncResult *result, gpointer data)
{
  PhoshXrayQuickSetting *self = data;
  g_autoptr (GVariant) reply = NULL;
  g_autoptr (GError) error = NULL;

  reply = g_dbus_connection_call_finish (G_DBUS_CONNECTION (source), result, &error);
  if (!reply) {
    g_debug ("Xray service not available: %s", error->message);
    set_state (self, "off", NULL);
    phosh_status_icon_set_info (self->info, "Xray service unavailable");
    return;
  }
  read_properties (self, reply);
}


static void
refresh (PhoshXrayQuickSetting *self)
{
  if (!self->bus)
    return;
  g_dbus_connection_call (self->bus, BUS_NAME, OBJECT_PATH,
                          "org.freedesktop.DBus.Properties", "GetAll",
                          g_variant_new ("(s)", IFACE), G_VARIANT_TYPE ("(a{sv})"),
                          G_DBUS_CALL_FLAGS_NONE, -1, NULL,
                          get_all_finished, self);
}


static void
on_name_appeared (GDBusConnection *connection,
                  const char *name,
                  const char *owner,
                  gpointer data)
{
  refresh (data);
}


static void
on_name_vanished (GDBusConnection *connection,
                  const char *name,
                  gpointer data)
{
  PhoshXrayQuickSetting *self = data;
  set_state (self, "off", "");
  phosh_status_icon_set_info (self->info, "Xray service unavailable");
}


static void
on_bus_ready (GObject *source, GAsyncResult *result, gpointer data)
{
  PhoshXrayQuickSetting *self = data;
  g_autoptr (GError) error = NULL;

  self->bus = g_bus_get_finish (result, &error);
  if (!self->bus) {
    g_warning ("Cannot connect to the system bus: %s", error->message);
    update_ui (self);
    return;
  }

  self->subscription = g_dbus_connection_signal_subscribe (
    self->bus, BUS_NAME, "org.freedesktop.DBus.Properties", "PropertiesChanged",
    OBJECT_PATH, NULL, G_DBUS_SIGNAL_FLAGS_NONE,
    on_properties_changed, self, NULL);

  self->watcher = g_bus_watch_name_on_connection (
    self->bus, BUS_NAME, G_BUS_NAME_WATCHER_FLAGS_NONE,
    on_name_appeared, on_name_vanished, self, NULL);

  refresh (self);
}


static void
on_clicked (PhoshXrayQuickSetting *self)
{
  toggle (self);
}


static void
on_long_pressed (PhoshXrayQuickSetting *self)
{
  g_autoptr (GDesktopAppInfo) info = g_desktop_app_info_new (DESKTOP_ID);

  if (!info)
    return;

  g_autoptr (GError) error = NULL;
  if (!g_app_info_launch (G_APP_INFO (info), NULL, NULL, &error))
    g_warning ("Cannot launch Xray: %s", error->message);
}


static void
phosh_xray_quick_setting_finalize (GObject *object)
{
  PhoshXrayQuickSetting *self = (PhoshXrayQuickSetting *)object;

  if (self->subscription && self->bus)
    g_dbus_connection_signal_unsubscribe (self->bus, self->subscription);
  if (self->watcher)
    g_bus_unwatch_name (self->watcher);
  g_clear_object (&self->bus);
  g_free (self->state);
  g_free (self->profile);

  G_OBJECT_CLASS (phosh_xray_quick_setting_parent_class)->finalize (object);
}


static void
phosh_xray_quick_setting_class_init (PhoshXrayQuickSettingClass *klass)
{
  G_OBJECT_CLASS (klass)->finalize = phosh_xray_quick_setting_finalize;
}


static void
phosh_xray_quick_setting_init (PhoshXrayQuickSetting *self)
{
  self->state = g_strdup ("off");
  self->profile = g_strdup ("");

  self->info = PHOSH_STATUS_ICON (phosh_status_icon_new ());
  phosh_status_icon_set_icon_name (self->info, "xray-quick-setting-symbolic");
  phosh_status_icon_set_pixel_size (self->info, 16);
  gtk_widget_set_visible (GTK_WIDGET (self->info), TRUE);
  /* phosh doesn't export the setter; the property is what its .ui files use */
  g_object_set (self, "status-icon", self->info, NULL);

  g_signal_connect_swapped (self, "clicked", G_CALLBACK (on_clicked), self);
  g_signal_connect_swapped (self, "long-pressed", G_CALLBACK (on_long_pressed), self);

  update_ui (self);
  g_bus_get (G_BUS_TYPE_SYSTEM, NULL, on_bus_ready, self);
}


void
g_io_module_load (GIOModule *module)
{
  g_type_module_use (G_TYPE_MODULE (module));
  g_io_extension_point_implement (PHOSH_PLUGIN_EXTENSION_POINT_QUICK_SETTING_WIDGET,
                                  phosh_xray_quick_setting_get_type (),
                                  "xray-quick-setting", 10);
}


void
g_io_module_unload (GIOModule *module)
{
}


char **
g_io_phosh_plugin_xray_quick_setting_query (void)
{
  char *extension_points[] = { PHOSH_PLUGIN_EXTENSION_POINT_QUICK_SETTING_WIDGET, NULL };

  return g_strdupv (extension_points);
}
