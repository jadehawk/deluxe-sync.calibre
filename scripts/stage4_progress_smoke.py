"""Deterministic Stage 4 server-progress -> Calibre-column smoke test."""

from __future__ import annotations

from copy import deepcopy
from datetime import timezone
from types import SimpleNamespace

from calibre.customize.ui import find_plugin
from qt.core import QApplication, QWidget


app = QApplication.instance() or QApplication([])

plugin = find_plugin("Deluxe Sync")
if plugin is None:
    raise RuntimeError("Deluxe Sync is not installed")
plugin.load_actual_plugin(None)

from calibre_plugins.deluxe_sync.api import ApiError, DeluxeSyncApi  # noqa: E402
from calibre_plugins.deluxe_sync.dialogs.sync_books import SyncSelectedBooksDialog  # noqa: E402
from calibre_plugins.deluxe_sync.progress_sync import (  # noqa: E402
    apply_progress_snapshot,
    snapshot_from_progress,
)
from calibre_plugins.deluxe_sync.settings import (  # noqa: E402
    get_column_mappings,
    set_column_mappings,
)


def assert_equal(actual, expected, label):
    if actual != expected:
        raise RuntimeError(f"{label}: expected {expected!r}, got {actual!r}")


# ---------------------------------------------------------------------------
# Server payload normalization: mixed progress states + completed handling.
# ---------------------------------------------------------------------------

reading = snapshot_from_progress(
    {
        "percentage": 0.42,
        "progress": "/body/DocFragment[3]/body/div[2]/p[7]",
        "timestamp": 1_700_000_000,
    }
)
assert_equal(reading.progress, 42.0, "reading progress")
assert_equal(reading.status, "Reading", "reading status")
assert_equal(
    reading.last_location,
    "/body/DocFragment[3]/body/div[2]/p[7]",
    "reading location",
)
if reading.last_sync is None or reading.last_sync.tzinfo is None:
    raise RuntimeError("Stage 4 did not normalize the server timestamp as an aware datetime")
assert_equal(reading.last_sync.tzinfo, timezone.utc, "reading timestamp timezone")

finished = snapshot_from_progress(
    {
        "percentage": 1.0,
        "progress": "/body/DocFragment[9]/body/div[5]",
        "timestamp": 1_700_000_100,
    }
)
assert_equal(finished.progress, 100.0, "finished progress")
assert_equal(finished.status, "Finished", "finished status")

near_finished = snapshot_from_progress(
    {
        "percentage": 0.999,
        "progress": "/almost/end",
        "timestamp": 1_700_000_099,
    }
)
assert_equal(near_finished.progress, 99.9, "near-finished progress")
assert_equal(near_finished.status, "Reading", "near-finished status")

manual_finished = snapshot_from_progress(
    {
        "percentage": 0.75,
        "progress": "/manual/completion",
        "timestamp": 1_700_000_101,
        "reading_state": {"manual_completion": True},
    }
)
assert_equal(manual_finished.status, "Finished", "manual completion status")

cover_only = snapshot_from_progress(
    {
        "percentage": 0.005,
        "progress": "/cover",
        "timestamp": 1_700_000_102,
    }
)
assert_equal(cover_only.progress, 0.5, "cover-only progress")
assert_equal(cover_only.status, "Not started", "cover-only status")
assert_equal(cover_only.last_location, "/cover", "cover-only location")

active_reading = snapshot_from_progress(
    {
        "percentage": 0.01,
        "progress": "/chapter/one",
        "timestamp": 1_700_000_103,
    }
)
assert_equal(active_reading.progress, 1.0, "active-reading threshold progress")
assert_equal(active_reading.status, "Reading", "active-reading threshold status")

not_started = snapshot_from_progress(None)
assert_equal(not_started.progress, 0.0, "not-started progress")
assert_equal(not_started.status, "Not started", "not-started status")
assert_equal(not_started.last_location, "", "not-started location")
assert_equal(not_started.last_sync, None, "not-started timestamp")


# ---------------------------------------------------------------------------
# Exact-row Calibre writes + idempotent rerun.
# ---------------------------------------------------------------------------


class FakeDbApi:
    def __init__(self):
        self.values = {
            "#ds_progress": {2: 5.0, 9: 88.0},
            "#ds_status": {2: "Reading", 9: "Do not touch"},
            "#ds_last_location": {2: "/old", 9: "/other-book"},
            "#ds_last_sync": {2: None, 9: None},
        }
        self.writes = []

    def field_for(self, name, book_id, default_value=None):
        return self.values.get(name, {}).get(book_id, default_value)

    def set_field(self, name, values):
        affected = set()
        for book_id, value in values.items():
            before = self.values.setdefault(name, {}).get(book_id)
            if before != value:
                self.values[name][book_id] = value
                affected.add(book_id)
                self.writes.append((name, book_id, value))
        return affected


class FakeDb:
    def __init__(self):
        self.new_api = FakeDbApi()


mappings = {
    "progress": "#ds_progress",
    "status": "#ds_status",
    "last_location": "#ds_last_location",
    "last_sync": "#ds_last_sync",
}

db = FakeDb()
unrelated_before = {
    lookup: deepcopy(values.get(9))
    for lookup, values in db.new_api.values.items()
}

first_apply = apply_progress_snapshot(db, 2, mappings, reading)
assert_equal(
    set(first_apply.changed_fields),
    {"progress", "last_location", "last_sync"},
    "changed mapped fields",
)
assert_equal(first_apply.changed_book_ids, frozenset({2}), "changed Calibre row ids")
if any(book_id != 2 for _lookup, book_id, _value in db.new_api.writes):
    raise RuntimeError("Stage 4 wrote a Calibre row outside the requested book")

unrelated_after = {
    lookup: deepcopy(values.get(9))
    for lookup, values in db.new_api.values.items()
}
assert_equal(unrelated_after, unrelated_before, "unrelated Calibre row")

write_count = len(db.new_api.writes)
second_apply = apply_progress_snapshot(db, 2, mappings, reading)
assert_equal(second_apply.changed_fields, (), "idempotent changed fields")
assert_equal(second_apply.changed_book_ids, frozenset(), "idempotent changed rows")
assert_equal(len(db.new_api.writes), write_count, "idempotent write count")

finished_apply = apply_progress_snapshot(db, 2, mappings, finished)
if "progress" not in finished_apply.changed_fields or "status" not in finished_apply.changed_fields:
    raise RuntimeError("Completed-book sync did not update progress and status")
assert_equal(db.new_api.values["#ds_progress"][2], 100.0, "completed Calibre progress")
assert_equal(db.new_api.values["#ds_status"][2], "Finished", "completed Calibre status")


# ---------------------------------------------------------------------------
# Effective enhanced/logical state + standard KOSync fallback.
# ---------------------------------------------------------------------------


class FakeProgressApi(DeluxeSyncApi):
    def __init__(self):
        super().__init__("https://example.invalid", plugin_version="stage4-smoke")
        self.logical_requests = []
        self.kosync_requests = []

    def get_logical_book(self, logical_book_id, profile):
        self.logical_requests.append(logical_book_id)
        if logical_book_id == 999:
            raise ApiError("stale logical id", status=404)
        if logical_book_id != 7:
            raise ApiError("unknown logical id", status=404)
        return {
            "kind": "logical",
            "logical_book_id": 7,
            "title": "Linked Book",
            "progress": "/shared/current",
            "percentage": 0.77,
            "reading_state": "reading",
            "timestamp": 1_700_000_777,
            "members": [
                {"kind": "raw", "document": "member-a"},
                {"kind": "raw", "document": "member-b"},
            ],
        }

    def get_kosync_progress(self, document, profile):
        self.kosync_requests.append(document)
        return {
            "document": document,
            "progress": "/standard/current",
            "percentage": 0.31,
            "timestamp": 1_700_000_310,
        }


enhanced_capabilities = {"capabilities": {"logical_library": True}}
enhanced_library = {
    "books": [
        {
            "kind": "logical",
            "logical_book_id": 7,
            "title": "Linked Book",
            "percentage": 0.77,
        },
        {
            "kind": "raw",
            "document": "raw-unlinked",
            "title": "Raw Book",
            "progress": "/raw/current",
            "percentage": 0.18,
            "timestamp": 1_700_000_180,
        },
    ]
}
paired_profile = {"auth_mode": "pairing", "session": "redacted"}

api = FakeProgressApi()
logical_cache = {}
effective = api.get_effective_progress(
    "member-b",
    paired_profile,
    capabilities_response=enhanced_capabilities,
    logical_book_id=999,
    library_response=enhanced_library,
    logical_cache=logical_cache,
)
assert_equal(effective["logical_book_id"], 7, "current logical-book resolution")
assert_equal(effective["percentage"], 0.77, "effective logical percentage")
assert_equal(effective["progress"], "/shared/current", "effective logical location")
assert_equal(api.logical_requests[:2], [999, 7], "stale logical relationship reconciliation")

raw_effective = api.get_effective_progress(
    "raw-unlinked",
    paired_profile,
    capabilities_response=enhanced_capabilities,
    library_response=enhanced_library,
    logical_cache=logical_cache,
)
assert_equal(raw_effective["percentage"], 0.18, "unlinked raw percentage")
assert_equal(raw_effective["progress"], "/raw/current", "unlinked raw location")

try:
    api.get_effective_progress(
        "deleted-bound-document",
        paired_profile,
        capabilities_response=enhanced_capabilities,
        library_response=enhanced_library,
        logical_cache=logical_cache,
    )
except ApiError as error:
    assert_equal(error.status, 404, "missing enhanced bound-document status")
else:
    raise RuntimeError("Missing enhanced bound document silently reset progress instead of failing")

standard_profile = {
    "auth_mode": "kosync",
    "username": "reader",
    "userkey": "redacted",
}
standard = api.get_effective_progress(
    "standard-doc",
    standard_profile,
    capabilities_response={"capabilities": {}},
)
assert_equal(standard["percentage"], 0.31, "standard KOSync percentage")
assert_equal(standard["progress"], "/standard/current", "standard KOSync location")
assert_equal(api.kosync_requests, ["standard-doc"], "standard KOSync fallback request")


class RawProgressProbe(DeluxeSyncApi):
    def __init__(self, response=None, error=None):
        super().__init__("https://example.invalid", plugin_version="stage4-smoke")
        self.response = response
        self.error = error

    def _request_json(self, path, **kwargs):
        if self.error is not None:
            raise self.error
        return self.response


assert_equal(
    RawProgressProbe(response={}).get_kosync_progress("missing-doc", standard_profile),
    None,
    "empty stock KOSync response means document is not on server",
)
assert_equal(
    RawProgressProbe(
        error=ApiError("Field 'document' not provided.", status=403)
    ).get_kosync_progress("stale-doc", standard_profile),
    None,
    "malformed stale binding means document is not on server",
)
try:
    RawProgressProbe(
        error=ApiError("Different forbidden response", status=403)
    ).get_kosync_progress("forbidden-doc", standard_profile)
except ApiError:
    pass
else:
    raise RuntimeError("Unrelated KOSync 403 was incorrectly treated as a missing document")


# ---------------------------------------------------------------------------
# Sync-button readiness: mappings must exist and be live in this Calibre session.
# ---------------------------------------------------------------------------


class MappingDb:
    library_id = "stage4-mapping-library"

    def __init__(self):
        self.new_api = FakeDbApi()

    def custom_field_metadata(self, include_composites=False):
        return {
            "#ds_progress": {"name": "DS Progress", "datatype": "float"},
            "#ds_status": {"name": "DS Status", "datatype": "text"},
            "#ds_last_location": {"name": "DS Last Location", "datatype": "comments"},
            "#ds_last_sync": {"name": "DS Last Sync", "datatype": "datetime"},
        }


class MappingGui(QWidget):
    def __init__(self):
        super().__init__()
        self.current_db = MappingDb()
        self.must_restart_before_config = False
        self.library_view = SimpleNamespace(
            model=lambda: SimpleNamespace(refresh_ids=lambda _ids: None)
        )


mapping_gui = MappingGui()
mapping_action = SimpleNamespace(
    gui=mapping_gui,
    interface_action_base_plugin=SimpleNamespace(version_string="0.1.0.0"),
    show_config=lambda: None,
)
original_mappings = get_column_mappings()
try:
    empty_mappings = {
        "progress": "",
        "status": "",
        "last_location": "",
        "last_sync": "",
    }
    set_column_mappings(empty_mappings)
    no_mapping_dialog = SyncSelectedBooksDialog(mapping_action, auto_sync=False)
    if no_mapping_dialog.sync_button.isEnabled():
        raise RuntimeError("Stage 4 Sync button enabled with no mapped columns")
    if "Map at least one" not in no_mapping_dialog.status_label.text():
        raise RuntimeError("Stage 4 did not explain why Sync is disabled without mappings")
    no_mapping_dialog.close()

    set_column_mappings({**empty_mappings, "progress": "#ds_progress"})
    mapping_gui.must_restart_before_config = True
    restart_pending_dialog = SyncSelectedBooksDialog(mapping_action, auto_sync=False)
    if restart_pending_dialog.sync_button.isEnabled():
        raise RuntimeError("Stage 4 Sync button enabled while a Calibre restart is pending")
    if "Restart Calibre" not in restart_pending_dialog.status_label.text():
        raise RuntimeError("Stage 4 did not explain the pending custom-column restart")
    restart_pending_dialog.close()

    mapping_gui.must_restart_before_config = False
    ready_dialog = SyncSelectedBooksDialog(mapping_action)
    app.processEvents()
    if ready_dialog._generation != 0:
        raise RuntimeError("Sync Selected Books auto-ran before the user pressed Sync")
    if not ready_dialog.sync_button.isEnabled():
        raise RuntimeError("Stage 4 Sync button stayed disabled after the mapped column became live")
    assert_equal(
        ready_dialog.sync_button.text(),
        "Sync Selected Books",
        "initial sync button label",
    )

    auto_completion_calls = []
    original_refresh_availability = ready_dialog._refresh_sync_availability
    original_sync_selected_books = ready_dialog.sync_selected_books
    ready_dialog._auto_show_completion = True
    ready_dialog._refresh_sync_availability = lambda: True
    ready_dialog.sync_selected_books = (
        lambda *, show_completion=False: auto_completion_calls.append(show_completion)
    )
    ready_dialog._auto_sync_if_ready()
    assert_equal(
        auto_completion_calls,
        [True],
        "toolbar auto-sync completion modal flag",
    )
    ready_dialog._refresh_sync_availability = original_refresh_availability
    ready_dialog.sync_selected_books = original_sync_selected_books

    ready_dialog._library_uuid = MappingDb.library_id
    completion_modals = []
    ready_dialog._show_completion_summary = completion_modals.append
    fetch_result = SimpleNamespace(
        book=SimpleNamespace(book_id=2, title="Linked Smoke Book"),
        snapshot=reading,
        error="",
    )
    ready_dialog._sync_loaded(
        (
            ready_dialog._generation,
            [fetch_result],
            [("Unlinked Smoke Book", "Not linked", "", "")],
            {"progress": "#ds_progress"},
            None,
            True,
        )
    )
    expected_manual_summary = (
        "Sync complete: 1 updated, 0 already current, 1 not linked, 0 failed."
    )
    assert_equal(
        ready_dialog.result_summary_label.text(),
        expected_manual_summary,
        "button-row sync summary",
    )
    assert_equal(
        completion_modals,
        [expected_manual_summary],
        "manual completion modal summary",
    )
    assert_equal(ready_dialog.status_label.text(), "Sync complete.", "post-sync top status")
    assert_equal(ready_dialog.sync_button.text(), "Sync Again", "post-sync button label")

    ready_dialog._sync_loaded(
        (
            ready_dialog._generation,
            [fetch_result],
            [],
            {"progress": "#ds_progress"},
            None,
            False,
        )
    )
    assert_equal(
        ready_dialog.result_summary_label.text(),
        "Sync complete: 0 updated, 1 already current, 0 not linked, 0 failed.",
        "automatic sync summary",
    )
    assert_equal(
        completion_modals,
        [expected_manual_summary],
        "automatic sync modal suppression",
    )
    ready_dialog.close()

    set_column_mappings({**empty_mappings, "progress": "#missing_progress"})
    missing_column_dialog = SyncSelectedBooksDialog(mapping_action, auto_sync=False)
    if missing_column_dialog.sync_button.isEnabled():
        raise RuntimeError("Stage 4 Sync button enabled for a mapped column absent from the library")
    if "not available" not in missing_column_dialog.status_label.text():
        raise RuntimeError("Stage 4 did not explain an unavailable mapped column")
    missing_column_dialog.close()
finally:
    set_column_mappings(original_mappings)
    mapping_gui.close()


print(
    "Stage 4 progress smoke passed: normalization, exact-row writes, completed state, "
    "enhanced logical resolution, stale-link recovery, standard KOSync fallback, "
    "idempotent reruns, and mapping/restart Sync-button readiness gates."
)
