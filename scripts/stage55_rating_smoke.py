"""Deterministic Stage 5.5 rating round-trip smoke test inside Calibre."""

from __future__ import annotations

from types import SimpleNamespace

from calibre.customize.ui import find_plugin
from qt.core import QApplication, QWidget


app = QApplication.instance() or QApplication([])


def assert_equal(actual, expected, label):
    if actual != expected:
        raise RuntimeError(f"{label}: expected {expected!r}, got {actual!r}")


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
from calibre_plugins.deluxe_sync.dialogs.sync_preview import (  # noqa: E402
    SyncPreviewDialog,
    _FetchResult,
    _SyncPlan,
)
from calibre_plugins.deluxe_sync.metadata_preview import (  # noqa: E402
    ACTION_CALIBRE_TO_SERVER,
    ACTION_KEEP_CALIBRE,
    ACTION_SAME,
    ACTION_SERVER_TO_CALIBRE,
    POLICY_CALIBRE_WINS,
    POLICY_SERVER_WINS,
    build_metadata_preview,
    clean_metadata_policies,
    proposed_rating_action,
)
from calibre_plugins.deluxe_sync.metadata_sync import build_metadata_patch  # noqa: E402
from calibre_plugins.deluxe_sync.models import (  # noqa: E402
    CalibreBook,
    calibre_book_from_db,
)


# ---------------------------------------------------------------------------
# Native Calibre rating conversion: integer 0-10 units <-> half stars.
# ---------------------------------------------------------------------------


class RatingFields:
    def __init__(self, raw_rating):
        self.raw_rating = raw_rating

    def field_for(self, name, _book_id, default_value=None):
        values = {
            "uuid": "rating-conversion-book",
            "title": "Rating Conversion",
            "authors": ("Reader",),
            "identifiers": {},
            "series": "",
            "series_index": None,
            "rating": self.raw_rating,
        }
        return values.get(name, default_value)

    def formats(self, _book_id, verify_formats=False):
        return ()

    def cover(self, _book_id):
        return None


book_45 = calibre_book_from_db(
    SimpleNamespace(new_api=RatingFields(9)),
    "rating-library",
    1,
    include_cover_hash=False,
)
assert_equal(book_45.rating, 4.5, "Calibre raw 9 -> 4.5 stars")

book_unrated = calibre_book_from_db(
    SimpleNamespace(new_api=RatingFields(0)),
    "rating-library",
    1,
    include_cover_hash=False,
)
assert_equal(book_unrated.rating, None, "Calibre raw 0 -> unrated")


# ---------------------------------------------------------------------------
# Pure rating policy semantics, including unknown vs explicit clear.
# ---------------------------------------------------------------------------

defaults = clean_metadata_policies({})
assert_equal(defaults["rating"], POLICY_SERVER_WINS, "Rating default policy")

assert_equal(
    proposed_rating_action(4.0, 4.5, POLICY_SERVER_WINS, server_known=True),
    ACTION_SERVER_TO_CALIBRE,
    "Server-wins half-star difference",
)
assert_equal(
    proposed_rating_action(4.5, 4.5, POLICY_SERVER_WINS, server_known=True),
    ACTION_SAME,
    "Matching half-star rating",
)
assert_equal(
    proposed_rating_action(4.5, None, POLICY_SERVER_WINS, server_known=False),
    ACTION_KEEP_CALIBRE,
    "Unknown server rating must not clear Calibre",
)
assert_equal(
    proposed_rating_action(4.5, None, POLICY_SERVER_WINS, server_known=True),
    ACTION_SERVER_TO_CALIBRE,
    "Known server clear must clear Calibre",
)
assert_equal(
    proposed_rating_action(None, 4.5, POLICY_CALIBRE_WINS, server_known=True),
    ACTION_CALIBRE_TO_SERVER,
    "Calibre clear must be explicit under Calibre-wins",
)

preview = build_metadata_preview(
    CalibreBook(
        library_uuid="rating-library",
        book_uuid="preview-book",
        book_id=2,
        title="Preview",
        rating=4.0,
    ),
    {"rating_known": True, "rating": 4.5},
    {},
)
rating_preview = next(item for item in preview if item.field == "rating")
assert_equal(rating_preview.action, ACTION_SERVER_TO_CALIBRE, "Preview rating direction")

rating_patch = build_metadata_patch(
    CalibreBook(
        library_uuid="rating-library",
        book_uuid="patch-book",
        book_id=3,
        title="Patch",
        rating=4.5,
        review_note="Calibre private review",
    ),
    {
        "title": "Patch",
        "rating_known": True,
        "rating": 3.5,
        "review_known": True,
        "review_note": "Server private review",
    },
    {"rating": POLICY_CALIBRE_WINS, "review_note": POLICY_CALIBRE_WINS},
)
if "rating" in rating_patch or "review_note" in rating_patch:
    raise RuntimeError("Book feedback leaked into the ordinary metadata PATCH")


# ---------------------------------------------------------------------------
# Dedicated feedback API contract. Calibre may write rating and the mapped private review.
# ---------------------------------------------------------------------------

api = DeluxeSyncApi("https://example.invalid", plugin_version="0.1.0.0")
request_calls = []


def fake_request(path, **kwargs):
    request_calls.append((path, dict(kwargs)))
    if kwargs.get("method") == "PATCH":
        rating = kwargs["payload"].get("rating")
        return {
            "feedback": {
                "rating": rating,
                "rating_known": True,
                "review_note": "private server note",
                "review_known": True,
            }
        }
    return {
        "feedback": {
            "rating": 4.5,
            "rating_known": True,
            "review_note": "private server note",
            "review_known": True,
        }
    }


api._request_json = fake_request
capabilities = {
    "capabilities": {
        "book_feedback": True,
        "book_feedback_version": 1,
    }
}
read_feedback = api.get_document_feedback(
    "raw/doc",
    {"auth_mode": "pairing"},
    capabilities_response=capabilities,
)
assert_equal(read_feedback["feedback"]["rating"], 4.5, "Feedback GET rating")
path, kwargs = request_calls[-1]
assert_equal(path, "/api/v1/documents/raw%2Fdoc/feedback", "Feedback GET path")
assert_equal(kwargs.get("method", "GET"), "GET", "Feedback GET method")

write_feedback = api.patch_document_feedback(
    "raw/doc",
    {"rating": 4.5},
    {"auth_mode": "pairing"},
    capabilities_response=capabilities,
)
assert_equal(write_feedback["feedback"]["rating"], 4.5, "Feedback PATCH rating")
path, kwargs = request_calls[-1]
assert_equal(path, "/api/v1/documents/raw%2Fdoc/feedback", "Feedback PATCH path")
assert_equal(kwargs.get("method"), "PATCH", "Feedback PATCH method")
assert_equal(
    kwargs.get("payload"),
    {"rating": 4.5, "source": "calibre"},
    "Feedback PATCH payload",
)

api.patch_document_feedback(
    "raw/doc",
    {"review_note": "private Calibre review"},
    {"auth_mode": "pairing"},
    capabilities_response=capabilities,
)
path, kwargs = request_calls[-1]
assert_equal(path, "/api/v1/documents/raw%2Fdoc/feedback", "Review PATCH path")
assert_equal(kwargs.get("method"), "PATCH", "Review PATCH method")
assert_equal(
    kwargs.get("payload"),
    {"review_note": "private Calibre review", "source": "calibre"},
    "Review PATCH payload",
)

try:
    api.get_document_feedback(
        "raw/doc",
        {"auth_mode": "pairing"},
        capabilities_response={"capabilities": {"book_feedback": False}},
    )
except CapabilityError:
    pass
else:
    raise RuntimeError("Feedback GET was not capability-gated")


# ---------------------------------------------------------------------------
# Real SyncPreviewDialog worker/UI paths with a fake enhanced server.
# ---------------------------------------------------------------------------


class FakeCalibreApi:
    def __init__(self):
        self.ratings = {}
        self.reviews = {}

    def set_field(self, name, values, **_kwargs):
        if name == "rating":
            self.ratings.update(values)
            return
        if name == "#ds_review":
            self.reviews.update(values)
            return
        raise RuntimeError(f"Unexpected Calibre field write: {name}")

    def field_for(self, name, book_id, default_value=None):
        if name == "rating":
            return self.ratings.get(book_id, default_value)
        if name == "#ds_review":
            return self.reviews.get(book_id, default_value)
        return default_value

    def cover(self, _book_id):
        return None

    def set_cover(self, _values):
        raise RuntimeError("Cover write was not expected in rating smoke test")


class FakeGui(QWidget):
    def __init__(self):
        super().__init__()
        self.rating_api = FakeCalibreApi()
        self.current_db = SimpleNamespace(
            library_id="stage55-rating-library",
            new_api=self.rating_api,
        )


class FakeEnhancedApi(DeluxeSyncApi):
    server_ratings = {
        "calibre-to-server": 4.0,
        "server-to-calibre": 4.5,
        "server-clear": None,
    }
    server_reviews = {
        "calibre-to-server": "older server review",
        "server-to-calibre": "server private review",
        "server-clear": None,
    }
    patches = []

    def discover_capabilities(self, _profile):
        return {
            "capabilities": {
                "book_feedback": True,
                "book_feedback_version": 1,
                "document_metadata": True,
                "document_metadata_write": False,
                "document_cover_sync": False,
            }
        }

    def get_document_metadata(
        self,
        document,
        _profile,
        *,
        capabilities_response=None,
    ):
        return {
            "document": document,
            "metadata": {
                "filename": f"{document}.epub",
                "title": document,
                "authors": "Reader",
                "isbn": None,
                "asin": None,
                "series": None,
                "series_index": None,
            },
            "cover": {"hash": ""},
        }

    def get_document_feedback(
        self,
        document,
        _profile,
        *,
        capabilities_response=None,
    ):
        return {
            "feedback": {
                "rating": self.server_ratings.get(document),
                "rating_known": True,
                "review_note": self.server_reviews.get(document),
                "review_known": True,
            }
        }

    def patch_document_feedback(
        self,
        document,
        feedback,
        _profile,
        *,
        capabilities_response=None,
    ):
        unexpected = set(feedback) - {"rating", "review_note"}
        if unexpected:
            raise RuntimeError(f"Unexpected feedback fields: {sorted(unexpected)}")
        if "rating" in feedback:
            self.server_ratings[document] = feedback["rating"]
        if "review_note" in feedback:
            self.server_reviews[document] = feedback["review_note"]
        self.patches.append((document, dict(feedback)))
        return self.get_document_feedback(
            document,
            {},
            capabilities_response=capabilities_response,
        )


gui = FakeGui()
action = SimpleNamespace(
    gui=gui,
    interface_action_base_plugin=SimpleNamespace(version_string="0.1.0.0"),
)

calibre_to_server_book = CalibreBook(
    library_uuid="stage55-rating-library",
    book_uuid="calibre-to-server-book",
    book_id=11,
    title="calibre-to-server",
    rating=4.5,
    review_note="Calibre private review",
)
server_to_calibre_book = CalibreBook(
    library_uuid="stage55-rating-library",
    book_uuid="server-to-calibre-book",
    book_id=12,
    title="server-to-calibre",
    rating=3.0,
    review_note="older Calibre review",
)
server_clear_book = CalibreBook(
    library_uuid="stage55-rating-library",
    book_uuid="server-clear-book",
    book_id=13,
    title="server-clear",
    rating=2.5,
    review_note="clear this review",
)

dialog = SyncPreviewDialog(
    action,
    auto_refresh=False,
    books=[
        calibre_to_server_book,
        server_to_calibre_book,
        server_clear_book,
    ],
)
dialog._library_uuid = "stage55-rating-library"
dialog._profile_id = "stage55-profile"
dialog._results = [
    _FetchResult(
        book=calibre_to_server_book,
        metadata={
            "rating_known": True,
            "rating": 4.0,
            "review_known": True,
            "review_note": "older server review",
        },
        document="calibre-to-server",
    ),
    _FetchResult(
        book=server_to_calibre_book,
        metadata={
            "rating_known": True,
            "rating": 4.5,
            "review_known": True,
            "review_note": "server private review",
        },
        document="server-to-calibre",
    ),
    _FetchResult(
        book=server_clear_book,
        metadata={
            "rating_known": True,
            "rating": None,
            "review_known": True,
            "review_note": None,
        },
        document="server-clear",
    ),
]

plans = [
    _SyncPlan(
        preview=dialog._results[0],
        metadata_patch={},
        rating_action=ACTION_CALIBRE_TO_SERVER,
        review_action=ACTION_CALIBRE_TO_SERVER,
    ),
    _SyncPlan(
        preview=dialog._results[1],
        metadata_patch={},
        rating_action=ACTION_SERVER_TO_CALIBRE,
        review_action=ACTION_SERVER_TO_CALIBRE,
    ),
    _SyncPlan(
        preview=dialog._results[2],
        metadata_patch={},
        rating_action=ACTION_SERVER_TO_CALIBRE,
        review_action=ACTION_SERVER_TO_CALIBRE,
    ),
]

original_api = preview_dialog_module.DeluxeSyncApi
original_mappings = preview_dialog_module.get_column_mappings
preview_dialog_module.DeluxeSyncApi = FakeEnhancedApi
preview_dialog_module.get_column_mappings = lambda: {"review_note": "#ds_review"}
try:
    dialog._sync_worker(
        dialog._generation,
        plans,
        "https://example.invalid",
        {"auth_mode": "pairing"},
        False,
    )
    app.processEvents()
finally:
    preview_dialog_module.DeluxeSyncApi = original_api
    preview_dialog_module.get_column_mappings = original_mappings

assert_equal(
    FakeEnhancedApi.server_ratings["calibre-to-server"],
    4.5,
    "Calibre -> server half-star result",
)
assert_equal(
    FakeEnhancedApi.server_reviews["calibre-to-server"],
    "Calibre private review",
    "Calibre -> server private review result",
)
assert_equal(
    FakeEnhancedApi.patches,
    [
        (
            "calibre-to-server",
            {"rating": 4.5, "review_note": "Calibre private review"},
        )
    ],
    "Calibre -> server used combined rating/review feedback PATCH",
)
assert_equal(gui.rating_api.ratings.get(12), 9, "Server 4.5 stars -> Calibre raw 9")
assert_equal(gui.rating_api.ratings.get(13), 0, "Server explicit clear -> Calibre raw 0")
assert_equal(
    gui.rating_api.reviews.get(12),
    "server private review",
    "Server review -> mapped Calibre review column",
)
assert_equal(
    gui.rating_api.reviews.get(13),
    "",
    "Server review clear -> empty mapped Calibre review column",
)

merged_by_uuid = {item.book.book_uuid: item for item in dialog._results}
assert_equal(
    merged_by_uuid["server-to-calibre-book"].book.rating,
    4.5,
    "Server -> Calibre model rating",
)
assert_equal(
    merged_by_uuid["server-clear-book"].book.rating,
    None,
    "Server clear -> Calibre model unrated",
)
assert_equal(
    merged_by_uuid["server-to-calibre-book"].book.review_note,
    "server private review",
    "Server -> Calibre model review",
)
assert_equal(
    merged_by_uuid["server-clear-book"].book.review_note,
    "",
    "Server clear -> Calibre model review clear",
)

print(
    "Stage 5.5 feedback smoke passed: half-star native rating conversion, mapped "
    "private reviews, Calibre -> server writes, server -> Calibre writes, and clears."
)
