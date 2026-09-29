"""Metadata-only write helpers for Deluxe Sync Stage 6."""

from __future__ import annotations

from typing import Any, Mapping

from calibre_plugins.deluxe_sync.metadata_preview import (
    ACTION_CALIBRE_TO_SERVER,
    POLICY_DO_NOT_SYNC,
    build_metadata_preview,
    normalize_metadata_value,
)
from calibre_plugins.deluxe_sync.models import CalibreBook


def build_metadata_patch(
    book: CalibreBook,
    server_metadata: Mapping[str, Any] | None,
    policies: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Return only fields explicitly approved for Calibre-to-server writes."""

    patch = {
        preview.field: preview.calibre_value
        for preview in build_metadata_preview(book, server_metadata, policies)
        if preview.action == ACTION_CALIBRE_TO_SERVER and preview.field not in {"cover", "rating", "review_note"}
    }
    remote = server_metadata if isinstance(server_metadata, Mapping) else {}
    local_series = normalize_metadata_value("series", book.series)
    remote_series = normalize_metadata_value("series", remote.get("series"))
    remote_index = normalize_metadata_value("series_index", remote.get("series_index"))
    series_index_policy = str((policies or {}).get("series_index") or "").strip()
    if (
        not local_series
        and not remote_series
        and remote_index is not None
        and series_index_policy != POLICY_DO_NOT_SYNC
    ):
        patch["series_index"] = None
    return patch


def metadata_patch_mismatches(
    patch: Mapping[str, Any],
    server_metadata: Mapping[str, Any] | None,
) -> tuple[str, ...]:
    """Return patched fields whose re-read server values do not match."""

    remote = server_metadata if isinstance(server_metadata, Mapping) else {}
    return tuple(
        field
        for field, expected in patch.items()
        if normalize_metadata_value(field, remote.get(field))
        != normalize_metadata_value(field, expected)
    )
