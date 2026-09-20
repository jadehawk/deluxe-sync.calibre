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
    "title",
    "authors",
    "isbn",
    "asin",
    "series",
    "series_index",
)

DEFAULT_METADATA_POLICIES = {
    "cover": POLICY_CALIBRE_WINS,
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


def normalize_metadata_value(field: str, value: Any) -> Any:
    """Normalize only for equality checks; display keeps the original value."""

    if field == "series_index":
        return _clean_series_index(value)
    if field == "cover":
        return str(value or "").strip().lower()
    if field in {"isbn", "asin"}:
        return normalize_identifier(value)
    if field in {"title", "authors", "series"}:
        return normalize_text(value)
    return str(value or "").strip()


def metadata_value_is_empty(field: str, value: Any) -> bool:
    normalized = normalize_metadata_value(field, value)
    if field == "series_index":
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


def build_metadata_preview(
    book: CalibreBook,
    server_metadata: Mapping[str, Any] | None,
    policies: Mapping[str, Any] | None,
) -> list[MetadataFieldPreview]:
    """Build all supported field comparisons for one linked Calibre book."""

    local = calibre_metadata(book)
    remote = server_metadata if isinstance(server_metadata, Mapping) else {}
    clean_policies = clean_metadata_policies(policies)

    return [
        MetadataFieldPreview(
            field=field,
            calibre_value=local.get(field),
            server_value=remote.get(field),
            policy=clean_policies[field],
            action=proposed_action(
                field,
                local.get(field),
                remote.get(field),
                clean_policies[field],
            ),
        )
        for field in SUPPORTED_METADATA_FIELDS
    ]
