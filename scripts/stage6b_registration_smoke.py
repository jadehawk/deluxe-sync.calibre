"""Deterministic Stage 6B Calibre-origin registration smoke test."""

from __future__ import annotations

import hashlib

from types import SimpleNamespace
from time import monotonic, sleep

from calibre.customize.ui import find_plugin
from qt.core import QApplication, QDialog, Qt


app = QApplication.instance() or QApplication([])


def wait_until(predicate, label, timeout=4.0):
    deadline = monotonic() + timeout
    while monotonic() < deadline:
        app.processEvents()
        if predicate():
            return
        sleep(0.01)
    raise RuntimeError(f"Timed out waiting for {label}")


class FakePrefs(dict):
    def __init__(self):
        super().__init__()
        self.defaults = {}


plugin = find_plugin("Deluxe Sync")
if plugin is None:
    raise RuntimeError("Deluxe Sync is not installed")
plugin.load_actual_plugin(None)

from calibre_plugins.deluxe_sync.api import (  # noqa: E402
    ApiError,
    CapabilityError,
    DeluxeSyncApi,
)
from calibre_plugins.deluxe_sync import bindings as bindings_module  # noqa: E402
from calibre_plugins.deluxe_sync.bindings import (  # noqa: E402
    bind_server_document,
    get_binding,
)
from calibre_plugins.deluxe_sync.dialogs import match_books as match_dialog_module  # noqa: E402
from calibre_plugins.deluxe_sync.dialogs.match_books import MatchBooksDialog  # noqa: E402
from calibre_plugins.deluxe_sync.document_registration import (  # noqa: E402
    calibre_document_id,
    registration_metadata,
)
from calibre_plugins.deluxe_sync.models import CalibreBook  # noqa: E402


LOCAL_COVER = b"\xff\xd8\xff\xe0stage6b-cover"
LOCAL_COVER_HASH = hashlib.sha256(LOCAL_COVER).hexdigest()


book = CalibreBook(
    library_uuid="stage6b-library",
    book_uuid="stage6b-book",
    book_id=42,
    title="Stage 6B Book",
    authors=("Alice Example", "Bob Example"),
    identifiers={
        "isbn": "9781234567890",
        "asin": "B0DTT5LV77",
    },
    series="Stage 6B Series",
    series_index=3,
    filenames=("z.pdf", "A.epub"),
    cover_hash=LOCAL_COVER_HASH,
)

document = calibre_document_id(book)
if document != "calibre:stage6b-library:stage6b-book":
    raise RuntimeError(f"Unexpected stable Calibre document id: {document}")

metadata = registration_metadata(book)
expected_metadata = {
    "filename": "A.epub",
    "title": "Stage 6B Book",
    "authors": "Alice Example, Bob Example",
    "isbn": "9781234567890",
    "asin": "B0DTT5LV77",
    "series": "Stage 6B Series",
    "series_index": 3.0,
}
if metadata != expected_metadata:
    raise RuntimeError(f"Unexpected registration metadata: {metadata}")

empty_book = CalibreBook(
    library_uuid="stage6b-library",
    book_uuid="empty-book",
    book_id=43,
    title="Only Title",
)
if registration_metadata(empty_book) != {"title": "Only Title"}:
    raise RuntimeError("Empty registration metadata fields were not omitted")

orphan_index_book = CalibreBook(
    library_uuid="stage6b-library",
    book_uuid="orphan-index",
    book_id=44,
    title="No Series",
    series="",
    series_index=1,
)
if registration_metadata(orphan_index_book) != {"title": "No Series"}:
    raise RuntimeError("Series index must not be registered without a series name")


# ---------------------------------------------------------------------------
# API contract: capability-gated POST with an exact metadata-only payload.
# ---------------------------------------------------------------------------


class RecordingApi(DeluxeSyncApi):
    def __init__(self):
        super().__init__("http://stage6b.invalid", plugin_version="0.1.0.0")
        self.calls = []

    def _request_json(self, path, **kwargs):
        self.calls.append((path, kwargs))
        payload = dict(kwargs.get("payload") or {})
        if path == "/api/v1/documents":
            return {
                "document": payload.get("document"),
                "source": payload.get("source"),
                "created": True,
                "metadata": {
                    key: value
                    for key, value in payload.items()
                    if key not in {"document", "source"}
                },
            }
        raise RuntimeError(f"Unexpected API path {path}")


api = RecordingApi()
enhanced_caps = {
    "capabilities": {
        "document_registration": True,
        "document_metadata": True,
    }
}
registered = api.register_document(
    document,
    metadata,
    {"auth_mode": "pairing"},
    capabilities_response=enhanced_caps,
)
if registered.get("document") != document:
    raise RuntimeError("API registration did not preserve the exact document id")
if len(api.calls) != 1:
    raise RuntimeError("API registration did not make exactly one request")
path, call = api.calls[0]
if path != "/api/v1/documents" or call.get("method") != "POST":
    raise RuntimeError(f"Unexpected registration request: {path} {call.get('method')}")
expected_payload = {
    "document": document,
    "source": "calibre",
    **expected_metadata,
}
if call.get("payload") != expected_payload:
    raise RuntimeError(f"Unexpected registration payload: {call.get('payload')}")
if any(key in call["payload"] for key in ("progress", "percentage", "device", "timestamp")):
    raise RuntimeError("Registration payload included reading-progress state")

blocked = RecordingApi()
try:
    blocked.register_document(
        document,
        metadata,
        {"auth_mode": "pairing"},
        capabilities_response={"capabilities": {"document_registration": False}},
    )
except CapabilityError:
    pass
else:
    raise RuntimeError("Registration was not blocked without the capability")
if blocked.calls:
    raise RuntimeError("Capability-blocked registration still made an HTTP request")

try:
    RecordingApi().register_document(
        document,
        {"progress": "/fake"},
        {"auth_mode": "pairing"},
        capabilities_response=enhanced_caps,
    )
except ApiError:
    pass
else:
    raise RuntimeError("Registration accepted a progress field")


# ---------------------------------------------------------------------------
# Binding helper: registration enters the normal enrolled binding flow.
# ---------------------------------------------------------------------------

original_prefs = bindings_module._PREFS
bindings_module._PREFS = FakePrefs()
try:
    saved = bind_server_document(book, document, "stage6b-profile")
    if saved.get("server_document") != document or saved.get("enrolled") is not True:
        raise RuntimeError("Registered document did not become an enrolled binding")
    if get_binding("stage6b-library", "stage6b-book", "stage6b-profile") is None:
        raise RuntimeError("Registered document binding was not persisted")
finally:
    bindings_module._PREFS = original_prefs


# ---------------------------------------------------------------------------
# Real Qt dialog with a fake enhanced server.
# ---------------------------------------------------------------------------


class FakeApi:
    capability_enabled = True
    browse_enabled = True
    created_documents = {}
    register_calls = []
    upload_calls = []
    library_calls = 0

    def __init__(self, server_url, *, plugin_version, timeout=10):
        self.server_url = server_url
        self.plugin_version = plugin_version

    def discover_capabilities(self, profile=None):
        return {
            "server_family": "kosync",
            "server_type": "enhanced",
            "server_version": "0.2.0.3",
            "capabilities": {
                "logical_library": True,
                "document_metadata": True,
                "document_registration": self.capability_enabled,
                "document_cover_sync": True,
                "calibre_bindings": False,
            },
        }

    def get_library(self, profile, capabilities_response=None):
        type(self).library_calls += 1
        if not type(self).browse_enabled:
            raise CapabilityError("browseable document list unavailable")
        books = []
        for raw_document, stored_metadata in self.created_documents.items():
            books.append(
                {
                    "kind": "raw",
                    "document": raw_document,
                    "title": stored_metadata.get("title"),
                    "authors": stored_metadata.get("authors"),
                    "filename": stored_metadata.get("filename"),
                    "isbn": stored_metadata.get("isbn"),
                    "asin": stored_metadata.get("asin"),
                    "series": stored_metadata.get("series"),
                    "series_index": stored_metadata.get("series_index"),
                    "progress": None,
                    "sync_percentage": None,
                    "percentage": 0,
                    "device": None,
                    "device_id": None,
                    "timestamp": None,
                    "linked_count": 1,
                }
            )
        return {"books": books}

    def register_document(
        self,
        raw_document,
        stored_metadata,
        profile,
        *,
        capabilities_response=None,
    ):
        if not self.capability_enabled:
            raise CapabilityError("registration unavailable")
        self.register_calls.append((raw_document, dict(stored_metadata)))
        created = raw_document not in self.created_documents
        if created:
            self.created_documents[raw_document] = dict(stored_metadata)
        return {
            "document": raw_document,
            "created": created,
            "metadata": dict(self.created_documents[raw_document]),
        }

    def get_document_metadata(
        self,
        raw_document,
        profile,
        *,
        capabilities_response=None,
    ):
        stored = self.created_documents.get(raw_document)
        if stored is None:
            raise ApiError("missing", status=404)
        return {
            "document": raw_document,
            "metadata": dict(stored),
        }

    def upload_document_cover(
        self,
        raw_document,
        cover_bytes,
        profile,
        *,
        capabilities_response=None,
    ):
        self.upload_calls.append((raw_document, bytes(cover_bytes)))
        return {"document": raw_document, "cover_hash": hashlib.sha256(cover_bytes).hexdigest()}

    def get_logical_book(self, logical_book_id, profile):
        raise RuntimeError("No logical books expected in Stage 6B smoke test")

    def get_effective_progress(self, *args, **kwargs):
        raise RuntimeError("Stage 6B must not read or write progress")

    def get_kosync_progress(self, *args, **kwargs):
        raise RuntimeError("Stage 6B must not read or write progress")

    def put_kosync_progress(self, *args, **kwargs):
        raise RuntimeError("Stage 6B must not read or write progress")


original_profile = match_dialog_module.get_active_server_profile
original_profile_id = match_dialog_module.get_active_server_profile_id
original_api = match_dialog_module.DeluxeSyncApi
original_selected_books = MatchBooksDialog._selected_books
original_prefs = bindings_module._PREFS

bindings_module._PREFS = FakePrefs()
match_dialog_module.get_active_server_profile = lambda: {
    "server_url": "http://stage6b.invalid",
    "auth_mode": "pairing",
}
match_dialog_module.get_active_server_profile_id = lambda: "stage6b-profile"
match_dialog_module.DeluxeSyncApi = FakeApi
MatchBooksDialog._selected_books = lambda self: [book]
FakeApi.capability_enabled = True
FakeApi.created_documents = {}
FakeApi.register_calls = []
FakeApi.upload_calls = []
FakeApi.library_calls = 0

dialog = None
try:
    fake_gui = QDialog()
    fake_gui.current_db = SimpleNamespace(
        library_id="stage6b-library",
        new_api=SimpleNamespace(cover=lambda book_id: LOCAL_COVER),
    )
    fake_action = SimpleNamespace(
        gui=fake_gui,
        interface_action_base_plugin=SimpleNamespace(version_string="0.1.0.0"),
        show_library_dialog=lambda: None,
    )
    dialog = MatchBooksDialog(fake_action)
    wait_until(
        lambda: not dialog._loading and dialog.results_model.rowCount() == 1,
        "Stage 6B initial no-match load",
    )
    if FakeApi.library_calls != 1:
        raise RuntimeError(f"Expected one initial library load, got {FakeApi.library_calls}")

    row = dialog.results_model.index(0, 0)
    dialog.results_table.setCurrentIndex(row)
    app.processEvents()

    if dialog._results[0].state != "no_match":
        raise RuntimeError(f"Expected no_match before registration: {dialog._results[0].state}")
    if not dialog.create_button.isEnabled():
        raise RuntimeError("Create on Server was not enabled for enhanced no-match book")
    if dialog.create_button.isHidden():
        raise RuntimeError("Create on Server was hidden despite safe document_registration")

    dialog.create_selected_on_server()
    wait_until(
        lambda: FakeApi.library_calls >= 2 and not dialog._creating and not dialog._loading,
        "Stage 6B registration, cover upload, and automatic refresh",
    )

    if len(FakeApi.register_calls) != 1:
        raise RuntimeError("Create on Server did not perform exactly one registration")
    raw_document, sent_metadata = FakeApi.register_calls[0]
    if raw_document != document or sent_metadata != expected_metadata:
        raise RuntimeError("Create on Server sent the wrong document or metadata")
    if any(key in sent_metadata for key in ("progress", "percentage", "device", "timestamp")):
        raise RuntimeError("Dialog registration attempted to send progress state")
    if FakeApi.upload_calls != [(document, LOCAL_COVER)]:
        raise RuntimeError(f"Create on Server did not upload the Calibre cover: {FakeApi.upload_calls!r}")
    if FakeApi.library_calls != 2:
        raise RuntimeError(f"Create on Server did not perform exactly one automatic match refresh: {FakeApi.library_calls}")

    binding = get_binding("stage6b-library", "stage6b-book", "stage6b-profile")
    if binding is None or binding.get("server_document") != document:
        raise RuntimeError("Create on Server did not persist the returned raw binding")
    if not dialog._results or dialog._results[0].state != "already_enrolled":
        raise RuntimeError("Registered binding did not resolve as Linked after automatic refresh")

    # Standard/unsupported server: preview matching still works, creation stays disabled.
    dialog.close()
    dialog = None
    bindings_module._PREFS = FakePrefs()
    FakeApi.capability_enabled = False
    FakeApi.created_documents = {}
    FakeApi.register_calls = []

    dialog = MatchBooksDialog(fake_action)
    wait_until(
        lambda: not dialog._loading and dialog.results_model.rowCount() == 1,
        "Stage 6B unsupported-capability load",
    )
    dialog.results_table.setCurrentIndex(dialog.results_model.index(0, 0))
    app.processEvents()
    if dialog.create_button.isEnabled():
        raise RuntimeError("Create on Server was enabled without document_registration")
    if not dialog.create_button.isHidden():
        raise RuntimeError("Create on Server remained visible without document_registration")
    if "sync it from your e-reader first" not in dialog.workflow_label.text():
        raise RuntimeError("Browse-only server did not show reader-first matching guidance")
    dialog.create_selected_on_server()
    if FakeApi.register_calls:
        raise RuntimeError("Unsupported server received a registration request")

    # Truly stock/progress-only KOSync: no browseable document list means no matching UI.
    dialog.close()
    dialog = None
    FakeApi.browse_enabled = False
    dialog = MatchBooksDialog(fake_action)
    wait_until(
        lambda: not dialog._loading,
        "Stage 6B non-browseable KOSync load",
    )
    if dialog.results_model.rowCount() != 0:
        raise RuntimeError("Non-browseable KOSync unexpectedly produced matching rows")
    if "Server book matching is unavailable" not in dialog.workflow_label.text():
        raise RuntimeError("Non-browseable KOSync did not explain progress-only behavior")
    if dialog.server_library_button.isEnabled():
        raise RuntimeError("Open Server Library stayed enabled without a browseable server library")
    if not dialog.create_button.isHidden():
        raise RuntimeError("Create on Server was visible for non-browseable KOSync")
finally:
    if dialog is not None:
        dialog.close()
    match_dialog_module.get_active_server_profile = original_profile
    match_dialog_module.get_active_server_profile_id = original_profile_id
    match_dialog_module.DeluxeSyncApi = original_api
    MatchBooksDialog._selected_books = original_selected_books
    bindings_module._PREFS = original_prefs


print("Deluxe Sync Stage 6B registration smoke test passed")
