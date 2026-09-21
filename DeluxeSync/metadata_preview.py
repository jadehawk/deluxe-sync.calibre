"""Pure Stage 5 metadata comparison helpers for Deluxe Sync."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from calibre_plugins.deluxe_sync.matching import normalize_identifier, normalize_text
from calibre_plugins.deluxe_sync.models import CalibreBook


POLICY_CALIBRE_WINS = "calibre_wins"
POLICY_SERVER_WINS = "server_wins"
POLICY_DO_NOT_SYNC = "do_not_sync"

VALID_POLICIES = frozenset(
    {
        POLICY_CALIBRE_WINS,
        POLICY_SERVER_WINS,
        POLICY_DO_NOT_SYNC,
    }
)

SUPPORTED_METADATA_FIELDS = (
    "cover",
    "rating",
    "review_note",
    "title",
    "authors",
    "isbn",
    "asin",
    "series",
    "series_index",
)

DEFAULT_METADATA_POLICIES = {
    "cover": POLICY_CALIBRE_WINS,
    "rating": POLICY_SERVER_WINS,
    "review_note": POLICY_SERVER_WINS,
    "title": POLICY_CALIBRE_WINS,
    "authors": POLICY_CALIBRE_WINS,
    "isbn": POLICY_DO_NOT_SYNC,
    "asin": POLICY_DO_NOT_SYNC,
    "series": POLICY_CALIBRE_WINS,
    "series_index": POLICY_CALIBRE_WINS,
}

ACTION_SAME = "same"
ACTION_CALIBRE_TO_SERVER = "calibre_to_server"
ACTION_SERVER_TO_CALIBRE = "server_to_calibre"
ACTION_DO_NOT_SYNC = "do_not_sync"
ACTION_KEEP_SERVER = "keep_server"
ACTION_KEEP_CALIBRE = "keep_calibre"


@dataclass(frozen=True)
class MetadataFieldPreview:
    """One metadata-field comparison and its proposed Stage 5 direction."""

    field: str
    calibre_value: Any
    server_value: Any
    policy: str
    action: str


def clean_metadata_policies(
    value: Mapping[str, Any] | None,
) -> dict[str, str]:
    """Return a complete, validated policy map with safe defaults."""

    source = value if isinstance(value, Mapping) else {}
    cleaned: dict[str, str] = {}
    for field in SUPPORTED_METADATA_FIELDS:
        policy = str(source.get(field) or "").strip()
        if policy not in VALID_POLICIES:
            policy = DEFAULT_METADATA_POLICIES[field]
        cleaned[field] = policy
    return cleaned


def calibre_metadata(book: CalibreBook) -> dict[str, Any]:
    """Project the supported metadata fields from one Calibre book."""

    return {
        "cover": book.cover_hash,
        "rating": book.rating,
        "review_note": book.review_note,
        "title": book.title,
        "authors": book.authors_text,
        "isbn": book.isbn,
        "asin": book.asin,
        "series": book.series,
        "series_index": book.series_index,
    }


def _clean_series_index(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _clean_rating(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        rating = float(value)
    except (TypeError, ValueError):
        return None
    if rating < 0.5 or rating > 5:
        return None
    doubled = rating * 2
    if abs(doubled - round(doubled)) > 1e-9:
        return None
    return round(doubled) / 2


def normalize_metadata_value(field: str, value: Any) -> Any:
    """Normalize only for equality checks; display keeps the original value."""

    if field == "series_index":
        return _clean_series_index(value)
    if field == "rating":
        return _clean_rating(value)
    if field == "review_note":
        if value is None:
            return None
        return str(value).strip()
    if field == "cover":
        return str(value or "").strip().lower()
    if field in {"isbn", "asin"}:
        return normalize_identifier(value)
    if field in {"title", "authors", "series"}:
        return normalize_text(value)
    return str(value or "").strip()


def metadata_value_is_empty(field: str, value: Any) -> bool:
    normalized = normalize_metadata_value(field, value)
    if field in {"series_index", "rating"}:
        return normalized is None
    return normalized == ""


def proposed_action(
    field: str,
    calibre_value: Any,
    server_value: Any,
    policy: str,
) -> str:
    """Resolve a read-only action while protecting non-empty metadata from erasure."""

    if normalize_metadata_value(field, calibre_value) == normalize_metadata_value(
        field, server_value
    ):
        return ACTION_SAME

    clean_policy = (
        policy if policy in VALID_POLICIES else DEFAULT_METADATA_POLICIES[field]
    )
    if clean_policy == POLICY_DO_NOT_SYNC:
        return ACTION_DO_NOT_SYNC

    calibre_empty = metadata_value_is_empty(field, calibre_value)
    server_empty = metadata_value_is_empty(field, server_value)

    if clean_policy == POLICY_CALIBRE_WINS:
        if calibre_empty and not server_empty:
            return ACTION_KEEP_SERVER
        return ACTION_CALIBRE_TO_SERVER

    if server_empty and not calibre_empty:
        return ACTION_KEEP_CALIBRE
    return ACTION_SERVER_TO_CALIBRE


def proposed_rating_action(
    calibre_value: Any,
    server_value: Any,
    policy: str,
    *,
    server_known: bool,
) -> str:
    """Resolve rating sync while distinguishing unknown from an explicit clear."""

    clean_policy = (
        policy if policy in VALID_POLICIES else DEFAULT_METADATA_POLICIES["rating"]
    )
    calibre_rating = normalize_metadata_value("rating", calibre_value)
    if not server_known:
        if calibre_rating is None:
            return ACTION_SAME
        if clean_policy == POLICY_CALIBRE_WINS:
            return ACTION_CALIBRE_TO_SERVER
        if clean_policy == POLICY_DO_NOT_SYNC:
            return ACTION_DO_NOT_SYNC
        return ACTION_KEEP_CALIBRE

    server_rating = normalize_metadata_value("rating", server_value)
    if calibre_rating == server_rating:
        return ACTION_SAME
    if clean_policy == POLICY_DO_NOT_SYNC:
        return ACTION_DO_NOT_SYNC
    if clean_policy == POLICY_CALIBRE_WINS:
        return ACTION_CALIBRE_TO_SERVER
    return ACTION_SERVER_TO_CALIBRE


def proposed_review_action(
    calibre_value: Any,
    server_value: Any,
    policy: str,
    *,
    server_known: bool,
) -> str:
    """Resolve private review sync; None means the Calibre review column is not mapped."""

    if calibre_value is None:
        return ACTION_DO_NOT_SYNC
    clean_policy = (
        policy if policy in VALID_POLICIES else DEFAULT_METADATA_POLICIES["review_note"]
    )
    calibre_review = normalize_metadata_value("review_note", calibre_value)
    if not server_known:
        if not calibre_review:
            return ACTION_SAME
        if clean_policy == POLICY_CALIBRE_WINS:
            return ACTION_CALIBRE_TO_SERVER
        if clean_policy == POLICY_DO_NOT_SYNC:
            return ACTION_DO_NOT_SYNC
        return ACTION_KEEP_CALIBRE

    server_review = normalize_metadata_value("review_note", server_value)
    if calibre_review == server_review:
        return ACTION_SAME
    if clean_policy == POLICY_DO_NOT_SYNC:
        return ACTION_DO_NOT_SYNC
    if clean_policy == POLICY_CALIBRE_WINS:
        return ACTION_CALIBRE_TO_SERVER
    return ACTION_SERVER_TO_CALIBRE


def build_metadata_preview(
    book: CalibreBook,
    server_metadata: Mapping[str, Any] | None,
    policies: Mapping[str, Any] | None,
) -> list[MetadataFieldPreview]:
    """Build all supported field comparisons for one linked Calibre book."""

    local = calibre_metadata(book)
    remote = server_metadata if isinstance(server_metadata, Mapping) else {}
    clean_policies = clean_metadata_policies(policies)
    previews: list[MetadataFieldPreview] = []

    for field in SUPPORTED_METADATA_FIELDS:
        server_value = remote.get(field)
        if field == "rating":
            action = proposed_rating_action(
                local.get(field),
                server_value,
                clean_policies[field],
                server_known=remote.get("rating_known") is True,
            )
        elif field == "review_note":
            action = proposed_review_action(
                local.get(field),
                server_value,
                clean_policies[field],
                server_known=remote.get("review_known") is True,
            )
        else:
            action = proposed_action(
                field,
                local.get(field),
                server_value,
                clean_policies[field],
            )
        previews.append(
            MetadataFieldPreview(
                field=field,
                calibre_value=local.get(field),
                server_value=server_value,
                policy=clean_policies[field],
                action=action,
            )
        )

    return previews
