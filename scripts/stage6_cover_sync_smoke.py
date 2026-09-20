"""Deterministic bidirectional cover-sync smoke test inside Calibre."""

from __future__ import annotations

import hashlib
import json
from types import SimpleNamespace
from time import monotonic, sleep

from calibre.customize.ui import find_plugin
from qt.core import QApplication, QWidget


app = QApplication.instance() or QApplication([])


def assert_equal(actual, expected, label):
    if actual != expected:
        raise RuntimeError(f"{label}: expected {expected!r}, got {actual!r}")


def wait_until(predicate, label, timeout=4.0):
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
    POLICY_SERVER_WINS,
)
from calibre_plugins.deluxe_sync.models import CalibreBook  # noqa: E402
from calibre_plugins.deluxe_sync.settings import (  # noqa: E402
    get_metadata_policies,
    set_metadata_policies,
)


LOCAL_COVER = b"\xff\xd8\xff\xe0LOCAL-COVER-BYTES"
REMOTE_COVER = b"\xff\xd8\xff\xe0REMOTE-COVER-BYTES"
LOCAL_HASH = hashlib.sha256(LOCAL_COVER).hexdigest()
REMOTE_HASH = hashlib.sha256(REMOTE_COVER).hexdigest()


# ---------------------------------------------------------------------------
# Raw API contract: binary upload/download stays out of metadata PATCH.
# ---------------------------------------------------------------------------

api = DeluxeSyncApi("https://example.invalid", plugin_version="0.1.0.0")
binary_calls = []


def fake_bytes(path, **kwargs):
    binary_calls.append((path, dict(kwargs)))
    if path.startswith("/covers/upload?"):
        return json.dumps({"cover_hash": LOCAL_HASH}).encode("utf-8")
    if path == f"/covers/images/{REMOTE_HASH}":
        return REMOTE_COVER
    raise RuntimeError(f"Unexpected binary path {path}")


api._request_bytes = fake_bytes
uploaded = api.upload_document_cover(
    "raw/doc",
    LOCAL_COVER,
    {"auth_mode": "pairing", "session": "redacted"},
    capabilities_response={"capabilities": {"document_cover_sync": True}},
)
assert_equal(uploaded["cover_hash"], LOCAL_HASH, "binary upload hash")
path, kwargs = binary_calls[0]
assert_equal(path, "/covers/upload?document=raw%2Fdoc", "binary upload path")
assert_equal(kwargs.get("method"), "POST", "binary upload method")
assert_equal(kwargs.get("data"), LOCAL_COVER, "binary upload bytes")
assert_equal(kwargs.get("content_type"), "image/jpeg", "binary upload content type")
assert_equal(api.download_cover(REMOTE_HASH), REMOTE_COVER, "verified cover download")


# ---------------------------------------------------------------------------
# Real Qt dialog with cover-only differences in both directions.
# ---------------------------------------------------------------------------


class FakeCalibreApi:
    def __init__(self):
        self.cover_bytes = LOCAL_COVER
        self.set_calls = []

    def cover(self, book_id):
        if book_id != 1:
            raise RuntimeError("Unexpected Calibre book id")
        return self.cover_bytes

    def set_cover(self, mapping):
        self.set_calls.append(dict(mapping))
        self.cover_bytes = bytes(mapping[1])


class FakeGui(QWidget):
    def __init__(self):
        super().__init__()
        self.calibre_api = FakeCalibreApi()
        self.current_db = SimpleNamespace(
            library_id="stage6-cover-library",
            new_api=self.calibre_api,
        )


class FakeCoverApi:
    cover_supported = True
    server_cover = REMOTE_COVER
    upload_calls = []
    download_calls = []
    patch_calls = []
    progress_calls = []

    def __init__(self, _server_url, *, plugin_version, timeout=10):
        self.plugin_version = plugin_version
        self.timeout = timeout

    @classmethod
    def server_hash(cls):
        return hashlib.sha256(cls.server_cover).hexdigest()

    def discover_capabilities(self, _profile):
        return {
            "server_type": "enhanced",
            "capabilities": {
                "document_metadata": True,
                "document_metadata_write": False,
                "document_cover_sync": type(self).cover_supported,
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
            raise RuntimeError("Cover preview failed to reuse capabilities")
        return {
            "document": document,
            "metadata": {
                "filename": None,
                "title": "Cover Test",
                "authors": "Cover Author",
                "isbn": None,
                "asin": None,
                "series": None,
                "series_index": None,
            },
            "cover": {
                "hash": type(self).server_hash(),
                "source": "custom",
                "url": f"/covers/images/{type(self).server_hash()}",
            },
            "updated_at": 1,
        }

    def patch_document_metadata(self, *args, **kwargs):
        type(self).patch_calls.append((args, kwargs))
        raise RuntimeError("Cover-only sync attempted metadata PATCH")

    def upload_document_cover(
        self,
        document,
        cover_bytes,
        _profile,
        *,
        capabilities_response=None,
    ):
        if not capabilities_response["capabilities"].get("document_cover_sync"):
            raise RuntimeError("Cover upload attempted without capability")
        type(self).upload_calls.append((document, bytes(cover_bytes)))
        type(self).server_cover = bytes(cover_bytes)
        return {"cover_hash": type(self).server_hash()}

    def download_cover(self, cover_hash):
        type(self).download_calls.append(cover_hash)
        if cover_hash != type(self).server_hash():
            raise RuntimeError("Dialog requested the wrong server cover")
        return bytes(type(self).server_cover)

    def get_effective_progress(self, *_args, **_kwargs):
        type(self).progress_calls.append("effective")
        raise RuntimeError("Cover sync called progress reader")

    def get_kosync_progress(self, *_args, **_kwargs):
        type(self).progress_calls.append("kosync-get")
        raise RuntimeError("Cover sync called progress reader")

    def put_kosync_progress(self, *_args, **_kwargs):
        type(self).progress_calls.append("kosync-put")
        raise RuntimeError("Cover sync called progress writer")


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


def selected_book():
    data = gui.calibre_api.cover(1)
    return CalibreBook(
        library_uuid="stage6-cover-library",
        book_uuid="cover-book",
        book_id=1,
        title="Cover Test",
        authors=("Cover Author",),
        cover_hash=hashlib.sha256(bytes(data)).hexdigest() if data else "",
    )


dialog = None
try:
    set_metadata_policies({})
    FakeCoverApi.cover_supported = True
    FakeCoverApi.server_cover = REMOTE_COVER
    FakeCoverApi.upload_calls = []
    FakeCoverApi.download_calls = []
    FakeCoverApi.patch_calls = []
    FakeCoverApi.progress_calls = []

    preview_dialog_module.selected_calibre_books = lambda _gui: [selected_book()]
    preview_dialog_module.get_active_server_profile = lambda: {
        "server_url": "https://example.invalid",
        "auth_mode": "pairing",
        "session": "redacted",
    }
    preview_dialog_module.get_active_server_profile_id = lambda: "primary"
    preview_dialog_module.get_library_bindings = lambda _library, _profile: {
        "cover-book": {
            "server_document": "raw-cover",
            "enrolled": True,
        }
    }
    preview_dialog_module.DeluxeSyncApi = FakeCoverApi

    dialog = SyncPreviewDialog(action, auto_refresh=False)
    dialog.refresh_preview()
    wait_until(lambda: not dialog._loading, "Calibre-wins cover preview")

    if not dialog.sync_metadata_button.isEnabled():
        raise RuntimeError("Cover-only Calibre-wins difference did not enable sync")
    cover_rows = [
        [
            dialog.results_model.item(row, column).text()
            for column in range(dialog.results_model.columnCount())
        ]
        for row in range(dialog.results_model.rowCount())
        if dialog.results_model.item(row, 0).text() == "Cover"
    ]
    assert_equal(len(cover_rows), 1, "cover preview row count")
    assert_equal(cover_rows[0][1], "Cover available", "Calibre cover display")
    assert_equal(cover_rows[0][2], "Cover available", "server cover display")
    assert_equal(cover_rows[0][3], "Calibre → Server", "default cover direction")

    dialog.sync_metadata(show_completion=False)
    wait_until(lambda: not dialog._loading, "Calibre-wins cover upload")
    assert_equal(
        FakeCoverApi.upload_calls,
        [("raw-cover", LOCAL_COVER)],
        "Calibre-wins cover upload",
    )
    assert_equal(FakeCoverApi.patch_calls, [], "cover-only metadata PATCH calls")
    assert_equal(FakeCoverApi.progress_calls, [], "cover-only progress calls")
    assert_equal(FakeCoverApi.server_hash(), LOCAL_HASH, "server cover after Calibre wins")

    rows = [
        [
            dialog.results_model.item(row, column).text()
            for column in range(dialog.results_model.columnCount())
        ]
        for row in range(dialog.results_model.rowCount())
    ]
    cover_row = next(row for row in rows if row[0] == "Cover")
    assert_equal(cover_row[3], "Already matches", "Calibre-wins verified cover")

    # Server wins with a different remote cover must replace the actual Calibre cover.
    FakeCoverApi.server_cover = REMOTE_COVER
    cover_combo = dialog._policy_boxes["cover"]
    server_wins_index = cover_combo.findData(POLICY_SERVER_WINS)
    if server_wins_index < 0:
        raise RuntimeError("Server-wins cover policy option is missing")
    cover_combo.setCurrentIndex(server_wins_index)
    app.processEvents()
    dialog.refresh_preview()
    wait_until(lambda: not dialog._loading, "Server-wins cover preview")

    if not dialog.sync_metadata_button.isEnabled():
        raise RuntimeError("Cover-only Server-wins difference did not enable sync")
    dialog.sync_metadata(show_completion=False)
    wait_until(lambda: not dialog._loading, "Server-wins cover download/apply")

    assert_equal(FakeCoverApi.download_calls, [REMOTE_HASH], "server cover download")
    assert_equal(gui.calibre_api.cover_bytes, REMOTE_COVER, "Calibre cover after Server wins")
    assert_equal(len(gui.calibre_api.set_calls), 1, "Calibre set_cover call count")
    assert_equal(FakeCoverApi.patch_calls, [], "Server-wins metadata PATCH calls")
    assert_equal(FakeCoverApi.progress_calls, [], "Server-wins progress calls")

    rows = [
        [
            dialog.results_model.item(row, column).text()
            for column in range(dialog.results_model.columnCount())
        ]
        for row in range(dialog.results_model.rowCount())
    ]
    cover_row = next(row for row in rows if row[0] == "Cover")
    assert_equal(cover_row[3], "Already matches", "Server-wins verified cover")

    # Removing the capability leaves cover comparison visible but non-actionable.
    FakeCoverApi.cover_supported = False
    FakeCoverApi.server_cover = LOCAL_COVER
    dialog.refresh_preview()
    wait_until(lambda: not dialog._loading, "cover-disabled preview")
    assert_equal(
        dialog.sync_metadata_button.isEnabled(),
        False,
        "cover-disabled sync button",
    )
finally:
    if dialog is not None:
        dialog.close()
    preview_dialog_module.selected_calibre_books = original_selected
    preview_dialog_module.get_active_server_profile = original_profile
    preview_dialog_module.get_active_server_profile_id = original_profile_id
    preview_dialog_module.get_library_bindings = original_bindings
    preview_dialog_module.DeluxeSyncApi = original_api
    set_metadata_policies(original_policies)
    gui.close()


print(
    "Stage 6 cover sync smoke passed: binary API contract, cover-only button gating, "
    "Calibre → Server upload, Server → Calibre set_cover, SHA-based verification, "
    "capability gating, and zero progress/metadata-PATCH calls."
)
