"""Deterministic Stage 5 metadata-preview smoke test inside Calibre."""

from __future__ import annotations

from types import SimpleNamespace
from time import monotonic, sleep

from calibre.customize.ui import find_plugin
from qt.core import QApplication, QPalette, Qt, QWidget


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

from calibre_plugins.deluxe_sync.api import DeluxeSyncApi  # noqa: E402
from calibre_plugins.deluxe_sync.dialogs import sync_preview as preview_dialog_module  # noqa: E402
from calibre_plugins.deluxe_sync.dialogs.sync_preview import SyncPreviewDialog  # noqa: E402
from calibre_plugins.deluxe_sync.metadata_preview import (  # noqa: E402
    ACTION_CALIBRE_TO_SERVER,
    ACTION_DO_NOT_SYNC,
    ACTION_KEEP_CALIBRE,
    ACTION_KEEP_SERVER,
    ACTION_SAME,
    ACTION_SERVER_TO_CALIBRE,
    POLICY_CALIBRE_WINS,
    POLICY_DO_NOT_SYNC,
    POLICY_SERVER_WINS,
    build_metadata_preview,
    clean_metadata_policies,
    proposed_action,
)
from calibre_plugins.deluxe_sync.models import CalibreBook  # noqa: E402
from calibre_plugins.deluxe_sync.settings import (  # noqa: E402
    get_metadata_policies,
    set_metadata_policies,
)


# ---------------------------------------------------------------------------
# Pure comparison rules, defaults, normalization, and empty-value protection.
# ---------------------------------------------------------------------------

defaults = clean_metadata_policies({})
assert_equal(defaults["cover"], POLICY_CALIBRE_WINS, "Cover default policy")
assert_equal(defaults["title"], POLICY_CALIBRE_WINS, "Title default policy")
assert_equal(defaults["authors"], POLICY_CALIBRE_WINS, "Authors default policy")
assert_equal(defaults["isbn"], POLICY_DO_NOT_SYNC, "ISBN default policy")
assert_equal(defaults["asin"], POLICY_DO_NOT_SYNC, "ASIN default policy")
assert_equal(defaults["series"], POLICY_CALIBRE_WINS, "Series default policy")
assert_equal(defaults["series_index"], POLICY_CALIBRE_WINS, "Series # default policy")

if proposed_action("title", "The Book", "the-book", POLICY_CALIBRE_WINS) != ACTION_SAME:
    raise RuntimeError("Title normalization did not recognize equivalent values")
if (
    proposed_action("isbn", "978-1-2345-6789-0", "9781234567890", POLICY_DO_NOT_SYNC)
    != ACTION_SAME
):
    raise RuntimeError("ISBN normalization did not recognize equivalent values")
if (
    proposed_action("title", "Calibre Title", "Server Title", POLICY_CALIBRE_WINS)
    != ACTION_CALIBRE_TO_SERVER
):
    raise RuntimeError("Calibre-wins difference did not preview Calibre → Server")
if (
    proposed_action("title", "Calibre Title", "Server Title", POLICY_SERVER_WINS)
    != ACTION_SERVER_TO_CALIBRE
):
    raise RuntimeError("Server-wins difference did not preview Server → Calibre")
if (
    proposed_action("asin", "B000LOCAL", "B000REMOTE", POLICY_DO_NOT_SYNC)
    != ACTION_DO_NOT_SYNC
):
    raise RuntimeError("Do-not-sync identifier policy did not suppress a proposed write")
if (
    proposed_action("series", "", "Server Series", POLICY_CALIBRE_WINS)
    != ACTION_KEEP_SERVER
):
    raise RuntimeError("Empty Calibre value would erase non-empty server metadata")
if (
    proposed_action("title", "Calibre Title", None, POLICY_SERVER_WINS)
    != ACTION_KEEP_CALIBRE
):
    raise RuntimeError("Empty server value would erase non-empty Calibre metadata")


local_book = CalibreBook(
    library_uuid="stage5-library",
    book_uuid="linked-book",
    book_id=1,
    title="Calibre Title",
    authors=("Alice Example",),
    identifiers={"isbn": "978-1-2345-6789-0", "asin": "B000LOCAL"},
    series="Calibre Series",
    series_index=2,
)
preview = build_metadata_preview(
    local_book,
    {
        "title": "Server Title",
        "authors": "Alice Example",
        "isbn": "9781234567890",
        "asin": "B000REMOTE",
        "series": "Server Series",
        "series_index": 2.5,
    },
    {},
)
actions = {item.field: item.action for item in preview}
assert_equal(actions["title"], ACTION_CALIBRE_TO_SERVER, "preview title action")
assert_equal(actions["authors"], ACTION_SAME, "preview authors action")
assert_equal(actions["isbn"], ACTION_SAME, "preview ISBN normalization")
assert_equal(actions["asin"], ACTION_DO_NOT_SYNC, "preview ASIN default protection")
assert_equal(actions["series"], ACTION_CALIBRE_TO_SERVER, "preview series action")
assert_equal(actions["series_index"], ACTION_CALIBRE_TO_SERVER, "preview series # action")


# ---------------------------------------------------------------------------
# API metadata reads: enhanced route is GET-only and standard KOSync falls back
# to /syncs/documents without introducing a mutation path.
# ---------------------------------------------------------------------------

api = DeluxeSyncApi("https://example.invalid", plugin_version="0.1.0.0")
request_calls = []


def fake_request(path, **kwargs):
    request_calls.append((path, dict(kwargs)))
    return {
        "document": "raw/doc",
        "metadata": {
            "filename": "Book.epub",
            "title": "Server Title",
            "authors": "Alice Example",
            "isbn": None,
            "asin": None,
            "series": "Server Series",
            "series_index": 3,
        },
        "updated_at": 123,
    }


api._request_json = fake_request
enhanced = api.get_document_metadata(
    "raw/doc",
    {"auth_mode": "pairing"},
    capabilities_response={"capabilities": {"document_metadata": True}},
)
assert_equal(enhanced["metadata"]["title"], "Server Title", "enhanced metadata title")
assert_equal(len(request_calls), 1, "enhanced metadata request count")
path, kwargs = request_calls[0]
assert_equal(path, "/api/v1/documents/raw%2Fdoc/metadata", "metadata GET path")
if kwargs.get("method", "GET") != "GET":
    raise RuntimeError("Stage 5 metadata reader attempted a non-GET enhanced request")
if "payload" in kwargs and kwargs["payload"] is not None:
    raise RuntimeError("Stage 5 metadata reader unexpectedly sent a payload")

api.get_raw_documents = lambda _profile: {
    "documents": [
        {
            "document": "standard-doc",
            "filename": "Standard.epub",
            "title": "Standard Title",
            "authors": "Standard Author",
            "isbn": "9780000000001",
            "asin": "",
            "series": "",
            "series_index": None,
            "timestamp": 456,
        }
    ]
}
standard = api.get_document_metadata(
    "standard-doc",
    {"auth_mode": "kosync"},
    capabilities_response={"capabilities": {}},
)
assert_equal(standard["metadata"]["title"], "Standard Title", "standard metadata fallback")


# ---------------------------------------------------------------------------
# Real Qt preview surface with a fake read-only API. It must use only selected
# linked books, persist policy changes, and never touch Calibre/server metadata.
# ---------------------------------------------------------------------------


class FakeGui(QWidget):
    def __init__(self):
        super().__init__()
        self.current_db = SimpleNamespace(library_id="stage5-library")


class FakePreviewApi:
    metadata_requests = []
    mutation_calls = []

    def __init__(self, _server_url, *, plugin_version, timeout=10):
        self.plugin_version = plugin_version
        self.timeout = timeout

    def discover_capabilities(self, _profile):
        return {
            "server_type": "enhanced",
            "capabilities": {
                "document_metadata": True,
                "document_metadata_write": True,
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
            "metadata": {
                "filename": "Linked.epub",
                "title": "Server Title",
                "authors": "Alice Example",
                "isbn": "9781234567890",
                "asin": "B000REMOTE",
                "series": "Server Series",
                "series_index": 2.5,
            },
            "updated_at": 999,
        }

    def patch_document_metadata(self, *_args, **_kwargs):
        type(self).mutation_calls.append("patch")
        raise RuntimeError("Stage 5 attempted a server metadata write")


unlinked_book = CalibreBook(
    library_uuid="stage5-library",
    book_uuid="unlinked-book",
    book_id=2,
    title="Unlinked Book",
    authors=("Nobody",),
)

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
    preview_dialog_module.selected_calibre_books = lambda _gui: [
        local_book,
        unlinked_book,
    ]
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
    preview_dialog_module.DeluxeSyncApi = FakePreviewApi

    dialog = SyncPreviewDialog(action, auto_refresh=False)

    expected_policy_positions = {
        "cover": (0, 1),
        "title": (1, 1),
        "authors": (2, 1),
        "isbn": (0, 3),
        "asin": (1, 3),
        "series": (2, 3),
        "series_index": (0, 5),
        "rating": (1, 5),
    }
    for field, expected_position in expected_policy_positions.items():
        combo = dialog._policy_boxes[field]
        layout_index = dialog.policy_grid.indexOf(combo)
        row, column, _row_span, _column_span = dialog.policy_grid.getItemPosition(
            layout_index
        )
        assert_equal((row, column), expected_position, f"{field} policy grid position")

    policy_label_widths = {
        dialog.policy_grid.itemAtPosition(row, column).widget().minimumWidth()
        for row, column in (
            (0, 0), (1, 0), (2, 0),
            (0, 2), (1, 2), (2, 2),
            (0, 4), (1, 4),
        )
    }
    assert_equal(len(policy_label_widths), 1, "aligned policy label widths")

    dialog.refresh_preview()
    wait_until(lambda: not dialog._loading, "Stage 5 preview worker")

    assert_equal(
        FakePreviewApi.metadata_requests,
        ["raw-linked"],
        "selected linked metadata requests",
    )
    assert_equal(FakePreviewApi.mutation_calls, [], "server mutation calls")
    assert_equal(dialog.results_model.columnCount(), 4, "grouped preview columns")
    assert_equal(dialog.results_model.rowCount(), 12, "grouped preview result rows")

    rows = [
        [
            dialog.results_model.item(row, column).text()
            for column in range(dialog.results_model.columnCount())
        ]
        for row in range(dialog.results_model.rowCount())
    ]
    unlinked_header_index = next(
        index for index, row in enumerate(rows) if row[0] == "Unlinked Book"
    )
    linked_header_index = next(
        index for index, row in enumerate(rows) if row[0] == "Calibre Title"
    )
    title_row = next(row for row in rows if row[0] == "Title")
    asin_row = next(row for row in rows if row[0] == "ASIN")
    unlinked_status_row = next(row for row in rows if row[0] == "Status")

    assert_equal(
        dialog.results_table.columnSpan(unlinked_header_index, 0),
        4,
        "unlinked book header span",
    )
    assert_equal(
        dialog.results_table.columnSpan(linked_header_index, 0),
        4,
        "linked book header span",
    )
    assert_equal(
        dialog.results_table.alternatingRowColors(),
        False,
        "field-level alternating row colors disabled",
    )

    palette = dialog.results_table.palette()
    base_rgba = palette.color(QPalette.ColorRole.Base).rgba()
    alternate_rgba = palette.color(QPalette.ColorRole.AlternateBase).rgba()
    for row_index in (unlinked_header_index, unlinked_header_index + 1):
        assert_equal(
            dialog.results_model.item(row_index, 0).background().color().rgba(),
            base_rgba,
            "unlinked book block background",
        )
    for row_index in range(linked_header_index, dialog.results_model.rowCount()):
        assert_equal(
            dialog.results_model.item(row_index, 0).background().color().rgba(),
            alternate_rgba,
            "linked book block background",
        )

    linked_header_item = dialog.results_model.item(linked_header_index, 0)
    assert_equal(
        linked_header_item.textAlignment(),
        Qt.AlignmentFlag.AlignCenter,
        "book title centered",
    )
    assert_equal(linked_header_item.font().bold(), True, "book title bold")
    assert_equal(title_row[1], "Calibre Title", "Calibre title display")
    assert_equal(title_row[2], "Server Title", "server title display")
    assert_equal(title_row[3], "Calibre → Server", "default title direction")
    assert_equal(asin_row[3], "No change (Do not sync)", "default ASIN policy")
    assert_equal(unlinked_status_row[3], "Not linked", "unlinked selection result")

    title_combo = dialog._policy_boxes["title"]
    server_wins_index = title_combo.findData(POLICY_SERVER_WINS)
    if server_wins_index < 0:
        raise RuntimeError("Server-wins policy option is missing")
    title_combo.setCurrentIndex(server_wins_index)
    app.processEvents()

    saved_policies = clean_metadata_policies(get_metadata_policies())
    assert_equal(
        saved_policies["title"],
        POLICY_SERVER_WINS,
        "persisted title policy",
    )
    rows_after_policy = [
        [
            dialog.results_model.item(row, column).text()
            for column in range(dialog.results_model.columnCount())
        ]
        for row in range(dialog.results_model.rowCount())
    ]
    title_row = next(row for row in rows_after_policy if row[0] == "Title")
    assert_equal(
        title_row[3],
        "Preview only: Server → Calibre",
        "updated title direction",
    )
    assert_equal(FakePreviewApi.metadata_requests, ["raw-linked"], "policy change network reads")

    dialog.refresh_preview()
    wait_until(lambda: not dialog._loading, "Stage 5 preview refresh")
    assert_equal(
        FakePreviewApi.metadata_requests,
        ["raw-linked", "raw-linked"],
        "refresh metadata GET count",
    )
    assert_equal(FakePreviewApi.mutation_calls, [], "refresh server mutation calls")
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
    "Stage 5 metadata preview smoke passed: comparison normalization, policy defaults, "
    "empty-value protection, GET-only metadata reads, selected linked-book scope, "
    "policy persistence, refresh behavior, and zero metadata mutations."
)
