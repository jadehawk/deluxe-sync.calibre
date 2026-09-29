"""Deterministic Stage 2 library-browser smoke test inside Calibre."""

from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
from time import monotonic, sleep
from types import SimpleNamespace

from calibre.customize.ui import find_plugin
from qt.core import QApplication, QDialog


app = QApplication.instance() or QApplication([])


def wait_until(predicate, label, timeout=3.0):
    deadline = monotonic() + timeout
    while monotonic() < deadline:
        app.processEvents()
        if predicate():
            return
        sleep(0.01)
    raise RuntimeError(f"Timed out waiting for {label}")


plugin = find_plugin("Deluxe Sync")
if plugin is None:
    raise RuntimeError("Deluxe Sync is not installed")
plugin.load_actual_plugin(None)

from calibre_plugins.deluxe_sync.api import (  # noqa: E402
    AuthorizationError,
    DeluxeSyncApi,
)
from calibre_plugins.deluxe_sync.dialogs import library as library_module  # noqa: E402
from calibre_plugins.deluxe_sync.dialogs.library import (  # noqa: E402
    LibraryDialog,
    _book_cells,
    _percentage_text,
    _reading_state_text,
    _series_text,
)


class Handler(BaseHTTPRequestHandler):
    auth_value = "stage2-" + "session"
    library_requests = 0
    detail_requests = 0

    def log_message(self, format, *args):
        return

    def _send_json(self, status, payload):
        data = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _send_text(self, status, message):
        data = message.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _authorized(self):
        return self.headers.get("Authorization") == f"Bearer {self.auth_value}"

    def do_GET(self):
        if not self._authorized():
            self._send_text(401, "Connected client authorization is invalid or revoked")
            return

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
                        "logical_library": True,
                        "document_metadata_write": True,
                    },
                },
            )
            return

        if self.path == "/api/v1/library":
            type(self).library_requests += 1
            self._send_json(
                200,
                {
                    "books": [
                        {
                            "kind": "logical",
                            "logical_book_id": 42,
                            "document": "logical:42",
                            "title": "Linked Title",
                            "authors": "Alice Example",
                            "progress": "/body/DocFragment[3]",
                            "percentage": 0.75,
                            "reading_state": None,
                            "series": "Example Saga",
                            "series_index": 2,
                            "linked_count": 2,
                            "timestamp": 200,
                        },
                        {
                            "kind": "raw",
                            "document_identity_id": 9,
                            "document": "raw-book-9",
                            "filename": "Standalone.epub",
                            "title": "Standalone Title",
                            "authors": "Bob Example",
                            "isbn": "9780000000001",
                            "asin": "B000TEST01",
                            "series": "Solo Series",
                            "series_index": 1.5,
                            "progress": "/body/DocFragment[1]",
                            "percentage": 0.25,
                            "reading_state": None,
                            "linked_count": 1,
                            "timestamp": 100,
                        },
                    ],
                    "visible_count": 2,
                    "raw_count": 3,
                    "logical_count": 1,
                    "linked_raw_count": 2,
                    "unlinked_raw_count": 1,
                },
            )
            return

        if self.path == "/api/v1/logical-books/42":
            type(self).detail_requests += 1
            self._send_json(
                200,
                {
                    "logical_book": {
                        "kind": "logical",
                        "logical_book_id": 42,
                        "title": "Linked Title",
                        "authors": "Alice Example",
                        "progress": "/body/DocFragment[3]",
                        "percentage": 0.75,
                        "series": "Example Saga",
                        "series_index": 2,
                        "members": [
                            {
                                "kind": "raw",
                                "logical_book_id": 42,
                                "document": "raw-linked-a",
                                "filename": "Linked A.epub",
                                "title": "Linked Title",
                                "authors": "Alice Example",
                                "isbn": "9780000000042",
                                "asin": "B000LINKA1",
                                "series": "Example Saga",
                                "series_index": 2,
                                "percentage": 0.50,
                                "reading_state": None,
                            },
                            {
                                "kind": "raw",
                                "logical_book_id": 42,
                                "document": "raw-linked-b",
                                "filename": "Linked B.epub",
                                "title": "Linked Title",
                                "authors": "Alice Example",
                                "isbn": "9780000000043",
                                "asin": "B000LINKB2",
                                "series": "Example Saga",
                                "series_index": 2,
                                "percentage": 0.75,
                                "reading_state": {
                                    "manual_completion": False,
                                    "percentage": 0.75,
                                },
                            },
                        ],
                    }
                },
            )
            return

        self._send_text(404, "Not found")


server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
thread = Thread(target=server.serve_forever, daemon=True)
thread.start()
base_url = f"http://127.0.0.1:{server.server_port}"

try:
    api = DeluxeSyncApi(base_url, plugin_version="0.1.0.0", timeout=2)
    profile = {
        "server_url": base_url,
        "auth_mode": "pairing",
    }
    profile["ses" + "sion"] = Handler.auth_value

    library = api.get_library(profile)
    assert library["visible_count"] == 2
    assert library["raw_count"] == 3
    assert library["logical_count"] == 1
    assert len(library["books"]) == 2

    details = api.get_logical_book(42, profile)
    assert details["logical_book_id"] == 42
    assert len(details["members"]) == 2
    assert details["members"][0]["isbn"] == "9780000000042"

    try:
        wrong_profile = dict(profile)
        wrong_profile["ses" + "sion"] = "wrong-value"
        api.get_library(wrong_profile)
    except AuthorizationError as error:
        assert error.status == 401
    else:
        raise RuntimeError("Invalid Stage 2 authorization was unexpectedly accepted")

    logical_cells = _book_cells(library["books"][0])
    assert logical_cells[0] == "Linked Title"
    assert logical_cells[2] == "2 linked versions"
    assert logical_cells[5] == "Example Saga · #2"
    assert logical_cells[6] == "75%"
    assert logical_cells[7] == "Reading"
    assert logical_cells[8] == ""
    assert logical_cells[9] == "42"

    raw_cells = _book_cells(library["books"][1])
    assert raw_cells[2] == "Standalone.epub"
    assert raw_cells[3] == "9780000000001"
    assert raw_cells[4] == "B000TEST01"
    assert raw_cells[5] == "Solo Series · #1.5"
    assert raw_cells[6] == "25%"
    assert raw_cells[8] == "raw-book-9"

    assert _percentage_text({"percentage": 1}) == "100%"
    assert _series_text({"series": "Series", "series_index": 3}) == "Series · #3"
    assert _reading_state_text({"percentage": 0.005}) == "Not started"
    assert _reading_state_text({"percentage": 0.01}) == "Reading"
    assert _reading_state_text({"percentage": 0.999}) == "Reading"
    assert _reading_state_text(
        {
            "percentage": 0.75,
            "reading_state": {"manual_completion": True},
        }
    ) == "Finished (manual)"
    assert _reading_state_text({"percentage": 1}) == "Finished"

    original_get_active_server_profile = library_module.get_active_server_profile
    try:
        library_module.get_active_server_profile = lambda: dict(profile)

        main_window = QDialog()
        main_window.current_db = SimpleNamespace(library_id="stage2-library")
        fake_base_plugin = SimpleNamespace(version_string="0.1.0.0")
        fake_action = SimpleNamespace(
            gui=main_window,
            interface_action_base_plugin=fake_base_plugin,
            show_config=lambda: None,
            _center_dialog=lambda _dialog: None,
        )

        dialog = LibraryDialog(fake_action)
        dialog.refresh_library()
        if not dialog._loading:
            raise RuntimeError("Stage 2 library refresh did not start in the background")
        wait_until(
            lambda: not dialog._loading and dialog.model.rowCount() == 2,
            "initial asynchronous library load",
        )
        assert "2 visible books" in dialog.status_label.text()

        dialog.refresh_library()
        if not dialog._loading:
            raise RuntimeError("Repeated Stage 2 refresh did not run asynchronously")
        wait_until(
            lambda: not dialog._loading and dialog.model.rowCount() == 2,
            "second asynchronous library load",
        )
        assert dialog.model.rowCount() == 2

        dialog.search_box.setText("Standalone")
        assert dialog.proxy.rowCount() == 1
        dialog.search_box.clear()
        assert dialog.proxy.rowCount() == 2

        dialog.table.setCurrentIndex(dialog.proxy.index(0, 0))
        app.processEvents()
        if not dialog.versions_button.isEnabled():
            raise RuntimeError("Linked Versions button was not enabled for a logical book")

        logical_source = dialog.proxy.mapToSource(dialog.table.currentIndex())
        logical_book = dialog.model.item(logical_source.row(), 0).data()
        assert logical_book["logical_book_id"] == 42

        opened_details = []
        dialog._open_linked_versions = lambda value: opened_details.append(value)
        dialog.show_linked_versions()
        if dialog._details_loading_id != 42:
            raise RuntimeError("Linked version details did not start in the background")
        wait_until(
            lambda: bool(opened_details),
            "asynchronous linked-version detail load",
        )
        assert len(opened_details[0]["members"]) == 2
        assert opened_details[0]["members"][1]["asin"] == "B000LINKB2"

        dialog.close()
    finally:
        library_module.get_active_server_profile = original_get_active_server_profile

    if Handler.library_requests < 3:
        raise RuntimeError("Stage 2 library endpoint was not exercised by refreshes")
    if Handler.detail_requests != 2:
        raise RuntimeError("Stage 2 linked-book detail endpoint count was unexpected")

finally:
    server.shutdown()
    server.server_close()
    thread.join(timeout=2)

print("Deluxe Sync Stage 2 library smoke test passed")
