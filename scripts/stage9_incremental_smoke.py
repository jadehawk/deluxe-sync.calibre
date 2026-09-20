"""Deterministic Stage 9 change-cursor / incremental synchronization smoke test."""

from __future__ import annotations

from copy import deepcopy
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

from calibre.customize.ui import find_plugin


plugin = find_plugin("Deluxe Sync")
if plugin is None:
    raise RuntimeError("Deluxe Sync is not installed")
plugin.load_actual_plugin(None)

from calibre_plugins.deluxe_sync import bindings as bindings_module  # noqa: E402
from calibre_plugins.deluxe_sync.api import ApiError, DeluxeSyncApi  # noqa: E402
from calibre_plugins.deluxe_sync.dialogs import sync_books as sync_books_module  # noqa: E402
from calibre_plugins.deluxe_sync.incremental_sync import (  # noqa: E402
    COMPONENT_ANNOTATIONS,
    COMPONENT_METADATA,
    COMPONENT_PROGRESS,
    COMPONENT_VOCABULARY,
    ChangeWindow,
    change_components_for_binding,
    read_change_window,
    refresh_components_for_binding,
    update_change_tracking,
)


PROFILE = {"auth_mode": "pairing", "session": "redacted"}


def assert_equal(actual, expected, label):
    if actual != expected:
        raise RuntimeError(f"{label}: expected {expected!r}, got {actual!r}")


def change(change_id, entity_type, entity_key, operation="upsert", payload=None):
    return {
        "id": change_id,
        "entity_type": entity_type,
        "entity_key": entity_key,
        "operation": operation,
        "device_id": None,
        "payload": dict(payload or {}),
        "created_at": change_id,
    }


class FakeJournalApi:
    def __init__(self, changes):
        self.changes = [deepcopy(item) for item in changes]
        self.calls = []

    def get_changes(self, after, profile, *, limit=200):
        self.calls.append((after, limit))
        page = [item for item in self.changes if item["id"] > after][:limit]
        return {
            "after": after,
            "changes": deepcopy(page),
            "next_cursor": page[-1]["id"] if page else after,
        }


baseline_api = FakeJournalApi(
    [change(index, "document", f"doc-{index}", "metadata") for index in range(1, 206)]
)
baseline = read_change_window(baseline_api, PROFILE, None)
if not baseline.full_refresh:
    raise RuntimeError("Missing Stage 9 cursor did not request a full baseline refresh")
assert_equal(baseline.next_cursor, 205, "baseline high-water cursor")
assert_equal(
    baseline_api.calls,
    [(0, 200), (200, 200)],
    "baseline change-journal pagination",
)


initialized = [
    COMPONENT_PROGRESS,
    COMPONENT_METADATA,
    COMPONENT_ANNOTATIONS,
    COMPONENT_VOCABULARY,
]
binding_a = {
    "server_profile_id": "stage9-profile",
    "calibre_library_uuid": "stage9-library",
    "calibre_book_uuid": "stage9-book-a",
    "calibre_book_id": 1,
    "server_document": "doc-a",
    "logical_book_id": 42,
    "enrolled": True,
    "auto_sync": False,
    "last_change_cursor": 10,
    "change_cursor_components": initialized,
}
binding_b = {
    **binding_a,
    "calibre_book_uuid": "stage9-book-b",
    "calibre_book_id": 2,
    "server_document": "doc-b",
    "logical_book_id": None,
}


progress_window = ChangeWindow(
    next_cursor=11,
    changes=(change(11, "progress", "doc-a"),),
)
assert_equal(
    refresh_components_for_binding(
        progress_window,
        binding_a,
        {COMPONENT_PROGRESS},
    ),
    frozenset({COMPONENT_PROGRESS}),
    "affected progress book",
)
assert_equal(
    refresh_components_for_binding(
        progress_window,
        binding_b,
        {COMPONENT_PROGRESS},
    ),
    frozenset(),
    "unrelated progress book",
)


metadata_event = change(12, "document", "doc-a", "metadata")
assert_equal(
    change_components_for_binding(metadata_event, binding_a),
    frozenset({COMPONENT_METADATA}),
    "metadata event routing",
)
assert_equal(
    change_components_for_binding(metadata_event, binding_b),
    frozenset(),
    "unrelated metadata event routing",
)


annotation_event = change(
    13,
    "annotation",
    "annotation-13",
    payload={"document": "linked-member-b", "revision": 2, "deleted": False},
)
assert_equal(
    change_components_for_binding(
        annotation_event,
        binding_a,
        member_documents={"doc-a", "linked-member-b"},
    ),
    frozenset({COMPONENT_ANNOTATIONS}),
    "linked-member annotation routing",
)
assert_equal(
    change_components_for_binding(annotation_event, binding_b),
    frozenset(),
    "unrelated annotation routing",
)


vocabulary_event = change(14, "vocabulary", "vocab-14")
assert_equal(
    change_components_for_binding(
        vocabulary_event,
        binding_a,
        effective_title="Resolved Server Title",
        vocabulary_title="resolved server title",
    ),
    frozenset({COMPONENT_VOCABULARY}),
    "vocabulary title routing",
)
assert_equal(
    change_components_for_binding(
        vocabulary_event,
        binding_b,
        effective_title="Another Book",
        vocabulary_title="Resolved Server Title",
    ),
    frozenset(),
    "unrelated vocabulary routing",
)


logical_event = change(15, "logical_book", "42", "progress")
assert_equal(
    change_components_for_binding(logical_event, binding_a),
    frozenset({COMPONENT_PROGRESS}),
    "logical-book progress routing",
)
assert_equal(
    change_components_for_binding(
        change(16, "logical_book", "42", "presentation"),
        binding_a,
    ),
    frozenset({COMPONENT_METADATA}),
    "logical-book presentation routing",
)
assert_equal(
    change_components_for_binding(
        change(17, "logical_book", "42", "create"),
        binding_a,
    ),
    frozenset(
        {
            COMPONENT_PROGRESS,
            COMPONENT_METADATA,
            COMPONENT_ANNOTATIONS,
            COMPONENT_VOCABULARY,
        }
    ),
    "logical-book membership routing",
)


no_delta = ChangeWindow(next_cursor=20, changes=())
partially_initialized = {
    **binding_a,
    "last_change_cursor": 20,
    "change_cursor_components": [COMPONENT_PROGRESS],
}
assert_equal(
    refresh_components_for_binding(
        no_delta,
        partially_initialized,
        {COMPONENT_PROGRESS, COMPONENT_VOCABULARY},
    ),
    frozenset({COMPONENT_VOCABULARY}),
    "newly mapped component baseline",
)
fully_initialized = update_change_tracking(
    partially_initialized,
    cursor=20,
    successful_components={COMPONENT_VOCABULARY},
)
assert_equal(
    refresh_components_for_binding(
        no_delta,
        fully_initialized,
        {COMPONENT_PROGRESS, COMPONENT_VOCABULARY},
    ),
    frozenset(),
    "initialized no-delta no-op",
)
failed_retry = update_change_tracking(
    fully_initialized,
    cursor=21,
    failed_components={COMPONENT_VOCABULARY},
)
assert_equal(
    refresh_components_for_binding(
        ChangeWindow(next_cursor=21, changes=()),
        failed_retry,
        {COMPONENT_PROGRESS, COMPONENT_VOCABULARY},
    ),
    frozenset({COMPONENT_VOCABULARY}),
    "failed component retry baseline",
)


valid_delta_api = FakeJournalApi(
    [
        change(9, "document", "older", "metadata"),
        change(10, "document", "doc-a", "metadata"),
        change(11, "progress", "doc-a"),
        change(14, "annotation", "ann-14", payload={"document": "doc-a"}),
    ]
)
valid_delta = read_change_window(valid_delta_api, PROFILE, 10)
if valid_delta.full_refresh:
    raise RuntimeError("Valid saved cursor incorrectly triggered full reconciliation")
assert_equal(
    [item["id"] for item in valid_delta.changes],
    [11, 14],
    "valid cursor delta rows",
)
assert_equal(valid_delta.next_cursor, 14, "valid cursor next cursor")


stale_api = FakeJournalApi(
    [
        change(1, "document", "doc-a", "metadata"),
        change(2, "progress", "doc-a"),
        change(4, "annotation", "ann-4", payload={"document": "doc-a"}),
    ]
)
stale = read_change_window(stale_api, PROFILE, 99)
if not stale.full_refresh or stale.fallback_reason != "stale-cursor":
    raise RuntimeError("Stale Stage 9 cursor did not fall back to full reconciliation")
assert_equal(stale.next_cursor, 4, "stale cursor baseline high-water")

invalid = read_change_window(stale_api, PROFILE, "not-a-cursor")
if not invalid.full_refresh or invalid.fallback_reason != "missing-or-invalid-cursor":
    raise RuntimeError("Invalid Stage 9 cursor did not fall back to full reconciliation")


class ErroringJournalApi(FakeJournalApi):
    def get_changes(self, after, profile, *, limit=200):
        if after:
            raise ApiError("cursor expired", status=410)
        return super().get_changes(after, profile, limit=limit)


expired = read_change_window(
    ErroringJournalApi([change(1, "document", "doc-a", "metadata")]),
    PROFILE,
    10,
)
if not expired.full_refresh or expired.next_cursor != 1:
    raise RuntimeError("Expired cursor HTTP response did not recover with a baseline")


class FakeChangesClient(DeluxeSyncApi):
    def __init__(self):
        super().__init__("https://example.invalid", plugin_version="stage9-smoke")
        self.requests = []

    def _request_json(self, path, **kwargs):
        self.requests.append(path)
        parsed = urlsplit(path)
        query = parse_qs(parsed.query)
        if parsed.path != "/api/v1/changes":
            raise RuntimeError(f"Unexpected change-journal request: {path}")
        after = int(query["after"][0])
        limit = int(query["limit"][0])
        return {"after": after, "changes": [], "next_cursor": after, "limit": limit}


changes_client = FakeChangesClient()
changes_client.get_changes(7, PROFILE, limit=25)
assert_equal(len(changes_client.requests), 1, "change-journal API request count")
query = parse_qs(urlsplit(changes_client.requests[0]).query)
assert_equal(query.get("after"), ["7"], "change-journal after query")
assert_equal(query.get("limit"), ["25"], "change-journal limit query")


class FakeVocabularyChangeClient(DeluxeSyncApi):
    def __init__(self):
        super().__init__("https://example.invalid", plugin_version="stage9-smoke")
        self.requests = []

    def _request_json(self, path, **kwargs):
        self.requests.append(path)
        if path == "/api/v1/vocabulary/deleted-vocab":
            raise ApiError("not found", status=404)
        if path.startswith("/api/v1/vocabulary?"):
            return {
                "entries": [
                    {
                        "id": "deleted-vocab",
                        "word": "ephemeral",
                        "title": "Resolved Server Title",
                        "deleted": True,
                    }
                ],
                "total": 1,
                "limit": 500,
                "offset": 0,
            }
        raise RuntimeError(f"Unexpected vocabulary request: {path}")


vocab_client = FakeVocabularyChangeClient()
deleted_entry = vocab_client.get_vocabulary_entry_for_change(
    "deleted-vocab",
    PROFILE,
)
assert_equal(
    deleted_entry.get("title"),
    "Resolved Server Title",
    "deleted vocabulary change title recovery",
)
if not deleted_entry.get("deleted"):
    raise RuntimeError("Deleted vocabulary change lost tombstone state")


class FakePrefs(dict):
    def __init__(self):
        super().__init__()
        self.defaults = {}


original_prefs = bindings_module._PREFS
try:
    bindings_module._PREFS = FakePrefs()
    tracked = update_change_tracking(
        {**binding_a, "change_cursor_components": []},
        cursor=55,
        successful_components={COMPONENT_PROGRESS, COMPONENT_ANNOTATIONS},
    )
    bindings_module.save_binding_record(tracked, "stage9-profile")
    reloaded = bindings_module.get_binding(
        "stage9-library",
        "stage9-book-a",
        "stage9-profile",
    )
    if not isinstance(reloaded, dict):
        raise RuntimeError("Saved Stage 9 binding could not be reloaded")
    assert_equal(reloaded.get("last_change_cursor"), 55, "persisted change cursor")
    assert_equal(
        set(reloaded.get("change_cursor_components") or []),
        {COMPONENT_PROGRESS, COMPONENT_ANNOTATIONS},
        "persisted initialized components",
    )

    rebound = dict(reloaded)
    rebound["logical_book_id"] = 77
    bindings_module.save_binding_record(rebound, "stage9-profile")
    restarted_view = bindings_module.get_library_bindings(
        "stage9-library",
        "stage9-profile",
    )
    assert_equal(
        restarted_view["stage9-book-a"].get("last_change_cursor"),
        55,
        "cursor survives binding reload",
    )
    assert_equal(
        restarted_view["stage9-book-a"].get("logical_book_id"),
        77,
        "delta-discovered logical membership persistence",
    )
finally:
    bindings_module._PREFS = original_prefs


try:
    read_change_window(
        FakeJournalApi(
            [
                change(10, "document", "doc-a", "metadata"),
                change(9, "progress", "doc-a"),
            ]
        ),
        PROFILE,
        8,
    )
except ApiError:
    pass
else:
    raise RuntimeError("Non-monotonic change-journal rows were accepted")


worker_api_instances = []


class NoDeltaWorkerApi:
    def __init__(self, server_url, *, plugin_version=""):
        self.calls = []
        worker_api_instances.append(self)

    def discover_capabilities(self, profile):
        return {
            "server_type": "enhanced",
            "capabilities": {
                "change_journal": True,
                "logical_library": True,
                "annotations": True,
                "vocabulary_builder": True,
            },
        }

    def get_changes(self, after, profile, *, limit=200):
        self.calls.append(("changes", after, limit))
        if after == 9 and limit == 1:
            return {
                "after": 9,
                "changes": [change(10, "progress", "doc-a")],
                "next_cursor": 10,
            }
        if after == 10:
            return {"after": 10, "changes": [], "next_cursor": 10}
        raise RuntimeError(f"Unexpected worker change-journal request: {after=} {limit=}")

    def _forbidden(self, name):
        self.calls.append((name,))
        raise RuntimeError(f"No-delta worker unexpectedly called {name}")

    def get_library(self, *args, **kwargs):
        return self._forbidden("library")

    def get_logical_book(self, *args, **kwargs):
        return self._forbidden("logical-book")

    def get_effective_progress(self, *args, **kwargs):
        return self._forbidden("progress")

    def get_annotations_for_binding(self, *args, **kwargs):
        return self._forbidden("annotations")

    def get_vocabulary_for_binding(self, *args, **kwargs):
        return self._forbidden("vocabulary")

    def get_vocabulary_entry_for_change(self, *args, **kwargs):
        return self._forbidden("vocabulary-change")


emitted_payloads = []
fake_worker = SimpleNamespace(
    _plugin_version=lambda: "stage9-smoke",
    _bridge=SimpleNamespace(
        completed=SimpleNamespace(emit=lambda payload: emitted_payloads.append(payload))
    ),
)
worker_binding = {
    **binding_a,
    "last_change_cursor": 10,
    "change_cursor_components": [
        COMPONENT_PROGRESS,
        COMPONENT_METADATA,
        COMPONENT_ANNOTATIONS,
        COMPONENT_VOCABULARY,
    ],
}
worker_book = SimpleNamespace(book_id=1, title="No Delta Book")
original_worker_api = sync_books_module.DeluxeSyncApi
try:
    sync_books_module.DeluxeSyncApi = NoDeltaWorkerApi
    sync_books_module.SyncSelectedBooksDialog._load_worker(
        fake_worker,
        99,
        [(worker_book, worker_binding)],
        [],
        "https://example.invalid",
        PROFILE,
        {
            "progress": "#ds_progress",
            "annotations": "#ds_annotations",
            "vocabulary": "#ds_vocabulary",
        },
        False,
    )
finally:
    sync_books_module.DeluxeSyncApi = original_worker_api

assert_equal(len(emitted_payloads), 1, "no-delta worker completion count")
(
    generation,
    worker_results,
    skipped,
    worker_mappings,
    active_components,
    unsupported_components,
    fatal_error,
    show_completion,
) = emitted_payloads[0]
assert_equal(generation, 99, "no-delta worker generation")
if fatal_error is not None:
    raise RuntimeError(f"No-delta worker failed unexpectedly: {fatal_error}")
assert_equal(len(worker_results), 1, "no-delta worker result count")
assert_equal(
    worker_results[0].refresh_components,
    frozenset(),
    "no-delta worker refresh components",
)
assert_equal(worker_results[0].change_cursor, 10, "no-delta worker cursor")
assert_equal(
    worker_api_instances[0].calls,
    [("changes", 9, 1), ("changes", 10, 200)],
    "no-delta worker network calls",
)


print(
    "Stage 9 incremental-sync smoke passed: paged baseline/high-water cursor, exact progress/metadata/"
    "annotation/vocabulary/logical routing, no-delta skips, per-component initialization/retry, "
    "valid delta reads, invalid/stale/expired cursor fallback, API query contract, deleted vocabulary "
    "resolution, and durable binding cursor/component state."
)
