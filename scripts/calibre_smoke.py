"""Smoke-test the installed Deluxe Sync plugin inside Calibre's Python runtime."""

import inspect
from pathlib import Path

from calibre.customize.ui import find_plugin
from qt.core import QApplication, QDialog, QTimer, QVBoxLayout


app = QApplication.instance() or QApplication([])

plugin = find_plugin("Deluxe Sync")
if plugin is None:
    raise RuntimeError("Deluxe Sync is not installed")
if str(getattr(plugin, "author", "")) != "Jadehawk":
    raise RuntimeError(f"Installed Deluxe Sync author is unexpected: {plugin.author!r}")
if tuple(plugin.version) != (0, 1, 0, 0):
    raise RuntimeError(f"Installed Deluxe Sync version tuple is unexpected: {plugin.version!r}")
if str(getattr(plugin, "version_string", "")) != "0.1.0.0":
    raise RuntimeError(
        f"Installed Deluxe Sync version string is unexpected: {getattr(plugin, 'version_string', None)!r}"
    )

action = plugin.load_actual_plugin(None)
if action.__class__.__name__ != "DeluxeSyncAction":
    raise RuntimeError(f"Unexpected action class: {action.__class__.__name__}")

# Calibre's real GUI loader links the actual action back to its base plugin
# before genesis(); reproduce that resource-loading context in this smoke test.
action.interface_action_base_plugin = plugin

if action.action_spec[1] is not None:
    raise RuntimeError("Custom plugin icons must not use Calibre's built-in icon theme slot")

plugin_icon = action._load_plugin_icon("icon.png")
if plugin_icon is None or plugin_icon.isNull():
    raise RuntimeError("Packaged Deluxe Sync icon.png could not be loaded as a QIcon")

main_window = QDialog()
main_window.setGeometry(180, 120, 1200, 800)
action.gui = main_window

def assert_centered_on_main(dialog, label):
    main_center = main_window.frameGeometry().center()
    dialog_center = dialog.frameGeometry().center()
    if (
        abs(main_center.x() - dialog_center.x()) > 3
        or abs(main_center.y() - dialog_center.y()) > 3
    ):
        raise RuntimeError(f"{label} was not centered on the Calibre window")


center_probe = QDialog()
center_probe.resize(460, 180)
action._center_dialog(center_probe)
assert_centered_on_main(center_probe, "Deluxe Sync dialog centering helper")

from calibre_plugins.deluxe_sync.logger import REDACTED, get_logger, log_path
from calibre_plugins.deluxe_sync.settings import (
    get_column_mappings,
    set_column_mappings,
)


LOGGER = get_logger("smoke")

config_widget = plugin.config_widget()
if config_widget is None or config_widget.__class__.__name__ != "ConfigWidget":
    raise RuntimeError("Deluxe Sync configuration widget did not load")

for attribute in (
    "stack",
    "landing_page",
    "connection_page",
    "columns_page",
    "server_summary_label",
    "columns_summary_label",
):
    if not hasattr(config_widget, attribute):
        raise RuntimeError(f"Configuration landing control missing: {attribute}")

app.processEvents()
if config_widget.connection_page is not None:
    raise RuntimeError("Server Connection page was eagerly created on config open")
if config_widget.columns_page is not None:
    raise RuntimeError("Column Mappings page was eagerly created on config open")

host_dialog = QDialog()
host_layout = QVBoxLayout(host_dialog)
host_layout.addWidget(config_widget)

config_widget._resize_for_landing()
if host_dialog.width() < 600 or host_dialog.height() < 360:
    raise RuntimeError("Landing page dialog did not use the compact minimum size")
assert_centered_on_main(host_dialog, "Configuration landing page")

config_widget.show_connection()
app.processEvents()
connection = config_widget.connection_page
if connection is None:
    raise RuntimeError("Server Connection page was not created on first navigation")
if config_widget.stack.currentWidget() is not connection:
    raise RuntimeError("Server Connection navigation did not select its page")
for attribute in (
    "server_url",
    "registration_code",
    "connect_button",
    "test_button",
    "disconnect_button",
    "status_label",
    "capabilities_label",
):
    if not hasattr(connection, attribute):
        raise RuntimeError(f"Server Connection control missing: {attribute}")
if connection.registration_code.text():
    raise RuntimeError("Registration code field must not load a saved value")
if connection.server_url.minimumWidth() < 480:
    raise RuntimeError("Server URL field minimum width is too small")
if host_dialog.width() < 780 or host_dialog.height() < 620:
    raise RuntimeError("Server Connection page did not expand the dialog")
assert_centered_on_main(host_dialog, "Server Connection page")

config_widget.show_columns()
app.processEvents()
columns_page = config_widget.columns_page
if columns_page is None:
    raise RuntimeError("Column Mappings page was not created on first navigation")
if config_widget.stack.currentWidget() is not columns_page:
    raise RuntimeError("Column Mappings navigation did not select its page")
expected_mappings = {"progress", "status", "last_location", "last_sync", "annotations", "vocabulary"}
if set(columns_page.combos) != expected_mappings:
    raise RuntimeError("Column Mappings page does not expose the expected mappings")
if host_dialog.width() < 680 or host_dialog.height() < 440:
    raise RuntimeError("Column Mappings page did not size the dialog")
assert_centered_on_main(host_dialog, "Column Mappings page")

config_widget.show_landing()
app.processEvents()
if config_widget.stack.currentWidget() is not config_widget.landing_page:
    raise RuntimeError("Back navigation did not return to the landing page")
if host_dialog.width() > 700 or host_dialog.height() > 450:
    raise RuntimeError("Landing page remained unnecessarily large after returning")

original_mappings = get_column_mappings()
test_mappings = {
    "progress": "#smoke_progress",
    "status": "#smoke_status",
    "last_location": "",
    "last_sync": "",
}
try:
    set_column_mappings(test_mappings)
    if get_column_mappings() != test_mappings:
        raise RuntimeError("Column mapping settings did not persist")
finally:
    set_column_mappings(original_mappings)

import calibre_plugins.deluxe_sync.action as action_module

genesis_source = inspect.getsource(action_module.DeluxeSyncAction.genesis)
if "self.qaction.triggered.connect(self.show_dashboard)" not in genesis_source:
    raise RuntimeError("Primary toolbar action is not wired to the Deluxe Sync dashboard")
for required_menu_text in ('_("Dashboard")', '_("Configuration")', '_("About")'):
    if required_menu_text not in genesis_source:
        raise RuntimeError(f"Missing simplified menu entry: {required_menu_text}")
for removed_action_id in (
    "deluxe_sync_sync_selected",
    "deluxe_sync_sync_preview",
    "deluxe_sync_match_selected",
    "deluxe_sync_library",
):
    if removed_action_id in genesis_source:
        raise RuntimeError(f"Legacy direct menu action remained: {removed_action_id}")
if genesis_source.count("menu.addSeparator()") != 2:
    raise RuntimeError("Simplified menu does not contain exactly two dividers")

captured_sync_launches = []

original_sync_dialog = action_module.SyncSelectedBooksDialog
original_center_dialog = action._center_dialog

class _SyncLaunchProbe:
    def __init__(
        self,
        plugin_action,
        *,
        auto_sync=False,
        auto_show_completion=False,
    ):
        captured_sync_launches.append(
            (bool(auto_sync), bool(auto_show_completion))
        )

    def exec(self):
        return 0

try:

    action_module.SyncSelectedBooksDialog = _SyncLaunchProbe
    action._center_dialog = lambda dialog: None

    action.sync_selected_books_now()
    action.show_sync_selected_dialog()
finally:

    action_module.SyncSelectedBooksDialog = original_sync_dialog
    action._center_dialog = original_center_dialog

if captured_sync_launches != [(True, True), (False, False)]:
    raise RuntimeError(
        f"Direct/menu sync launch modes were unexpected: {captured_sync_launches}"
    )

LOGGER.debug(
    "Smoke logging registration_code=%s client_uuid=%s library_uuid=%s authorization=%s",
    REDACTED,
    REDACTED,
    REDACTED,
    REDACTED,
)
log_text = Path(log_path()).read_text(encoding="utf-8")
if "[Deluxe-Sync]" not in log_text or "[REDACTED]" not in log_text:
    raise RuntimeError("Persistent Deluxe Sync log did not contain prefixed redacted output")


def close_active_modal():
    dialog = QApplication.activeModalWidget()
    if dialog is None:
        QTimer.singleShot(50, close_active_modal)
        return
    dialog.accept()


QTimer.singleShot(50, close_active_modal)
action.show_stage_zero_dialog()

print("Deluxe Sync Calibre smoke test passed")
