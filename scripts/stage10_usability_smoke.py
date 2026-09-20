"""Deterministic Stage 10 dashboard and safe-batch-sync smoke test."""

from __future__ import annotations

from types import SimpleNamespace

from calibre.customize.ui import find_plugin
from qt.core import QApplication, QLabel, QPushButton, QWidget


app = QApplication.instance() or QApplication([])

plugin = find_plugin("Deluxe Sync")
if plugin is None:
    raise RuntimeError("Deluxe Sync is not installed")
plugin.load_actual_plugin(None)

from calibre_plugins.deluxe_sync.api import ApiError, AuthorizationError, DeluxeSyncApi  # noqa: E402
from calibre_plugins.deluxe_sync.dialogs import dashboard as dashboard_module  # noqa: E402
from calibre_plugins.deluxe_sync.dialogs import sync_books as sync_books_module  # noqa: E402
from calibre_plugins.deluxe_sync.dialogs import sync_preview as sync_preview_module  # noqa: E402
from calibre_plugins.deluxe_sync.dialogs.about import AboutDialog  # noqa: E402
from calibre_plugins.deluxe_sync.dialogs.dashboard import DashboardDialog, _ServerStatus  # noqa: E402
from calibre_plugins.deluxe_sync.dialogs.match_books import MatchBooksDialog  # noqa: E402
from calibre_plugins.deluxe_sync.dialogs.sync_books import SyncSelectedBooksDialog  # noqa: E402
from calibre_plugins.deluxe_sync.dialogs.sync_preview import SyncPreviewDialog  # noqa: E402
from calibre_plugins.deluxe_sync.models import (  # noqa: E402
    library_book_count,
    linked_calibre_books,
)


def assert_equal(actual, expected, label):
    if actual != expected:
        raise RuntimeError(f"{label}: expected {expected!r}, got {actual!r}")


def assert_contains(text, fragment, label):
    if fragment not in text:
        raise RuntimeError(f"{label}: expected {fragment!r} in {text!r}")


class ProfileProbeApi(DeluxeSyncApi):
    responses = {}
    calls = []

    def _request_json(self, path, **kwargs):
        self.__class__.calls.append((self.server_url, path))
        response = self.__class__.responses.get((self.server_url, path))
        if isinstance(response, Exception):
            raise response
        if response is None:
            raise ApiError("Not found", status=404)
        return response


PROFILE = {"auth_mode": "kosync", "username": "smoke", "userkey": "redacted"}
STANDARD_CAPABILITIES = {
    "server_family": "kosync",
    "server_type": "standard",
    "server_version": "unknown",
    "capabilities": {"progress_sync": True},
}
ENHANCED_CAPABILITIES = {
    "server_family": "techy-notes",
    "server_type": "enhanced",
    "server_version": "0.2.0.3",
    "capabilities": {"progress_sync": True},
}
CROSSPOINT_CAPABILITIES = {
    "server_family": "crosspoint",
    "server_type": "crosspoint",
    "server_version": "unknown",
    "capabilities": {"progress_sync": True},
}

assert_equal(
    ProfileProbeApi(
        "https://sync.example",
        plugin_version="stage10-smoke",
    ).detect_server_profile(PROFILE, ENHANCED_CAPABILITIES),
    "enhanced",
    "declared enhanced profile",
)

assert_equal(
    ProfileProbeApi(
        "https://crosspoint-declared.example",
        plugin_version="stage10-smoke",
    ).detect_server_profile(PROFILE, CROSSPOINT_CAPABILITIES),
    "crosspoint",
    "declared Crosspoint profile",
)

ProfileProbeApi.responses = {
    (
        "https://bookorbit.example",
        "/plugin/version",
    ): {
        "serverVersion": "1.0.0",
        "capabilities": ["catalogBulkManifest"],
    }
}
ProfileProbeApi.calls = []
bookorbit_probe = ProfileProbeApi(
    "https://bookorbit.example/api/v1/koreader",
    plugin_version="stage10-smoke",
)
assert_equal(
    bookorbit_probe.detect_server_profile(PROFILE, STANDARD_CAPABILITIES),
    "bookorbit",
    "BookOrbit compatibility profile",
)
assert_equal(
    ProfileProbeApi.calls,
    [("https://bookorbit.example", "/plugin/version")],
    "BookOrbit service-root probe",
)

ProfileProbeApi.responses = {
    (
        "https://crosspoint.example",
        "/api/v1/progress?limit=1",
    ): {
        "items": [],
    },
    (
        "https://crosspoint.example",
        "/api/v1/progress?limit=500",
    ): {
        "items": [
            {
                "document": "crosspoint-one",
                "title": "Crosspoint One",
                "author": "Author One",
                "filename": "Crosspoint One.epub",
                "percentage": 0.25,
                "progress": "/body/one",
                "timestamp": 101,
            },
            {
                "document": "crosspoint-two",
                "title": "Crosspoint Two",
                "author": "Author Two",
                "filename": "Crosspoint Two.epub",
                "percentage": 0.75,
                "progress": "/body/two",
                "timestamp": 202,
            },
        ],
    },
}
ProfileProbeApi.calls = []
crosspoint_probe = ProfileProbeApi(
    "https://crosspoint.example/api/v1/koreader",
    plugin_version="stage10-smoke",
)
assert_equal(
    crosspoint_probe.detect_server_profile(PROFILE, STANDARD_CAPABILITIES),
    "crosspoint",
    "Crosspoint compatibility profile",
)
assert_equal(
    ProfileProbeApi.calls,
    [
        ("https://crosspoint.example", "/plugin/version"),
        ("https://crosspoint.example", "/api/v1/progress?limit=1"),
    ],
    "Crosspoint service-root probes",
)

ProfileProbeApi.calls = []
crosspoint_library = crosspoint_probe.get_library(PROFILE, STANDARD_CAPABILITIES)
assert_equal(crosspoint_library["visible_count"], 2, "Crosspoint library visible count")
assert_equal(crosspoint_library["raw_count"], 2, "Crosspoint library raw count")
assert_equal(
    crosspoint_library["books"][0]["authors"],
    "Author One",
    "Crosspoint singular author normalization",
)
assert_equal(
    ProfileProbeApi.calls,
    [
        ("https://crosspoint.example/api/v1/koreader", "/syncs/documents"),
        ("https://crosspoint.example", "/api/v1/progress?limit=500"),
    ],
    "Crosspoint library fallback probes",
)

ProfileProbeApi.calls = []
crosspoint_metadata = crosspoint_probe.get_document_metadata(
    "crosspoint-one",
    PROFILE,
    capabilities_response=STANDARD_CAPABILITIES,
)
assert_equal(
    crosspoint_metadata["metadata"]["title"],
    "Crosspoint One",
    "Crosspoint Sync Preview metadata title",
)
assert_equal(
    crosspoint_metadata["metadata"]["authors"],
    "Author One",
    "Crosspoint Sync Preview metadata author",
)
assert_equal(
    ProfileProbeApi.calls,
    [
        ("https://crosspoint.example/api/v1/koreader", "/syncs/documents"),
        ("https://crosspoint.example", "/api/v1/progress?limit=500"),
    ],
    "Crosspoint Sync Preview fallback probes",
)

ProfileProbeApi.responses = {}
ProfileProbeApi.calls = []
assert_equal(
    ProfileProbeApi(
        "https://plain.example",
        plugin_version="stage10-smoke",
    ).detect_server_profile(PROFILE, STANDARD_CAPABILITIES),
    "standard",
    "plain KOSync profile",
)


class FakeApi:
    def __init__(self):
        self.records = {
            1: {
                "uuid": "book-one",
                "title": "Book One",
                "authors": ("Author One",),
                "identifiers": {"isbn": "111"},
                "series": "Series A",
                "series_index": 1.0,
            },
            2: {
                "uuid": "book-two",
                "title": "Book Two",
                "authors": ("Author Two",),
                "identifiers": {"isbn": "222"},
                "series": "Series A",
                "series_index": 2.0,
            },
            3: {
                "uuid": "book-three",
                "title": "Book Three",
                "authors": ("Author Three",),
                "identifiers": {},
                "series": "",
                "series_index": None,
            },
        }
        self.all_book_ids_calls = 0
        self.cover_calls = 0

    def all_book_ids(self):
        self.all_book_ids_calls += 1
        return tuple(self.records)

    def field_for(self, name, book_id, default_value=None):
        return self.records.get(int(book_id), {}).get(name, default_value)

    def formats(self, book_id, verify_formats=False):
        return ()

    def format_abspath(self, book_id, fmt):
        return None

    def cover(self, book_id):
        self.cover_calls += 1
        return b"cover-bytes"


class FakeDb:
    library_id = "stage10-library"
    library_path = r"C:\Calibre Libraries\Stage 10"

    def __init__(self):
        self.new_api = FakeApi()


class FakeLibraryView:
    def __init__(self, selected=(1,)):
        self.selected = tuple(selected)

    def get_selected_ids(self, as_set=False):
        return set(self.selected) if as_set else list(self.selected)


class FakeGui(QWidget):
    def __init__(self, selected=(1,)):
        super().__init__()
        self.current_db = FakeDb()
        self.library_view = FakeLibraryView(selected)
        self.must_restart_before_config = False


# ---------------------------------------------------------------------------
# Durable UUID batch resolution: cached IDs first, full scan only as fallback.
# ---------------------------------------------------------------------------

gui = FakeGui(selected=(1, 2))
bindings = {
    "book-one": {
        "calibre_book_id": 1,
        "server_document": "doc-one",
        "enrolled": True,
    },
    "book-two": {
        "calibre_book_id": 999,
        "server_document": "doc-two",
        "enrolled": True,
    },
    "book-three": {
        "calibre_book_id": 3,
        "server_document": "doc-three",
        "enrolled": False,
    },
    "book-missing": {
        "calibre_book_id": None,
        "server_document": "doc-missing",
        "enrolled": True,
    },
    "book-no-document": {
        "calibre_book_id": 3,
        "server_document": "",
        "enrolled": True,
    },
}

resolved = linked_calibre_books(gui, bindings)
assert_equal([book.book_uuid for book in resolved], ["book-one", "book-two"], "linked UUID resolution")
assert_equal(gui.current_db.new_api.all_book_ids_calls, 1, "stale/missing binding fallback scan count")
assert_equal(gui.current_db.new_api.cover_calls, 0, "batch resolver cover reads")

fast_gui = FakeGui()
fast_resolved = linked_calibre_books(
    fast_gui,
    {
        "book-one": {
            "calibre_book_id": 1,
            "server_document": "doc-one",
            "enrolled": True,
        }
    },
)
assert_equal([book.book_uuid for book in fast_resolved], ["book-one"], "cached linked resolution")
assert_equal(fast_gui.current_db.new_api.all_book_ids_calls, 0, "valid cached ID avoids library scan")
assert_equal(library_book_count(fast_gui), 3, "library book count")


# ---------------------------------------------------------------------------
# Dashboard: local counts/scopes plus safe server/auth/conflict status rendering.
# ---------------------------------------------------------------------------

original_dashboard_profile = dashboard_module.get_active_server_profile
original_dashboard_profile_id = dashboard_module.get_active_server_profile_id
original_dashboard_bindings = dashboard_module.get_library_bindings

action = SimpleNamespace(
    gui=gui,
    show_match_books_dialog=lambda: None,
    show_library_dialog=lambda: None,
    show_config=lambda: None,
    _center_dialog=lambda dialog: None,
    interface_action_base_plugin=SimpleNamespace(author="Jadehawk", version_string="stage10-smoke"),
)

try:
    dashboard_module.get_active_server_profile = lambda: {
        "server_url": "https://sync.example",
        "auth_mode": "pairing",
    }
    dashboard_module.get_active_server_profile_id = lambda: "stage10-profile"
    dashboard_module.get_library_bindings = lambda library_uuid, profile_id: dict(bindings)

    dashboard = DashboardDialog(action, auto_refresh=False)
    assert_equal(dashboard.total_count_label.text(), "3", "dashboard total count")
    assert_equal(dashboard.linked_count_label.text(), "2", "dashboard linked count")
    assert_equal(dashboard.unlinked_count_label.text(), "1", "dashboard unlinked count")
    assert_contains(dashboard.library_label.text(), "stage10-library", "dashboard library identity")
    assert_equal(dashboard.server_url_label.text(), "https://sync.example", "dashboard server URL")
    assert_equal(dashboard.match_button.text(), "Match Server Books", "unchecked matching button label")
    assert_contains(
        dashboard.match_button.toolTip(),
        "Refresh server status",
        "unchecked matching button tooltip",
    )
    assert_contains(
        dashboard.sync_button.toolTip(),
        "does not write title, author, series, identifier, or cover metadata",
        "Sync Now reading-data tooltip",
    )
    assert_contains(dashboard.scope_summary_label.text(), "2 currently selected", "selected scope summary")

    dashboard.scope_combo.setCurrentIndex(1)
    app.processEvents()
    assert_contains(dashboard.scope_summary_label.text(), "2 linked books", "linked scope summary")
    if not dashboard.sync_button.isEnabled():
        raise RuntimeError("Stage 10 linked-book Sync Now stayed disabled with linked books available")

    dashboard._generation = 1
    dashboard._loading = True
    dashboard._server_status_loaded(
        (1, _ServerStatus(error=AuthorizationError("rejected", status=401)))
    )
    assert_equal(dashboard.server_status_label.text(), "Authentication rejected", "invalid credentials status")
    assert_equal(dashboard.server_profile_label.text(), "Not available", "invalid credentials profile")
    assert_equal(dashboard.server_book_count_label.text(), "Not available", "invalid credentials book count")

    dashboard._generation = 2
    dashboard._loading = True
    dashboard._server_status_loaded(
        (
            2,
            _ServerStatus(
                capabilities=STANDARD_CAPABILITIES,
                server_profile="standard",
                server_book_count=None,
                browsing_supported=False,
                registration_supported=False,
                remote_bindings_supported=False,
            ),
        )
    )
    assert_equal(dashboard.server_status_label.text(), "Connected", "plain KOSync status")
    assert_equal(dashboard.server_profile_label.text(), "Standard KOSync", "plain KOSync profile")
    assert_equal(dashboard.server_book_count_label.text(), "Not available", "plain KOSync book count")
    assert_equal(
        dashboard.conflict_count_label.text(),
        "Not available on this server",
        "plain KOSync conflict capability",
    )
    assert_equal(
        dashboard.match_button.text(),
        "Server Matching Unavailable",
        "non-browseable KOSync matching label",
    )
    assert_equal(
        dashboard.match_button.isEnabled(),
        False,
        "non-browseable KOSync matching disabled",
    )
    assert_contains(
        dashboard.match_button.toolTip(),
        "does not expose a browseable book list",
        "non-browseable KOSync matching guidance",
    )

    dashboard._generation = 3
    dashboard._loading = True
    dashboard._server_status_loaded(
        (
            3,
            _ServerStatus(
                capabilities=ENHANCED_CAPABILITIES,
                server_profile="enhanced",
                server_book_count=69,
                browsing_supported=True,
                registration_supported=True,
                conflict_count=2,
                remote_bindings_supported=True,
            ),
        )
    )
    assert_equal(dashboard.server_status_label.text(), "Connected", "enhanced status")
    assert_equal(dashboard.server_profile_label.text(), "Enhanced", "enhanced profile")
    assert_equal(dashboard.server_book_count_label.text(), "69", "enhanced server book count")
    assert_contains(dashboard.conflict_count_label.text(), "2", "enhanced conflict count")
    assert_contains(
        dashboard.conflict_count_label.text(),
        "local links remain authoritative",
        "safe binding-conflict guidance",
    )
    assert_equal(
        dashboard.match_button.text(),
        "Match / Create on Server",
        "enhanced safe-create matching label",
    )
    assert_contains(
        dashboard.match_button.toolTip(),
        "safely create missing books",
        "enhanced safe-create matching guidance",
    )

    dashboard._generation = 4
    dashboard._loading = True
    dashboard._server_status_loaded(
        (
            4,
            _ServerStatus(
                capabilities=STANDARD_CAPABILITIES,
                server_profile="crosspoint",
                server_book_count=2,
                browsing_supported=True,
                registration_supported=False,
                remote_bindings_supported=False,
            ),
        )
    )
    assert_equal(dashboard.server_status_label.text(), "Connected", "Crosspoint status")
    assert_equal(dashboard.server_profile_label.text(), "Crosspoint", "Crosspoint profile")
    assert_equal(dashboard.server_book_count_label.text(), "2", "Crosspoint book count")
    assert_equal(
        dashboard.match_button.text(),
        "Match to Server Book",
        "browse-only matching label",
    )
    assert_contains(
        dashboard.match_button.toolTip(),
        "sync it from your e-reader first",
        "browse-only reader-first guidance",
    )
    dashboard._refresh_local_state()
    assert_equal(
        dashboard.server_status_label.text(),
        "Connected",
        "server status preserved after local refresh",
    )
    assert_equal(
        dashboard.server_profile_label.text(),
        "Crosspoint",
        "server profile preserved after local refresh",
    )
    assert_equal(
        dashboard.server_book_count_label.text(),
        "2",
        "server book count preserved after local refresh",
    )

    dashboard._generation = 5
    dashboard._loading = True
    dashboard._server_status_loaded(
        (
            5,
            _ServerStatus(
                capabilities=STANDARD_CAPABILITIES,
                server_profile="standard",
                server_book_count=3,
                browsing_supported=True,
                registration_supported=False,
                remote_bindings_supported=False,
            ),
        )
    )
    assert_equal(
        dashboard.match_button.text(),
        "Match to Server Book",
        "browseable extended KOSync matching label",
    )
    assert_contains(
        dashboard.match_button.toolTip(),
        "sync it from your e-reader first",
        "browseable extended KOSync reader-first guidance",
    )
    dashboard.close()
finally:
    dashboard_module.get_active_server_profile = original_dashboard_profile
    dashboard_module.get_active_server_profile_id = original_dashboard_profile_id
    dashboard_module.get_library_bindings = original_dashboard_bindings


# ---------------------------------------------------------------------------
# Dashboard child windows return to their parent, and About is self-contained.
# ---------------------------------------------------------------------------

preview_child = SyncPreviewDialog(
    action,
    auto_refresh=False,
    books=[resolved[0]],
    parent_dialog=dashboard,
)
assert_equal(preview_child.parent(), dashboard, "Sync Preview dashboard parent")
if "Back to Dashboard" not in {
    button.text() for button in preview_child.findChildren(QPushButton)
}:
    raise RuntimeError("Sync Preview did not expose Back to Dashboard")
preview_child.close()

sync_child = SyncSelectedBooksDialog(
    action,
    auto_sync=False,
    books=[resolved[0]],
    parent_dialog=dashboard,
)
assert_equal(sync_child.parent(), dashboard, "Sync Now dashboard parent")
if "Back to Dashboard" not in {
    button.text() for button in sync_child.findChildren(QPushButton)
}:
    raise RuntimeError("Sync Now did not expose Back to Dashboard")
sync_child.close()

match_child = MatchBooksDialog(
    action,
    parent_dialog=dashboard,
    auto_refresh=False,
)
assert_equal(match_child.parent(), dashboard, "Match Books dashboard parent")
if "Back to Dashboard" not in {
    button.text() for button in match_child.findChildren(QPushButton)
}:
    raise RuntimeError("Match Books did not expose Back to Dashboard")
match_child.close()

about_dialog = AboutDialog(action)
about_text = " ".join(
    label.text() for label in about_dialog.findChildren(QLabel)
)
assert_contains(about_text, "Jadehawk", "About author")
assert_contains(about_text, "stage10-smoke", "About plugin version")
assert_contains(
    about_text,
    "Calibre companion for sync.techy-notes.com",
    "About companion description",
)
assert_contains(
    about_text,
    "Deluxe-Sync (KOReader Plugin)",
    "About KOReader companion reference",
)
recommended_server = about_dialog.findChild(QLabel, "recommendedServerLink")
if recommended_server is None:
    raise RuntimeError("About dialog did not expose the recommended server link")
assert_contains(
    recommended_server.text(),
    'href="https://sync.techy-notes.com"',
    "About recommended server URL",
)
if not recommended_server.openExternalLinks():
    raise RuntimeError("About recommended server link does not open externally")
about_dialog.close()


# ---------------------------------------------------------------------------
# Batch sync respects server capabilities and stale/missing server bindings.
# ---------------------------------------------------------------------------


class BatchCapabilityApi:
    progress_calls = []
    annotation_calls = []
    vocabulary_calls = []

    def __init__(self, server_url, *, plugin_version, timeout=10):
        self.server_url = server_url

    def discover_capabilities(self, profile=None):
        return {
            "server_family": "kosync",
            "server_type": "standard",
            "server_version": "smoke",
            "capabilities": {
                "progress_sync": True,
                "annotations": False,
                "vocabulary_builder": False,
                "change_journal": False,
                "logical_library": False,
            },
        }

    def detect_server_profile(self, profile, capabilities_response=None):
        return "standard"

    def get_browseable_documents(self, profile):
        return {
            "documents": [
                {
                    "document": "doc-one",
                    "title": "Book One",
                    "authors": "Author One",
                }
            ]
        }

    def get_effective_progress(self, document, profile, **kwargs):
        type(self).progress_calls.append(document)
        return {
            "document": document,
            "progress": "/body/DocFragment[2]",
            "percentage": 0.25,
            "timestamp": 1_700_000_000,
        }

    def get_annotations_for_binding(self, *args, **kwargs):
        type(self).annotation_calls.append(args[0] if args else "")
        raise RuntimeError("annotations must not be called when unsupported")

    def get_vocabulary_for_binding(self, *args, **kwargs):
        type(self).vocabulary_calls.append(args[0] if args else "")
        raise RuntimeError("vocabulary must not be called when unsupported")


original_batch_api = sync_books_module.DeluxeSyncApi
captured_batch_payloads = []
try:
    sync_books_module.DeluxeSyncApi = BatchCapabilityApi
    BatchCapabilityApi.progress_calls = []
    BatchCapabilityApi.annotation_calls = []
    BatchCapabilityApi.vocabulary_calls = []

    worker = SimpleNamespace(
        _plugin_version=lambda: "stage10-smoke",
        _bridge=SimpleNamespace(
            completed=SimpleNamespace(emit=captured_batch_payloads.append)
        ),
    )
    SyncSelectedBooksDialog._load_worker(
        worker,
        77,
        [
            (resolved[0], {"server_document": "doc-one", "enrolled": True}),
            (resolved[1], {"server_document": "doc-missing", "enrolled": True}),
        ],
        [],
        "https://sync.example",
        PROFILE,
        {
            "progress": "#ds_progress",
            "annotations": "#ds_annotations",
            "vocabulary": "#ds_vocabulary",
        },
        False,
    )
    assert_equal(BatchCapabilityApi.progress_calls, ["doc-one"], "batch progress calls")
    assert_equal(BatchCapabilityApi.annotation_calls, [], "unsupported annotation calls")
    assert_equal(BatchCapabilityApi.vocabulary_calls, [], "unsupported vocabulary calls")
    if len(captured_batch_payloads) != 1:
        raise RuntimeError("Batch worker did not emit exactly one result payload")
    batch_payload = captured_batch_payloads[0]
    assert_equal(len(batch_payload), 8, "capability-aware batch payload size")
    _generation, batch_results, _skipped, _mappings, active, unsupported, error, _show = batch_payload
    assert_equal(error, None, "capability-aware batch fatal error")
    assert_equal(set(active), {"progress"}, "active batch components")
    assert_equal(
        set(unsupported),
        {"annotations", "vocabulary"},
        "unsupported batch components",
    )
    assert_equal(len(batch_results), 2, "batch result count")
    assert_equal(
        bool(getattr(batch_results[0], "not_on_server", False)),
        False,
        "present server book result",
    )
    assert_equal(
        bool(getattr(batch_results[1], "not_on_server", False)),
        True,
        "missing server book result",
    )
finally:
    sync_books_module.DeluxeSyncApi = original_batch_api


# UI rendering reports missing books as skips and unsupported components once, not per row.
original_mapping_readiness = SyncSelectedBooksDialog._mapping_readiness
try:
    SyncSelectedBooksDialog._mapping_readiness = lambda self: (True, {}, "")
    capability_dialog = SyncSelectedBooksDialog(
        action,
        auto_sync=False,
        books=[resolved[0], resolved[1]],
        scope_label="all linked books in this library",
    )
    capability_dialog._library_uuid = gui.current_db.library_id
    capability_dialog._sync_loaded(
        (
            capability_dialog._generation,
            [
                SimpleNamespace(
                    book=resolved[0],
                    not_on_server=False,
                    refresh_components=frozenset(),
                    change_cursor=None,
                ),
                SimpleNamespace(
                    book=resolved[1],
                    not_on_server=True,
                    refresh_components=frozenset(),
                    change_cursor=None,
                ),
            ],
            [],
            {
                "annotations": "#ds_annotations",
                "vocabulary": "#ds_vocabulary",
            },
            frozenset(),
            frozenset({"annotations", "vocabulary"}),
            None,
            False,
        )
    )
    assert_equal(
        capability_dialog.results_model.item(0, 1).text(),
        "Skipped — no mapped data is supported by this server",
        "unsupported-only row result",
    )
    assert_equal(
        capability_dialog.results_model.item(1, 1).text(),
        "Not on server — skipped",
        "missing server row result",
    )
    assert_contains(
        capability_dialog.result_summary_label.text(),
        "1 not on server",
        "missing server summary",
    )
    assert_contains(
        capability_dialog.result_summary_label.text(),
        "Unsupported server data skipped: Annotations, Vocabulary.",
        "one-time unsupported component summary",
    )
    if "Warning:" in capability_dialog.results_model.item(0, 1).text():
        raise RuntimeError("Unsupported server components leaked into a per-book warning")
    capability_dialog.close()
finally:
    SyncSelectedBooksDialog._mapping_readiness = original_mapping_readiness


# ---------------------------------------------------------------------------
# Reused sync/review dialogs accept an explicit batch instead of re-reading selection.
# ---------------------------------------------------------------------------

explicit_book = resolved[0]
original_mapping_readiness = SyncSelectedBooksDialog._mapping_readiness
original_sync_selected = sync_books_module.selected_calibre_books
original_sync_profile = sync_books_module.get_active_server_profile
original_preview_selected = sync_preview_module.selected_calibre_books
original_preview_profile = sync_preview_module.get_active_server_profile

try:
    SyncSelectedBooksDialog._mapping_readiness = lambda self: (True, {}, "")
    sync_books_module.selected_calibre_books = lambda gui: (_ for _ in ()).throw(
        RuntimeError("selection helper should not run for explicit batch")
    )
    sync_books_module.get_active_server_profile = lambda: None

    sync_dialog = SyncSelectedBooksDialog(
        action,
        auto_sync=False,
        books=[explicit_book],
        scope_label="all linked books in this library",
    )
    sync_dialog.sync_selected_books()
    assert_contains(sync_dialog.status_label.text(), "Server authentication required", "explicit sync batch")
    sync_dialog.close()

    sync_preview_module.selected_calibre_books = lambda gui: (_ for _ in ()).throw(
        RuntimeError("selection helper should not run for explicit preview batch")
    )
    sync_preview_module.get_active_server_profile = lambda: None

    preview_dialog = SyncPreviewDialog(
        action,
        auto_refresh=False,
        books=[explicit_book],
    )
    preview_dialog.refresh_preview()
    assert_contains(
        preview_dialog.status_label.text(),
        "Server authentication required",
        "explicit preview batch",
    )
    preview_dialog.close()
finally:
    SyncSelectedBooksDialog._mapping_readiness = original_mapping_readiness
    sync_books_module.selected_calibre_books = original_sync_selected
    sync_books_module.get_active_server_profile = original_sync_profile
    sync_preview_module.selected_calibre_books = original_preview_selected
    sync_preview_module.get_active_server_profile = original_preview_profile
    gui.close()


# ---------------------------------------------------------------------------
# Capability-aware Sync Now: unsupported archives are never called, and a
# browseable server index can skip stale/missing server books before progress.
# ---------------------------------------------------------------------------

class CapabilityFilterApi:
    browse_documents = [{"document": "doc-one"}]
    progress_calls = 0
    annotation_calls = 0
    vocabulary_calls = 0

    def __init__(self, *args, **kwargs):
        pass

    def discover_capabilities(self, profile):
        return {
            "capabilities": {
                "progress_sync": True,
                "logical_library": False,
                "change_journal": False,
                "annotations": False,
                "vocabulary_builder": False,
            }
        }

    def detect_server_profile(self, profile, capabilities_response=None):
        return "standard"

    def get_browseable_documents(self, profile):
        return {"documents": list(type(self).browse_documents)}

    def get_effective_progress(self, document, profile, **kwargs):
        type(self).progress_calls += 1
        return {
            "document": document,
            "progress": "epubcfi(/6/2)",
            "percentage": 0.5,
            "timestamp": 1_700_000_000,
        }

    def get_annotations_for_binding(self, *args, **kwargs):
        type(self).annotation_calls += 1
        raise RuntimeError("annotation API must not be called")

    def get_vocabulary_for_binding(self, *args, **kwargs):
        type(self).vocabulary_calls += 1
        raise RuntimeError("vocabulary API must not be called")


worker_gui = FakeGui()
worker_action = SimpleNamespace(
    gui=worker_gui,
    _center_dialog=lambda dialog: None,
    interface_action_base_plugin=SimpleNamespace(author="Jadehawk", version_string="stage10-smoke"),
)
worker_dialog = SyncSelectedBooksDialog(
    worker_action,
    auto_sync=False,
    books=[explicit_book],
    scope_label="all linked books in this library",
)
captured = []
worker_dialog._bridge = SimpleNamespace(
    completed=SimpleNamespace(emit=captured.append)
)
original_worker_api = sync_books_module.DeluxeSyncApi

try:
    sync_books_module.DeluxeSyncApi = CapabilityFilterApi
    binding = {"server_document": "doc-one", "enrolled": True}
    mappings = {
        "progress": "#ds_progress",
        "annotations": "#ds_annotations",
        "vocabulary": "#ds_vocabulary",
    }

    worker_dialog._load_worker(
        7,
        [(explicit_book, binding)],
        [],
        "https://sync.example",
        {},
        mappings,
        False,
    )
    filtered_payload = captured.pop()
    assert_equal(
        filtered_payload[4],
        frozenset({"progress"}),
        "unsupported archive components removed before sync",
    )
    assert_equal(
        filtered_payload[5],
        frozenset({"annotations", "vocabulary"}),
        "unsupported archive components reported once",
    )
    assert_equal(CapabilityFilterApi.progress_calls, 1, "supported progress fetch count")
    assert_equal(CapabilityFilterApi.annotation_calls, 0, "unsupported annotation API calls")
    assert_equal(CapabilityFilterApi.vocabulary_calls, 0, "unsupported vocabulary API calls")

    CapabilityFilterApi.browse_documents = []
    CapabilityFilterApi.progress_calls = 0
    worker_dialog._load_worker(
        8,
        [(explicit_book, binding)],
        [],
        "https://sync.example",
        {},
        mappings,
        False,
    )
    missing_payload = captured.pop()
    assert_equal(CapabilityFilterApi.progress_calls, 0, "missing server book progress fetch count")
    assert_equal(
        bool(getattr(missing_payload[1][0], "not_on_server", False)),
        True,
        "missing server book classified before progress",
    )

    worker_dialog._library_uuid = worker_gui.current_db.library_id
    worker_dialog._refresh_sync_availability = lambda update_status=False: None

    worker_dialog._generation = 8
    worker_dialog._sync_loaded(missing_payload)
    assert_equal(
        worker_dialog.results_model.item(0, 1).text(),
        "Not on server — skipped",
        "missing server book row",
    )
    assert_contains(
        worker_dialog.result_summary_label.text(),
        "1 not on server, 0 failed",
        "missing server book summary",
    )

    worker_dialog._generation = 9
    worker_dialog._sync_loaded(
        (
            9,
            [SimpleNamespace(book=explicit_book, not_on_server=False)],
            [],
            {
                "annotations": "#ds_annotations",
                "vocabulary": "#ds_vocabulary",
            },
            frozenset(),
            frozenset({"annotations", "vocabulary"}),
            None,
            False,
        )
    )
    assert_equal(
        worker_dialog.results_model.item(0, 1).text(),
        "Skipped — no mapped data is supported by this server",
        "unsupported-only row",
    )
    assert_contains(
        worker_dialog.result_summary_label.text(),
        "Unsupported server data skipped: Annotations, Vocabulary.",
        "unsupported archive summary",
    )
finally:
    sync_books_module.DeluxeSyncApi = original_worker_api
    worker_dialog.close()
    worker_gui.close()


print(
    "Stage 10 usability smoke passed: durable linked-book batch resolution, cached-ID fast path, "
    "compact dashboard counts/scopes, invalid-auth and standard/enhanced server status, safe conflict "
    "guidance, capability-filtered one-way archive sync, missing-server skip behavior, and explicit "
    "batch reuse for Sync Now and Review Changes."
)
