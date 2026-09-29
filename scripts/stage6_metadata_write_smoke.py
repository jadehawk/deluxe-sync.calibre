"""Deterministic Stage 6 metadata-write smoke test inside Calibre."""

from __future__ import annotations

from types import SimpleNamespace
from time import monotonic, sleep

from calibre.customize.ui import find_plugin
from qt.core import QApplication, QWidget


app = QApplication.instance() or QApplication([])


def assert_equal(actual, expected, label):
    if actual != expected:
        raise RuntimeError(f"{label}: expected {expected!r}, got {actual!r}")


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
    ApiError,
    CapabilityError,
    DeluxeSyncApi,
)
from calibre_plugins.deluxe_sync.dialogs import sync_preview as preview_dialog_module  # noqa: E402
from calibre_plugins.deluxe_sync.dialogs.sync_preview import SyncPreviewDialog  # noqa: E402
from calibre_plugins.deluxe_sync.metadata_preview import (  # noqa: E402
    POLICY_CALIBRE_WINS,
    POLICY_DO_NOT_SYNC,
    clean_metadata_policies,
)
from calibre_plugins.deluxe_sync.metadata_sync import (  # noqa: E402
    build_metadata_patch,
    metadata_patch_mismatches,
)
from calibre_plugins.deluxe_sync.models import CalibreBook  # noqa: E402
from calibre_plugins.deluxe_sync.settings import (  # noqa: E402
    get_metadata_policies,
    set_metadata_policies,
)


# ---------------------------------------------------------------------------
# Pure write planning: only approved Calibre -> Server differences may enter
# a PATCH, and empty-value protection from Stage 5 must remain intact.
# ---------------------------------------------------------------------------

local_book = CalibreBook(
    library_uuid="stage6-library",
    book_uuid="linked-book",
    book_id=1,
    title="Calibre Title",
    authors=("Alice Example",),
    identifiers={"isbn": "978-1-2345-6789-0", "asin": "B000LOCAL"},
    series="Calibre Series",
    series_index=2,
)
server_metadata = {
    "filename": "Linked.epub",
    "title": "Server Title",
    "authors": "Alice Example",
    "isbn": "9781234567890",
    "asin": "B000REMOTE",
    "series": "Server Series",
    "series_index": 2.5,
}
default_policies = clean_metadata_policies({})
patch = build_metadata_patch(local_book, server_metadata, default_policies)
if "cover" in patch:
    raise RuntimeError("Cover leaked into the text metadata PATCH")
assert_equal(
    patch,
    {
        "title": "Calibre Title",
        "series": "Calibre Series",
        "series_index": 2,
    },
    "approved metadata patch",
)

empty_series_book = CalibreBook(
    library_uuid="stage6-library",
    book_uuid="empty-series",
    book_id=2,
    title="Same Title",
    authors=("Alice Example",),
    series="",
    series_index=None,
)
empty_patch = build_metadata_patch(
    empty_series_book,
    {
        "title": "Same Title",
        "authors": "Alice Example",
        "series": "Keep This Series",
        "series_index": 4,
    },
    {
        "title": POLICY_CALIBRE_WINS,
        "authors": POLICY_CALIBRE_WINS,
        "isbn": POLICY_DO_NOT_SYNC,
        "asin": POLICY_DO_NOT_SYNC,
        "series": POLICY_CALIBRE_WINS,
        "series_index": POLICY_CALIBRE_WINS,
    },
)
if "series" in empty_patch or "series_index" in empty_patch:
    raise RuntimeError("Empty Calibre metadata would erase a non-empty server value")

orphan_index_patch = build_metadata_patch(
    CalibreBook(
        library_uuid="stage6-library",
        book_uuid="orphan-index",
        book_id=3,
        title="Same Title",
        authors=("Alice Example",),
        series="",
        series_index=1,
    ),
    {
        "title": "Same Title",
        "authors": "Alice Example",
        "series": "",
        "series_index": 1,
    },
    default_policies,
)
assert_equal(orphan_index_patch, {"series_index": None}, "orphan series index cleanup")

verified_metadata = dict(server_metadata)
verified_metadata.update(patch)
assert_equal(
    metadata_patch_mismatches(patch, verified_metadata),
    (),
    "post-PATCH verification",
)
bad_verified_metadata = dict(verified_metadata)
bad_verified_metadata["series_index"] = 9
assert_equal(
    metadata_patch_mismatches(patch, bad_verified_metadata),
    ("series_index",),
    "verification mismatch detection",
)


# ---------------------------------------------------------------------------
# API contract: exact metadata endpoint, PATCH method, exact field payload, and
# capability blocking. No progress endpoint is involved.
# ---------------------------------------------------------------------------

api = DeluxeSyncApi("https://example.invalid", plugin_version="0.1.0.0")
request_calls = []


def fake_request(path, **kwargs):
    request_calls.append((path, dict(kwargs)))
    return {
        "document": "raw/doc",
        "metadata": dict(verified_metadata),
        "updated_at": 123,
    }


api._request_json = fake_request
api.patch_document_metadata(
    "raw/doc",
    {"title": "Calibre Title", "series_index": 2},
    {"auth_mode": "pairing", "session": "redacted"},
    capabilities_response={
        "capabilities": {
            "document_metadata": True,
            "document_metadata_write": True,
        }
    },
)
assert_equal(len(request_calls), 1, "metadata PATCH request count")
path, kwargs = request_calls[0]
assert_equal(path, "/api/v1/documents/raw%2Fdoc/metadata", "metadata PATCH path")
assert_equal(kwargs.get("method"), "PATCH", "metadata PATCH method")
assert_equal(
    kwargs.get("payload"),
    {"title": "Calibre Title", "series_index": 2},
    "metadata PATCH payload",
)

request_calls.clear()
try:
    api.patch_document_metadata(
        "raw/doc",
        {"title": "Blocked"},
        {"auth_mode": "pairing", "session": "redacted"},
        capabilities_response={"capabilities": {"document_metadata_write": False}},
    )
except CapabilityError:
    pass
else:
    raise RuntimeError("Metadata PATCH was not blocked without write capability")
assert_equal(request_calls, [], "blocked metadata PATCH requests")

try:
    api.patch_document_metadata(
        "raw/doc",
        {"progress": 50},
        {"auth_mode": "pairing", "session": "redacted"},
        capabilities_response={"capabilities": {"document_metadata_write": True}},
    )
except ApiError:
    pass
else:
    raise RuntimeError("Unsupported progress field was accepted by metadata PATCH")


# ---------------------------------------------------------------------------
# Real Qt dialog with a fake enhanced API. Preview remains read-only; clicking
# Sync Metadata performs one PATCH, immediately re-reads metadata, verifies it,
# and never invokes a progress API.
# ---------------------------------------------------------------------------


class FakeGui(QWidget):
    def __init__(self):
        super().__init__()
        self.current_db = SimpleNamespace(library_id="stage6-library")


class FakeMetadataApi:
    metadata_requests = []
    patch_requests = []
    progress_calls = []
    capability_requests = 0
    write_supported = True
    server_metadata = dict(server_metadata)

    def __init__(self, _server_url, *, plugin_version, timeout=10):
        self.plugin_version = plugin_version
        self.timeout = timeout

    def discover_capabilities(self, _profile):
        type(self).capability_requests += 1
        return {
            "server_type": "enhanced",
            "capabilities": {
                "document_metadata": True,
                "document_metadata_write": type(self).write_supported,
            },
        }

    def get_document_metadata(
        self,
        document,
        _profile,
        *,
        capabilities_response=None,
    ):
        if capabilities_response is None:
            raise RuntimeError("Dialog failed to reuse capability discovery")
        type(self).metadata_requests.append(document)
        return {
            "document": document,
            "metadata": dict(type(self).server_metadata),
            "updated_at": 999,
        }

    def patch_document_metadata(
        self,
        document,
        metadata,
        _profile,
        *,
        capabilities_response=None,
    ):
        if capabilities_response is None:
            raise RuntimeError("Dialog failed to pass write capabilities")
        if not capabilities_response["capabilities"].get("document_metadata_write"):
            raise RuntimeError("Dialog attempted metadata write without capability")
        type(self).patch_requests.append((document, dict(metadata)))
        type(self).server_metadata.update(metadata)
        return {
            "document": document,
            "metadata": dict(type(self).server_metadata),
            "updated_at": 1000,
        }

    def get_effective_progress(self, *_args, **_kwargs):
        type(self).progress_calls.append("effective")
        raise RuntimeError("Stage 6 metadata sync called a progress reader")

    def get_kosync_progress(self, *_args, **_kwargs):
        type(self).progress_calls.append("kosync-get")
        raise RuntimeError("Stage 6 metadata sync called a KOSync progress reader")

    def put_kosync_progress(self, *_args, **_kwargs):
        type(self).progress_calls.append("kosync-put")
        raise RuntimeError("Stage 6 metadata sync called a progress writer")


original_selected = preview_dialog_module.selected_calibre_books
original_profile = preview_dialog_module.get_active_server_profile
original_profile_id = preview_dialog_module.get_active_server_profile_id
original_bindings = preview_dialog_module.get_library_bindings
original_api = preview_dialog_module.DeluxeSyncApi
original_policies = get_metadata_policies()

gui = FakeGui()
action = SimpleNamespace(
    gui=gui,
    interface_action_base_plugin=SimpleNamespace(version_string="0.1.0.0"),
)

try:
    set_metadata_policies({})
    FakeMetadataApi.metadata_requests = []
    FakeMetadataApi.patch_requests = []
    FakeMetadataApi.progress_calls = []
    FakeMetadataApi.capability_requests = 0
    FakeMetadataApi.write_supported = True
    FakeMetadataApi.server_metadata = dict(server_metadata)

    preview_dialog_module.selected_calibre_books = lambda _gui: [local_book]
    preview_dialog_module.get_active_server_profile = lambda: {
        "server_url": "https://example.invalid",
        "auth_mode": "pairing",
        "session": "redacted",
    }
    preview_dialog_module.get_active_server_profile_id = lambda: "primary"
    preview_dialog_module.get_library_bindings = lambda _library, _profile: {
        "linked-book": {
            "server_document": "raw-linked",
            "logical_book_id": 42,
            "enrolled": True,
        }
    }
    preview_dialog_module.DeluxeSyncApi = FakeMetadataApi

    dialog = SyncPreviewDialog(action, auto_refresh=False)
    dialog.refresh_preview()
    wait_until(lambda: not dialog._loading, "Stage 6 preview worker")

    assert_equal(FakeMetadataApi.patch_requests, [], "preview metadata writes")
    assert_equal(
        FakeMetadataApi.metadata_requests,
        ["raw-linked"],
        "initial metadata GET",
    )
    assert_equal(dialog.sync_metadata_button.isEnabled(), True, "metadata sync enabled")

    dialog.sync_metadata(show_completion=False)
    wait_until(lambda: not dialog._loading, "Stage 6 metadata write worker")

    assert_equal(
        FakeMetadataApi.patch_requests,
        [
            (
                "raw-linked",
                {
                    "title": "Calibre Title",
                    "series": "Calibre Series",
                    "series_index": 2,
                },
            )
        ],
        "dialog metadata PATCH",
    )
    assert_equal(
        FakeMetadataApi.metadata_requests,
        ["raw-linked", "raw-linked"],
        "post-PATCH metadata verification GET",
    )
    assert_equal(FakeMetadataApi.progress_calls, [], "progress API calls")
    assert_equal(
        dialog.summary_label.text(),
        "Sync complete: 1 updated, 0 no approved changes, 0 not linked, 0 failed.",
        "approved sync summary",
    )
    assert_equal(
        dialog.sync_metadata_button.isEnabled(),
        False,
        "metadata sync disabled after verified match",
    )

    rows = [
        [
            dialog.results_model.item(row, column).text()
            for column in range(dialog.results_model.columnCount())
        ]
        for row in range(dialog.results_model.rowCount())
    ]
    title_row = next(row for row in rows if row[0] == "Title")
    series_row = next(row for row in rows if row[0] == "Series")
    assert_equal(title_row[2], "Calibre Title", "verified server title")
    assert_equal(title_row[3], "Already matches", "verified title action")
    assert_equal(series_row[2], "Calibre Series", "verified server series")
    assert_equal(series_row[3], "Already matches", "verified series action")

    # A server without the write capability remains preview-only even when a
    # Calibre -> Server difference is visible.
    FakeMetadataApi.write_supported = False
    FakeMetadataApi.server_metadata["title"] = "Server Again"
    patches_before = list(FakeMetadataApi.patch_requests)
    dialog.refresh_preview()
    wait_until(lambda: not dialog._loading, "write-disabled preview worker")
    assert_equal(
        dialog.sync_metadata_button.isEnabled(),
        False,
        "write-disabled server button state",
    )
    assert_equal(
        dialog.server_write_support_label.text(),
        "Server writes supported: None. Metadata and covers are read-only on this server.",
        "write-disabled server support summary",
    )
    assert_equal(
        "does not support metadata, cover, rating, or review writes"
        in dialog.sync_metadata_button.toolTip(),
        True,
        "write-disabled button explanation",
    )
    disabled_rows = [
        [
            dialog.results_model.item(row, column).text()
            for column in range(dialog.results_model.columnCount())
        ]
        for row in range(dialog.results_model.rowCount())
    ]
    disabled_title_row = next(row for row in disabled_rows if row[0] == "Title")
    assert_equal(
        disabled_title_row[3],
        "Not supported by this server",
        "write-disabled title action",
    )
    dialog.sync_metadata(show_completion=False)
    app.processEvents()
    assert_equal(
        FakeMetadataApi.patch_requests,
        patches_before,
        "write-disabled server PATCH calls",
    )
    assert_equal(FakeMetadataApi.progress_calls, [], "write-disabled progress calls")
    dialog.close()
finally:
    preview_dialog_module.selected_calibre_books = original_selected
    preview_dialog_module.get_active_server_profile = original_profile
    preview_dialog_module.get_active_server_profile_id = original_profile_id
    preview_dialog_module.get_library_bindings = original_bindings
    preview_dialog_module.DeluxeSyncApi = original_api
    set_metadata_policies(original_policies)
    gui.close()


print(
    "Stage 6 metadata-write smoke passed: approved-field PATCH planning, empty-value "
    "protection, enhanced write capability gating, exact PATCH payloads, mandatory "
    "post-write metadata verification, preview-only unsupported servers, and zero "
    "progress API calls."
)
