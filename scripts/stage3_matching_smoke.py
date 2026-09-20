"""Deterministic Stage 3 selected-book matching smoke test inside Calibre."""

from __future__ import annotations

from types import SimpleNamespace
from time import monotonic, sleep

from calibre.customize.ui import find_plugin
from qt.core import QApplication, QDialog, Qt


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

from calibre_plugins.deluxe_sync import bindings as bindings_module  # noqa: E402
from calibre_plugins.deluxe_sync.dialogs import match_books as match_dialog_module  # noqa: E402
from calibre_plugins.deluxe_sync.dialogs.match_books import MatchBooksDialog  # noqa: E402
from calibre_plugins.deluxe_sync.matching import (  # noqa: E402
    build_remote_records,
    match_book,
    match_books,
)
from calibre_plugins.deluxe_sync.models import (  # noqa: E402
    CalibreBook,
    RemoteRecord,
    selected_calibre_books,
)


class FakePrefs(dict):
    def __init__(self):
        super().__init__()
        self.defaults = {}


class FakeDbApi:
    def __init__(self):
        self.fields = {
            2: {
                "uuid": "selected-two",
                "title": "Selected Two",
                "authors": ("Author Two",),
                "identifiers": {"isbn": "978-2"},
                "series": "Selected Series",
                "series_index": 2,
            },
            4: {
                "uuid": "selected-four",
                "title": "Selected Four",
                "authors": ("Author Four",),
                "identifiers": {"asin": "ASIN-4"},
                "series": "",
                "series_index": None,
            },
            9: {
                "uuid": "not-selected",
                "title": "Must Not Be Read",
                "authors": ("Ignored",),
                "identifiers": {},
                "series": "",
                "series_index": None,
            },
        }
        self.field_requests = []

    def field_for(self, name, book_id, default_value=None):
        self.field_requests.append((name, book_id))
        return self.fields.get(book_id, {}).get(name, default_value)

    def formats(self, book_id, verify_formats=True):
        if book_id == 2:
            return ("EPUB", "PDF")
        return ("EPUB",)

    def format_abspath(self, book_id, fmt):
        return f"C:/Calibre/{book_id}/Book {book_id}.{fmt.lower()}"


class FakeLibraryView:
    def get_selected_ids(self, as_set=False):
        if as_set:
            raise RuntimeError("Stage 3 should preserve Calibre's selected order")
        return [2, 4]


db_api = FakeDbApi()
fake_gui = SimpleNamespace(
    current_db=SimpleNamespace(library_id="stage3-library", new_api=db_api),
    library_view=FakeLibraryView(),
)
selected = selected_calibre_books(fake_gui)
if [book.book_id for book in selected] != [2, 4]:
    raise RuntimeError("Stage 3 did not restrict local discovery to selected books")
if any(book_id == 9 for _field, book_id in db_api.field_requests):
    raise RuntimeError("Stage 3 scanned an unselected Calibre book")
if selected[0].book_uuid != "selected-two":
    raise RuntimeError("Calibre UUID was not used as the durable local identity")
if selected[0].filenames != ("Book 2.epub", "Book 2.pdf"):
    raise RuntimeError("Selected-book format filenames were not collected correctly")


library = {
    "books": [
        {
            "kind": "raw",
            "document": "raw-asin",
            "title": "Remote ASIN Edition",
            "authors": "Remote Author",
            "asin": "ASIN-EXACT",
            "filename": "Remote ASIN.epub",
        },
        {
            "kind": "raw",
            "document": "raw-isbn",
            "title": "Remote ISBN Edition",
            "authors": "Remote Author",
            "isbn": "978-1-4028-9462-6",
            "filename": "Remote ISBN.epub",
        },
        {
            "kind": "raw",
            "document": "raw-title",
            "title": "The Title Match",
            "authors": "Author One",
            "filename": "Different Name.epub",
        },
        {
            "kind": "raw",
            "document": "series-one",
            "title": "Series Book",
            "authors": "Series Author",
            "series": "Example Saga",
            "series_index": 1,
        },
        {
            "kind": "raw",
            "document": "series-two",
            "title": "Series Book",
            "authors": "Series Author",
            "series": "Example Saga",
            "series_index": 2,
        },
        {
            "kind": "raw",
            "document": "ambiguous-a",
            "title": "Shared Title",
            "authors": "Shared Author",
        },
        {
            "kind": "raw",
            "document": "ambiguous-b",
            "title": "Shared Title",
            "authors": "Shared Author",
        },
        {
            "kind": "raw",
            "document": "filename-hint",
            "title": "Server Renamed Title",
            "authors": "Different Author",
            "filename": "Filename Only.epub",
        },
        {
            "kind": "logical",
            "logical_book_id": 42,
            "document": "logical:42",
            "title": "Logical Book",
            "authors": "Logical Author",
            "series": "Linked Saga",
            "series_index": 3,
            "linked_count": 2,
        },
    ],
    "visible_count": 9,
    "raw_count": 10,
    "logical_count": 1,
    "linked_raw_count": 2,
}

logical_details = {
    42: {
        "kind": "logical",
        "logical_book_id": 42,
        "title": "Logical Book",
        "authors": "Logical Author",
        "series": "Linked Saga",
        "series_index": 3,
        "members": [
            {
                "kind": "raw",
                "logical_book_id": 42,
                "document": "raw-logical-a",
                "title": "Logical Book — EPUB",
                "authors": "Logical Author",
                "isbn": "9780000000042",
                "asin": "LOGICAL-A",
                "series": "Linked Saga",
                "series_index": 3,
                "filename": "Logical EPUB.epub",
            },
            {
                "kind": "raw",
                "logical_book_id": 42,
                "document": "raw-logical-b",
                "title": "Logical Book — PDF",
                "authors": "Logical Author",
                "isbn": "9780000000043",
                "asin": "LOGICAL-B",
                "series": "Linked Saga",
                "series_index": 3,
                "filename": "Logical PDF.pdf",
            },
        ],
    }
}

remote_records = build_remote_records(library, logical_details)
if len([item for item in remote_records if item.logical_book_id == 42]) != 2:
    raise RuntimeError("Logical-book members were not expanded into durable raw identities")


def local(
    uuid,
    title,
    authors,
    *,
    identifiers=None,
    series="",
    series_index=None,
    filenames=(),
):
    return CalibreBook(
        library_uuid="stage3-library",
        book_uuid=uuid,
        book_id=len(uuid),
        title=title,
        authors=tuple(authors),
        identifiers=dict(identifiers or {}),
        series=series,
        series_index=series_index,
        filenames=tuple(filenames),
    )


local_books = [
    local(
        "book-asin",
        "Local ASIN title",
        ("Someone Else",),
        identifiers={"asin": "asin exact"},
    ),
    local(
        "book-isbn",
        "Local ISBN title",
        ("Someone Else",),
        identifiers={"isbn": "9781402894626"},
    ),
    local("book-title", "The Title Match", ("Author One",)),
    local(
        "book-series",
        "Series Book",
        ("Series Author",),
        series="Example Saga",
        series_index=2,
    ),
    local("book-ambiguous", "Shared Title", ("Shared Author",)),
    local(
        "book-filename",
        "Different Local Title",
        ("Local Author",),
        filenames=("Alternate Format.pdf", "Filename Only.epub"),
    ),
    local(
        "book-logical",
        "Logical Book",
        ("Logical Author",),
        identifiers={"isbn": "9780000000042"},
        series="Linked Saga",
        series_index=3,
    ),
    local("book-none", "No Server Match", ("Nobody",)),
]

results = match_books(local_books, remote_records)
by_uuid = {result.local.book_uuid: result for result in results}

expected_states = {
    "book-asin": "matched",
    "book-isbn": "matched",
    "book-title": "matched",
    "book-series": "matched",
    "book-ambiguous": "needs_review",
    "book-filename": "needs_review",
    "book-logical": "matched",
    "book-none": "no_match",
}
for book_uuid, state in expected_states.items():
    if by_uuid[book_uuid].state != state:
        raise RuntimeError(
            f"Unexpected Stage 3 match state for {book_uuid}: "
            f"{by_uuid[book_uuid].state}"
        )

if by_uuid["book-asin"].primary_candidate.remote.document != "raw-asin":
    raise RuntimeError("Exact ASIN did not take matching priority")
if by_uuid["book-isbn"].primary_candidate.remote.document != "raw-isbn":
    raise RuntimeError("Exact ISBN did not match the intended raw document")
if by_uuid["book-series"].primary_candidate.remote.document != "series-two":
    raise RuntimeError("Series/index did not disambiguate equal title/author candidates")
if len(by_uuid["book-ambiguous"].candidates) != 2:
    raise RuntimeError("Ambiguous same-title books were not preserved for review")
if any(candidate.automatic for candidate in by_uuid["book-ambiguous"].candidates):
    raise RuntimeError("An ambiguous title/author candidate was incorrectly automatic")
if by_uuid["book-filename"].primary_candidate.automatic:
    raise RuntimeError("Filename-only matching must never be automatic")
logical_candidate = by_uuid["book-logical"].primary_candidate
if (
    logical_candidate.remote.document != "raw-logical-a"
    or logical_candidate.remote.logical_book_id != 42
):
    raise RuntimeError("Logical-book matching lost the durable raw document identity")


existing_binding = {
    "server_profile_id": "stage3-profile",
    "calibre_library_uuid": "stage3-library",
    "calibre_book_uuid": "book-logical",
    "calibre_book_id": 7,
    "server_document": "raw-logical-a",
    "logical_book_id": None,
    "enrolled": True,
    "auto_sync": False,
}
bound = match_book(
    by_uuid["book-logical"].local,
    remote_records,
    existing_binding,
)
if bound.state != "already_enrolled" or not bound.binding_changed:
    raise RuntimeError("Existing raw binding was not reused and reconciled")
if bound.binding.get("logical_book_id") != 42:
    raise RuntimeError("New server-side logical membership was not detected")

unlinked = [
    RemoteRecord(
        document="raw-logical-a",
        title="Logical Book — EPUB",
        authors="Logical Author",
        isbn="9780000000042",
    )
]
detached_binding = dict(bound.binding)
detached = match_book(by_uuid["book-logical"].local, unlinked, detached_binding)
if not detached.binding_changed or detached.binding.get("logical_book_id") is not None:
    raise RuntimeError("Server-side unlink was not detected without losing the raw binding")


original_prefs = bindings_module._PREFS
bindings_module._PREFS = FakePrefs()
try:
    candidate = by_uuid["book-logical"].primary_candidate
    saved = bindings_module.bind_candidate(
        by_uuid["book-logical"].local,
        candidate,
        "stage3-profile",
    )
    if saved["server_document"] != "raw-logical-a" or not saved["enrolled"]:
        raise RuntimeError("Approved Stage 3 binding did not persist correctly")
    loaded = bindings_module.get_binding(
        "stage3-library",
        "book-logical",
        "stage3-profile",
    )
    if loaded is None or loaded["logical_book_id"] != 42:
        raise RuntimeError("Durable Stage 3 binding could not be reloaded")
    bindings_module.forget_binding(
        "stage3-library",
        "book-logical",
        "stage3-profile",
    )
    if bindings_module.get_binding(
        "stage3-library",
        "book-logical",
        "stage3-profile",
    ) is not None:
        raise RuntimeError("Stage 3 binding clear did not persist")
finally:
    bindings_module._PREFS = original_prefs


class FakeApi:
    def __init__(self, server_url, plugin_version=None):
        self.server_url = server_url
        self.plugin_version = plugin_version

    def discover_capabilities(self, profile=None):
        return {
            "server_type": "enhanced",
            "capabilities": {"calibre_bindings": False},
        }

    def get_library(self, profile, capabilities_response=None):
        return library

    def get_logical_book(self, logical_book_id, profile):
        return logical_details[logical_book_id]


original_profile = match_dialog_module.get_active_server_profile
original_profile_id = match_dialog_module.get_active_server_profile_id
original_api = match_dialog_module.DeluxeSyncApi
original_selected_books = MatchBooksDialog._selected_books
original_prefs = bindings_module._PREFS

bindings_module._PREFS = FakePrefs()
match_dialog_module.get_active_server_profile = lambda: {
    "server_url": "http://stage3.invalid",
    "auth_mode": "pairing",
}
match_dialog_module.get_active_server_profile_id = lambda: "stage3-profile"
match_dialog_module.DeluxeSyncApi = FakeApi
MatchBooksDialog._selected_books = lambda self: list(local_books)

dialog = None
try:
    fake_action = SimpleNamespace(
        gui=QDialog(),
        interface_action_base_plugin=SimpleNamespace(version_string="0.1.0.0"),
        show_library_dialog=lambda: None,
    )
    dialog = MatchBooksDialog(fake_action)
    dialog.refresh_matches()
    wait_until(
        lambda: not dialog._loading
        and dialog.results_model.rowCount() == len(local_books),
        "Stage 3 match-review load",
    )

    states = {result.local.book_uuid: result.state for result in dialog._results}
    if states != expected_states:
        raise RuntimeError(f"Match-review dialog states were unexpected: {states}")
    if "8 selected" not in dialog.status_label.text():
        raise RuntimeError("Match-review summary did not report selected-book scope")

    bind_row = None
    for row in range(dialog.results_model.rowCount()):
        result = dialog.results_model.item(row, 0).data(Qt.ItemDataRole.UserRole)
        if result.local.book_uuid == "book-isbn":
            bind_row = row
            break
    if bind_row is None:
        raise RuntimeError("Could not locate automatic candidate in match-review table")

    dialog.results_table.setCurrentIndex(dialog.results_model.index(bind_row, 0))
    app.processEvents()
    if dialog.candidates_model.rowCount() != 1:
        raise RuntimeError("Automatic match candidate details were not displayed")
    dialog.candidates_table.setCurrentIndex(dialog.candidates_model.index(0, 0))
    app.processEvents()
    if not dialog.bind_button.isEnabled():
        raise RuntimeError("Explicit Bind Selected Candidate action was not available")

    dialog.bind_selected_candidate()
    bound_isbn = bindings_module.get_binding(
        "stage3-library",
        "book-isbn",
        "stage3-profile",
    )
    if bound_isbn is None or bound_isbn["server_document"] != "raw-isbn":
        raise RuntimeError("Explicit UI binding did not persist the raw document")
finally:
    if dialog is not None:
        dialog.close()
    match_dialog_module.get_active_server_profile = original_profile
    match_dialog_module.get_active_server_profile_id = original_profile_id
    match_dialog_module.DeluxeSyncApi = original_api
    MatchBooksDialog._selected_books = original_selected_books
    bindings_module._PREFS = original_prefs


print("Deluxe Sync Stage 3 matching smoke test passed")
