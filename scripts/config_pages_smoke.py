"""Deterministic smoke test for Deluxe Sync configuration subpages."""

from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

from calibre.customize.ui import find_plugin
from qt.core import QApplication, QMessageBox


app = QApplication.instance() or QApplication([])

plugin = find_plugin("Deluxe Sync")
if plugin is None:
    raise RuntimeError("Deluxe Sync is not installed")
plugin.load_actual_plugin(None)

from calibre.gui2.preferences.create_custom_column import CreateNewCustomColumn
from calibre_plugins.deluxe_sync.config import ConfigWidget
import calibre_plugins.deluxe_sync.dialogs.connection as connection_module
from calibre_plugins.deluxe_sync.dialogs.columns import (
    CREATE_COLUMN_SENTINEL,
    MAPPING_SPECS,
    ColumnMappingsPage,
)
from calibre_plugins.deluxe_sync.dialogs.connection import ConnectionPage
from calibre_plugins.deluxe_sync.settings import (
    forget_server_profile,
    get_active_server_profile,
    get_column_mappings,
    get_server_url,
    save_server_profile,
    set_column_mappings,
    set_server_url,
)


class Handler(BaseHTTPRequestHandler):
    session_value = "stage1-config-smoke-session"

    def log_message(self, format, *args):
        return

    def _send_json(self, status, payload):
        data = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path == "/api/v1/capabilities":
            self._send_json(
                200,
                {
                    "server_version": "test",
                    "server_family": "techy-notes",
                    "server_type": "enhanced",
                    "capabilities": {
                        "calibre_client_pairing": True,
                        "calibre_client_pairing_version": 1,
                        "document_metadata_write": True,
                        "document_metadata_write_version": 1,
                        "logical_library": True,
                        "change_journal": True,
                        "annotations": True,
                        "vocabulary_builder": True,
                    },
                },
            )
            return

        if self.path == "/api/v1/connected-clients/me":
            if self.headers.get("Authorization") != f"Bearer {self.session_value}":
                self._send_json(401, {"message": "invalid"})
                return
            self._send_json(
                200,
                {
                    "server_version": "test",
                    "server_family": "techy-notes",
                    "server_type": "enhanced",
                    "client": {
                        "id": 77,
                        "scope": "calibre_sync_v1",
                        "client_uuid": "smoke-client",
                        "library_uuid": "stage1-config-smoke-library",
                        "device_name": "Smoke Desktop",
                        "library_name": "Smoke Library",
                    },
                },
            )
            return

        if self.headers.get("Authorization") != f"Bearer {self.session_value}":
            self._send_json(401, {"message": "invalid"})
            return

        if self.path == "/syncs/documents":
            self._send_json(200, {"documents": []})
            return

        if self.path == "/api/v1/linked-services":
            self._send_json(
                200,
                {
                    "accounts": [
                        {
                            "service_id": "bookfusion",
                            "display_name": "BookFusion",
                            "status": "connected",
                            "enabled": True,
                            "account_label": "Smoke Account",
                        }
                    ]
                },
            )
            return

        if self.path == "/api/v1/external-sync-servers":
            self._send_json(
                200,
                {
                    "servers": [
                        {
                            "name": "Smoke Relay",
                            "status": "connected",
                            "server_type": "standard",
                            "effective_mode": "standard",
                        }
                    ]
                },
            )
            return

        self._send_json(404, {"message": "not found"})


class FakeDb:
    library_id = "stage1-config-smoke-library"
    library_path = "C:/Smoke Library"

    def custom_field_metadata(self, include_composites=False):
        return {
            "#smoke_progress": {
                "name": "Smoke Progress",
                "datatype": "float",
            },
            "#smoke_status": {
                "name": "Smoke Status",
                "datatype": "text",
            },
            "#smoke_location": {
                "name": "Smoke Location",
                "datatype": "comments",
            },
            "#smoke_sync": {
                "name": "Smoke Last Sync",
                "datatype": "datetime",
            },
            "#smoke_annotations": {
                "name": "Smoke Annotations",
                "datatype": "comments",
            },
            "#smoke_vocabulary": {
                "name": "Smoke Vocabulary",
                "datatype": "comments",
            },
            "#smoke_review": {
                "name": "Smoke Review",
                "datatype": "comments",
            },
            "#smoke_count": {
                "name": "Smoke Count",
                "datatype": "int",
            },
        }


class FakeGui:
    current_db = FakeDb()
    must_restart_before_config = False

    def __init__(self):
        self.quit_calls = []

    def quit(self, *, restart=False):
        self.quit_calls.append(bool(restart))


class FakeBasePlugin:
    version_string = "0.1.0.0"


class FakeAction:
    gui = FakeGui()
    interface_action_base_plugin = FakeBasePlugin()


class ExplodingApi:
    def __init__(self, *args, **kwargs):
        raise RuntimeError(
            "Configuration construction must not instantiate the Deluxe Sync API client"
        )


class FakeColumnCreator:
    Result = CreateNewCustomColumn.Result

    def __init__(self):
        self.columns = {}
        self.calls = []

    def current_columns(self):
        return dict(self.columns)

    def create_column(
        self,
        lookup_name,
        column_heading,
        datatype,
        is_multiple,
        display=None,
        generate_unused_lookup_name=False,
        freeze_lookup_name=True,
    ):
        self.calls.append(
            {
                "lookup_name": lookup_name,
                "column_heading": column_heading,
                "datatype": datatype,
                "is_multiple": is_multiple,
                "display": dict(display or {}),
                "generate_unused_lookup_name": generate_unused_lookup_name,
                "freeze_lookup_name": freeze_lookup_name,
            }
        )
        if lookup_name in self.columns:
            return (self.Result.DUPLICATE_KEY, lookup_name)

        self.columns[lookup_name] = {
            "name": column_heading,
            "datatype": datatype,
        }
        return (self.Result.COLUMN_ADDED, lookup_name)


def combo_values(combo):
    return {
        str(combo.itemData(index) or "")
        for index in range(combo.count())
    }


server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
thread = Thread(target=server.serve_forever, daemon=True)
thread.start()

library_uuid = FakeDb.library_id
original_server_url = get_server_url()
original_profile = get_active_server_profile()
original_mappings = get_column_mappings()

try:
    server_url = f"http://127.0.0.1:{server.server_port}"
    profile = {
        "server_url": server_url,
        "auth_mode": "pairing",
        "client_id": 77,
        "scope": "calibre_sync_v1",
        "server_family": "techy-notes",
        "server_type": "enhanced",
        "server_version": "test",
        "capabilities": {
            "calibre_client_pairing": True,
            "calibre_client_pairing_version": 1,
            "document_metadata_write": True,
            "document_metadata_write_version": 1,
            "logical_library": True,
            "change_journal": True,
            "annotations": True,
            "vocabulary_builder": True,
        },
    }
    profile["ses" + "sion"] = Handler.session_value
    save_server_profile(profile)

    real_api_class = connection_module.DeluxeSyncApi
    connection_module.DeluxeSyncApi = ExplodingApi
    try:
        landing_config = ConfigWidget(FakeAction())
        app.processEvents()

        if landing_config.connection_page is not None:
            raise RuntimeError(
                "Opening the landing page eagerly created Server Connection"
            )
        if landing_config.columns_page is not None:
            raise RuntimeError(
                "Opening the landing page eagerly created Column Mappings"
            )
        if "Configured" not in landing_config.server_summary_label.text():
            raise RuntimeError(
                "Landing page did not render the saved registration from local state"
            )

        landing_config.show_connection()
        app.processEvents()
        lazy_connection = landing_config.connection_page
        if lazy_connection is None:
            raise RuntimeError(
                "Server Connection page was not lazily created on navigation"
            )
        if "Saved server profile loaded" not in lazy_connection.status_label.text():
            raise RuntimeError(
                "Server Connection page did not render saved state locally"
            )
    finally:
        connection_module.DeluxeSyncApi = real_api_class

    connection = ConnectionPage(
        FakeAction(),
        on_back=lambda: None,
    )
    connection.server_url.setText(server_url)
    connection.test_connection()

    status_text = connection.status_label.text()
    if "Server Status" not in status_text or "Connected" not in status_text:
        raise RuntimeError("Test Connection did not refresh connected-client details")
    capabilities_text = connection.capabilities_label.text()
    if "Book metadata" not in capabilities_text or "Available (v1)" not in capabilities_text:
        raise RuntimeError("Test Connection did not refresh capability details")
    linked_text = connection.linked_services_label.text()
    if "BookFusion" not in linked_text or "Connected" not in linked_text:
        raise RuntimeError("Linked Services summary did not load")
    relay_text = connection.relay_servers_label.text()
    if "Smoke Relay" not in relay_text or "Connected" not in relay_text:
        raise RuntimeError("KOSync Relay summary did not load")

    columns = ColumnMappingsPage(
        FakeAction(),
        on_back=lambda: None,
    )

    for spec in MAPPING_SPECS:
        tooltip = columns.combos[spec.key].toolTip()
        if tooltip != spec.help_text:
            raise RuntimeError(f"Unexpected tooltip for {spec.key}: {tooltip!r}")
        if len(tooltip) > 60:
            raise RuntimeError(f"Tooltip is too long for {spec.key}: {tooltip!r}")
        if "Create new Deluxe Sync column" in tooltip:
            raise RuntimeError(f"Technical create-column instructions leaked into {spec.key} tooltip")

    expected = {
        "progress": "#smoke_progress",
        "status": "#smoke_status",
        "last_location": "#smoke_location",
        "last_sync": "#smoke_sync",
        "annotations": "#smoke_annotations",
        "vocabulary": "#smoke_vocabulary",
        "review_note": "#smoke_review",
    }
    for mapping_key, column_key in expected.items():
        combo = columns.combos[mapping_key]
        if column_key not in combo_values(combo):
            raise RuntimeError(
                f"Compatible column missing from {mapping_key}: {column_key}"
            )
        if "#smoke_count" in combo_values(combo):
            raise RuntimeError(
                f"Incompatible integer column leaked into {mapping_key}"
            )
        combo.setCurrentIndex(combo.findData(column_key))

    columns.save_settings()
    if get_column_mappings() != expected:
        raise RuntimeError("Column Mapping page did not persist selected mappings")

    creation_columns = ColumnMappingsPage(
        FakeAction(),
        on_back=lambda: None,
    )
    fake_creator = FakeColumnCreator()
    creation_columns._custom_column_creator = fake_creator

    expected_created = {}
    for spec in MAPPING_SPECS:
        combo = creation_columns.combos[spec.key]
        create_index = combo.findData(CREATE_COLUMN_SENTINEL)
        if create_index < 0:
            raise RuntimeError(
                f"Create-new-column option missing from {spec.key} dropdown"
            )
        if spec.create_lookup not in combo.itemText(create_index):
            raise RuntimeError(
                f"Create-new-column option does not name {spec.create_lookup}"
            )
        if not spec.create_lookup.startswith("#ds_"):
            raise RuntimeError(
                f"Recommended Deluxe Sync lookup does not use #ds_: {spec.create_lookup}"
            )
        if spec.create_lookup != spec.create_lookup.lower():
            raise RuntimeError(
                f"Calibre custom-column lookup is not lowercase: {spec.create_lookup}"
            )

        combo.setCurrentIndex(create_index)
        app.processEvents()

        if combo.currentData() != spec.create_lookup:
            raise RuntimeError(
                f"Created custom column was not selected for {spec.key}"
            )

        call = fake_creator.calls[-1]
        if call["lookup_name"] != spec.create_lookup:
            raise RuntimeError(f"Wrong create lookup for {spec.key}")
        if call["column_heading"] != spec.create_heading:
            raise RuntimeError(f"Wrong create heading for {spec.key}")
        if call["datatype"] != spec.create_datatype:
            raise RuntimeError(f"Wrong create datatype for {spec.key}")
        if call["is_multiple"] is not False:
            raise RuntimeError(f"Created column unexpectedly allows multiple values: {spec.key}")
        if call["freeze_lookup_name"] is not True:
            raise RuntimeError(f"Deluxe Sync lookup name was not frozen: {spec.key}")
        if spec.key in {"annotations", "vocabulary"}:
            if call["display"].get("interpret_as") != "html":
                raise RuntimeError("Annotation archive column was not configured as HTML")
            if call["display"].get("heading_position") != "hide":
                raise RuntimeError("Annotation archive column did not hide the redundant heading")

        expected_created[spec.key] = spec.create_lookup

    if not creation_columns.restart_required:
        raise RuntimeError("Creating custom columns did not request a Calibre restart")
    if "Restart Calibre" not in creation_columns.creation_status_label.text():
        raise RuntimeError("Column creation did not show the restart notice")

    creation_columns.save_settings()
    if get_column_mappings() != expected_created:
        raise RuntimeError("Created Deluxe Sync column mappings did not persist")

    # Calibre's plugin config wrapper calls validate() once before accepting
    # and once again before save_settings(). The restart choice must be shown
    # only once and must support Cancel / Close / Restart Calibre.
    restart_box = QMessageBox()
    restart_box.setIcon(QMessageBox.Icon.Warning)
    restart_button = restart_box.addButton(
        "Restart Calibre", QMessageBox.ButtonRole.AcceptRole
    )
    restart_box.addButton("Close", QMessageBox.ButtonRole.ActionRole)
    cancel_button = restart_box.addButton(
        "Cancel", QMessageBox.ButtonRole.RejectRole
    )
    restart_box.setDefaultButton(restart_button)
    restart_box.setEscapeButton(cancel_button)
    restart_box.close()

    FakeAction.gui.must_restart_before_config = True

    cancel_config = ConfigWidget(FakeAction())
    cancel_config._prompt_restart_choice = lambda: "cancel"
    if cancel_config.validate():
        raise RuntimeError("Restart prompt Cancel did not keep Preferences open")
    if cancel_config._restart_after_save:
        raise RuntimeError("Restart prompt Cancel incorrectly scheduled a restart")

    close_prompts = []
    close_config = ConfigWidget(FakeAction())

    def choose_close():
        close_prompts.append("close")
        return "close"

    close_config._prompt_restart_choice = choose_close
    if not close_config.validate() or not close_config.validate():
        raise RuntimeError("Restart prompt Close did not allow Preferences to close")
    if close_prompts != ["close"]:
        raise RuntimeError("Restart prompt was shown more than once during Calibre validation")
    quit_count = len(FakeAction.gui.quit_calls)
    close_config.save_settings()
    app.processEvents()
    if len(FakeAction.gui.quit_calls) != quit_count:
        raise RuntimeError("Restart prompt Close unexpectedly restarted Calibre")

    restart_prompts = []
    restart_config = ConfigWidget(FakeAction())

    def choose_restart():
        restart_prompts.append("restart")
        return "restart"

    restart_config._prompt_restart_choice = choose_restart
    if not restart_config.validate() or not restart_config.validate():
        raise RuntimeError("Restart Calibre choice did not allow Preferences to close")
    if restart_prompts != ["restart"]:
        raise RuntimeError("Restart Calibre choice was prompted more than once")
    quit_count = len(FakeAction.gui.quit_calls)
    restart_config.save_settings()
    app.processEvents()
    if len(FakeAction.gui.quit_calls) != quit_count + 1:
        raise RuntimeError("Restart Calibre choice did not invoke Calibre quit(restart=True)")
    if FakeAction.gui.quit_calls[-1] is not True:
        raise RuntimeError("Restart Calibre choice did not request a restart")

    FakeAction.gui.must_restart_before_config = False

finally:
    if original_profile is None:
        forget_server_profile()
    else:
        save_server_profile(original_profile)
    set_server_url(original_server_url)
    set_column_mappings(original_mappings)
    server.shutdown()
    server.server_close()
    thread.join(timeout=2)

print("Deluxe Sync configuration pages smoke test passed")
