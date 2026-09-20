"""Deterministic Stage 7 server annotation -> Calibre archive smoke test."""

from __future__ import annotations

from copy import deepcopy
from types import SimpleNamespace

from calibre.customize.ui import find_plugin
from qt.core import QApplication, QWidget


app = QApplication.instance() or QApplication([])

plugin = find_plugin("Deluxe Sync")
if plugin is None:
    raise RuntimeError("Deluxe Sync is not installed")
plugin.load_actual_plugin(None)

from calibre_plugins.deluxe_sync.annotation_sync import (  # noqa: E402
    apply_annotation_archive,
    render_annotation_archive,
)
from calibre_plugins.deluxe_sync.api import (  # noqa: E402
    CapabilityError,
    DeluxeSyncApi,
)
from calibre_plugins.deluxe_sync.dialogs.sync_books import (  # noqa: E402
    SyncSelectedBooksDialog,
)
from calibre_plugins.deluxe_sync.settings import (  # noqa: E402
    get_column_mappings,
    set_column_mappings,
)


def assert_equal(actual, expected, label):
    if actual != expected:
        raise RuntimeError(f"{label}: expected {expected!r}, got {actual!r}")


annotations = [
    {
        "id": "note-2",
        "document": "linked-version-b.epub",
        "kind": "note",
        "deleted": False,
        "created_datetime": "2026-09-18T12:20:00Z",
        "updated_datetime": "2026-09-18T12:21:00Z",
        "text": 'Second <highlight> & "quoted"',
        "note": "Remember this\nfor later.",
        "chapter": "Chapter 2",
        "pageno": 22,
        "pageref": "22",
        "pos0": "/body/p[4]",
        "pos1": "/body/p[5]",
        "updated_by_device": "Boox Go7",
    },
    {
        "id": "highlight-1",
        "document": "linked-version-a.epub",
        "kind": "highlight",
        "deleted": False,
        "created_datetime": "2026-09-18T12:10:00Z",
        "updated_datetime": "2026-09-18T12:10:00Z",
        "text": "First highlight",
        "chapter": "Chapter 1",
        "page": {"page": 10, "total": 300},
        "origin_device": "Kindle 12th Gen",
    },
    {
        "id": "deleted-3",
        "document": "linked-version-a.epub",
        "kind": "highlight",
        "deleted": True,
        "text": "Must not appear",
    },
]

archive = render_annotation_archive({"logical_book_id": 8, "annotations": annotations})
assert_equal(archive.count, 2, "current annotation count")
if "Must not appear" in archive.html:
    raise RuntimeError("Deleted annotation leaked into the Calibre archive")
if archive.html.index("First highlight") > archive.html.index("Second &lt;highlight&gt;"):
    raise RuntimeError("Annotation archive ordering is not deterministic/chronological")
if 'Second <highlight>' in archive.html or "&amp;" not in archive.html:
    raise RuntimeError("Annotation HTML did not escape server text")
for expected_text in (
    "linked-version-a.epub",
    "linked-version-b.epub",
    "Kindle 12th Gen",
    "Boox Go7",
    "Chapter 1",
    "Chapter 2",
    "Start: /body/p[4]",
    "End: /body/p[5]",
    "Remember this<br>for later.",
):
    if expected_text not in archive.html:
        raise RuntimeError(f"Annotation archive lost context: {expected_text}")

reordered = render_annotation_archive(
    {"logical_book_id": 8, "annotations": list(reversed(annotations))}
)
assert_equal(reordered.html, archive.html, "deterministic archive regeneration")


class FakeDbApi:
    def __init__(self):
        self.values = {
            "#ds_annotations": {
                2: "<p>old archive</p>",
                9: "<p>other book</p>",
            }
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
                self.writes.append((name, book_id, value))
                affected.add(book_id)
        return affected


class FakeDb:
    library_id = "stage7-library"

    def __init__(self):
        self.new_api = FakeDbApi()

    def custom_field_metadata(self, include_composites=False):
        return {
            "#ds_annotations": {
                "name": "DS Annotations",
                "datatype": "comments",
            }
        }


db = FakeDb()
other_before = deepcopy(db.new_api.values["#ds_annotations"][9])
first_apply = apply_annotation_archive(db, 2, "#ds_annotations", archive)
if not first_apply.changed:
    raise RuntimeError("Stage 7 did not replace the existing archive")
assert_equal(first_apply.changed_book_ids, frozenset({2}), "changed archive row")
assert_equal(db.new_api.values["#ds_annotations"][9], other_before, "unrelated Calibre row")

write_count = len(db.new_api.writes)
second_apply = apply_annotation_archive(db, 2, "#ds_annotations", reordered)
if second_apply.changed:
    raise RuntimeError("Stage 7 rerun duplicated/replaced identical annotation HTML")
assert_equal(len(db.new_api.writes), write_count, "idempotent annotation writes")

empty_archive = render_annotation_archive({"annotations": []})
cleared = apply_annotation_archive(db, 2, "#ds_annotations", empty_archive)
if not cleared.changed or db.new_api.values["#ds_annotations"][2] != "":
    raise RuntimeError("Stage 7 did not clear stale Calibre annotation HTML")


class FakeAnnotationApi(DeluxeSyncApi):
    def __init__(self):
        super().__init__("https://example.invalid", plugin_version="stage7-smoke")
        self.logical_requests = []
        self.raw_requests = []
        self.resolution_calls = []

    def get_effective_progress(
        self,
        document,
        profile,
        *,
        capabilities_response=None,
        logical_book_id=None,
        library_response=None,
        logical_cache=None,
    ):
        self.resolution_calls.append((document, logical_book_id))
        if document == "linked-version-b.epub":
            return {
                "kind": "logical",
                "logical_book_id": 8,
                "members": [{"document": document}],
            }
        return {"kind": "raw", "document": document}

    def get_logical_book_annotations(self, logical_book_id, profile):
        self.logical_requests.append(logical_book_id)
        return {"logical_book_id": logical_book_id, "annotations": annotations}

    def get_document_annotations(self, document, profile):
        self.raw_requests.append(document)
        return {"document": document, "annotations": annotations[:1]}


api = FakeAnnotationApi()
profile = {"auth_mode": "pairing", "session": "redacted"}
enhanced_capabilities = {
    "capabilities": {
        "annotations": True,
        "logical_library": True,
    }
}
logical_payload = api.get_annotations_for_binding(
    "linked-version-b.epub",
    profile,
    capabilities_response=enhanced_capabilities,
    logical_book_id=999,
    library_response={"books": []},
    logical_cache={},
)
assert_equal(logical_payload["logical_book_id"], 8, "stale logical annotation resolution")
assert_equal(api.logical_requests, [8], "logical aggregate annotation endpoint")
assert_equal(
    api.resolution_calls,
    [("linked-version-b.epub", 999)],
    "logical relationship resolver call",
)

raw_payload = api.get_annotations_for_binding(
    "unlinked.epub",
    profile,
    capabilities_response=enhanced_capabilities,
    library_response={"books": []},
    logical_cache={},
)
assert_equal(raw_payload["document"], "unlinked.epub", "raw annotation fallback")
assert_equal(api.raw_requests, ["unlinked.epub"], "raw annotation endpoint")

try:
    api.get_annotations_for_binding(
        "unlinked.epub",
        profile,
        capabilities_response={"capabilities": {"annotations": False}},
    )
except CapabilityError:
    pass
else:
    raise RuntimeError("Annotation sync ignored the server annotations capability gate")


class MappingGui(QWidget):
    def __init__(self):
        super().__init__()
        self.current_db = FakeDb()
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
    set_column_mappings({"annotations": "#ds_annotations"})
    dialog = SyncSelectedBooksDialog(mapping_action, auto_sync=False)
    if not dialog.sync_button.isEnabled():
        raise RuntimeError("Annotation-only mapping did not enable Sync Selected Books")

    dialog._library_uuid = FakeDb.library_id
    fetch_result = SimpleNamespace(
        book=SimpleNamespace(book_id=2, title="Linked Annotation Book"),
        snapshot=None,
        error="",
        archive=archive,
        annotation_error="",
    )
    dialog._sync_loaded(
        (
            dialog._generation,
            [fetch_result],
            [],
            {"annotations": "#ds_annotations"},
            None,
            False,
        )
    )
    if "1 updated" not in dialog.result_summary_label.text():
        raise RuntimeError("Annotation-only sync did not report an updated book")
    row_result = dialog.results_model.item(0, 1).text()
    if "2 annotations" not in row_result:
        raise RuntimeError("Annotation count was not shown in Sync Selected Books")
    assert_equal(
        mapping_gui.current_db.new_api.values["#ds_annotations"][2],
        archive.html,
        "annotation-only dialog write",
    )

    dialog._sync_loaded(
        (
            dialog._generation,
            [fetch_result],
            [],
            {"annotations": "#ds_annotations"},
            None,
            False,
        )
    )
    if "1 already current" not in dialog.result_summary_label.text():
        raise RuntimeError("Annotation-only rerun was not idempotent")
    dialog.close()
finally:
    set_column_mappings(original_mappings)
    mapping_gui.close()


print(
    "Stage 7 annotation smoke passed: deterministic portable HTML, linked-source context, "
    "escaping, deleted filtering, exact-row replacement/clearing, idempotent reruns, "
    "capability gating, stale logical-id recovery, raw fallback, and annotation-only sync."
)
