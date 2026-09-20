"""Deterministic Stage 8 server vocabulary -> Calibre archive smoke test."""

from __future__ import annotations

from copy import deepcopy
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

from calibre.customize.ui import find_plugin
from qt.core import QApplication, QWidget


app = QApplication.instance() or QApplication([])

plugin = find_plugin("Deluxe Sync")
if plugin is None:
    raise RuntimeError("Deluxe Sync is not installed")
plugin.load_actual_plugin(None)

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
from calibre_plugins.deluxe_sync.vocabulary_sync import (  # noqa: E402
    apply_vocabulary_archive,
    render_vocabulary_archive,
)


def assert_equal(actual, expected, label):
    if actual != expected:
        raise RuntimeError(f"{label}: expected {expected!r}, got {actual!r}")


entries = [
    {
        "id": "vocab-2",
        "word": "  quaint  ",
        "title": "Resolved Server Title",
        "deleted": False,
    },
    {
        "id": "vocab-1",
        "word": "Quaint",
        "title": "Resolved Server Title",
        "deleted": False,
    },
    {
        "id": "vocab-3",
        "word": "obdurate & <rare>",
        "title": "Resolved Server Title",
        "deleted": False,
    },
    {
        "id": "vocab-deleted",
        "word": "must-not-appear",
        "title": "Resolved Server Title",
        "deleted": True,
    },
]

archive = render_vocabulary_archive({"entries": entries})
assert_equal(archive.count, 2, "normalized unique vocabulary count")
if "must-not-appear" in archive.html:
    raise RuntimeError("Deleted vocabulary leaked into the Calibre archive")
if "obdurate &amp; &lt;rare&gt;" not in archive.html:
    raise RuntimeError("Vocabulary HTML did not escape server words")
if archive.html.index("obdurate") > archive.html.index("Quaint"):
    raise RuntimeError("Vocabulary archive ordering is not deterministic")
reordered = render_vocabulary_archive({"entries": list(reversed(entries))})
assert_equal(reordered.html, archive.html, "deterministic vocabulary regeneration")


class FakeDbApi:
    def __init__(self):
        self.values = {
            "#ds_vocabulary": {
                2: "<p>old vocabulary</p>",
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
    library_id = "stage8-library"

    def __init__(self):
        self.new_api = FakeDbApi()

    def custom_field_metadata(self, include_composites=False):
        return {
            "#ds_vocabulary": {
                "name": "DS Vocabulary",
                "datatype": "comments",
            }
        }


db = FakeDb()
other_before = deepcopy(db.new_api.values["#ds_vocabulary"][9])
first_apply = apply_vocabulary_archive(db, 2, "#ds_vocabulary", archive)
if not first_apply.changed:
    raise RuntimeError("Stage 8 did not replace the existing vocabulary archive")
assert_equal(first_apply.changed_book_ids, frozenset({2}), "changed vocabulary row")
assert_equal(db.new_api.values["#ds_vocabulary"][9], other_before, "unrelated Calibre row")

write_count = len(db.new_api.writes)
second_apply = apply_vocabulary_archive(db, 2, "#ds_vocabulary", reordered)
if second_apply.changed:
    raise RuntimeError("Stage 8 rewrote identical vocabulary HTML")
assert_equal(len(db.new_api.writes), write_count, "idempotent vocabulary writes")

empty_archive = render_vocabulary_archive({"entries": []})
cleared = apply_vocabulary_archive(db, 2, "#ds_vocabulary", empty_archive)
if not cleared.changed or db.new_api.values["#ds_vocabulary"][2] != "":
    raise RuntimeError("Stage 8 did not clear stale Calibre vocabulary HTML")


class FakePagedVocabularyApi(DeluxeSyncApi):
    def __init__(self):
        super().__init__("https://example.invalid", plugin_version="stage8-smoke")
        self.requests = []

    def _request_json(self, path, **kwargs):
        self.requests.append(path)
        query = parse_qs(urlsplit(path).query)
        offset = int(query.get("offset", ["0"])[0])
        if query.get("book") != ["Resolved Server Title"]:
            raise RuntimeError(f"Vocabulary request lost the exact book filter: {path}")
        if offset == 0:
            page = [
                {
                    "id": f"word-{index}",
                    "word": f"word-{index}",
                    "title": "Resolved Server Title",
                }
                for index in range(500)
            ]
            return {"entries": page, "total": 502, "limit": 500, "offset": 0}
        if offset == 500:
            return {
                "entries": [
                    {
                        "id": "last",
                        "word": "last-word",
                        "title": "Resolved Server Title",
                    },
                    {
                        "id": "unrelated",
                        "word": "wrong-book-word",
                        "title": "Another Book",
                    },
                ],
                "total": 502,
                "limit": 500,
                "offset": 500,
            }
        raise RuntimeError(f"Unexpected vocabulary offset: {offset}")


paged_api = FakePagedVocabularyApi()
paged = paged_api.get_vocabulary_for_title(
    "Resolved Server Title",
    {"auth_mode": "pairing", "session": "redacted"},
)
assert_equal(len(paged_api.requests), 2, "vocabulary pagination request count")
assert_equal(len(paged["entries"]), 501, "unrelated vocabulary exclusion")
if any(item.get("title") == "Another Book" for item in paged["entries"]):
    raise RuntimeError("Vocabulary from an unrelated book leaked through the API client")


class FakeBindingVocabularyApi(DeluxeSyncApi):
    def __init__(self):
        super().__init__("https://example.invalid", plugin_version="stage8-smoke")
        self.resolution_calls = []
        self.title_requests = []

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
        return {
            "kind": "logical",
            "logical_book_id": 8,
            "title": "Resolved Server Title",
            "members": [{"document": document}],
        }

    def get_vocabulary_for_title(self, title, profile):
        self.title_requests.append(title)
        return {"title": title, "entries": entries, "total": len(entries)}


binding_api = FakeBindingVocabularyApi()
profile = {"auth_mode": "pairing", "session": "redacted"}
enhanced_capabilities = {
    "capabilities": {
        "vocabulary_builder": True,
        "logical_library": True,
    }
}
resolved_payload = binding_api.get_vocabulary_for_binding(
    "linked-version.epub",
    "Stale Calibre Title",
    profile,
    capabilities_response=enhanced_capabilities,
    logical_book_id=999,
    library_response={"books": []},
    logical_cache={},
)
assert_equal(
    binding_api.resolution_calls,
    [("linked-version.epub", 999)],
    "matched-book title resolver call",
)
assert_equal(
    binding_api.title_requests,
    ["Resolved Server Title"],
    "resolved server title vocabulary lookup",
)
assert_equal(resolved_payload["title"], "Resolved Server Title", "resolved payload title")

try:
    binding_api.get_vocabulary_for_binding(
        "linked-version.epub",
        "Calibre Title",
        profile,
        capabilities_response={"capabilities": {"vocabulary_builder": False}},
    )
except CapabilityError:
    pass
else:
    raise RuntimeError("Vocabulary sync ignored the vocabulary_builder capability gate")


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
    set_column_mappings({"vocabulary": "#ds_vocabulary"})
    dialog = SyncSelectedBooksDialog(mapping_action, auto_sync=False)
    if not dialog.sync_button.isEnabled():
        raise RuntimeError("Vocabulary-only mapping did not enable Sync Selected Books")

    dialog._library_uuid = FakeDb.library_id
    fetch_result = SimpleNamespace(
        book=SimpleNamespace(book_id=2, title="Vocabulary Book"),
        snapshot=None,
        error="",
        archive=None,
        annotation_error="",
        vocabulary_archive=archive,
        vocabulary_error="",
    )
    dialog._sync_loaded(
        (
            dialog._generation,
            [fetch_result],
            [],
            {"vocabulary": "#ds_vocabulary"},
            None,
            False,
        )
    )
    if "1 updated" not in dialog.result_summary_label.text():
        raise RuntimeError("Vocabulary-only sync did not report an updated book")
    row_result = dialog.results_model.item(0, 1).text()
    if "2 words" not in row_result:
        raise RuntimeError("Vocabulary word count was not shown in Sync Selected Books")
    assert_equal(
        mapping_gui.current_db.new_api.values["#ds_vocabulary"][2],
        archive.html,
        "vocabulary-only dialog write",
    )
    assert_equal(
        mapping_gui.current_db.new_api.values["#ds_vocabulary"][9],
        "<p>other book</p>",
        "vocabulary-only unrelated row",
    )

    dialog._sync_loaded(
        (
            dialog._generation,
            [fetch_result],
            [],
            {"vocabulary": "#ds_vocabulary"},
            None,
            False,
        )
    )
    if "1 already current" not in dialog.result_summary_label.text():
        raise RuntimeError("Vocabulary-only rerun was not idempotent")
    dialog.close()
finally:
    set_column_mappings(original_mappings)
    mapping_gui.close()


print(
    "Stage 8 vocabulary smoke passed: exact-book filtering, pagination, resolved server title, "
    "duplicate normalization, deterministic escaped HTML, exact-row replacement/clearing, "
    "idempotent reruns, capability gating, unrelated-book exclusion, and vocabulary-only sync."
)
