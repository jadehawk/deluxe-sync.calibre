"""Pure helpers for safe Calibre-origin document registration."""

from __future__ import annotations

from math import isfinite

from calibre_plugins.deluxe_sync.models import CalibreBook


def calibre_document_id(book: CalibreBook) -> str:
    """Return the stable raw server identity for one Calibre book."""

    library_uuid = str(book.library_uuid or "").strip()
    book_uuid = str(book.book_uuid or "").strip()
    if (
        not library_uuid
        or not book_uuid
        or ":" in library_uuid
        or ":" in book_uuid
        or len(library_uuid) > 128
        or len(book_uuid) > 128
    ):
        raise ValueError(
            "Calibre library/book UUIDs are required for server registration."
        )
    return f"calibre:{library_uuid}:{book_uuid}"


def registration_metadata(book: CalibreBook) -> dict[str, object]:
    """Return only non-empty metadata fields safe for initial registration."""

    metadata: dict[str, object] = {}

    filenames = sorted(
        {str(value or "").strip() for value in book.filenames if str(value or "").strip()},
        key=str.casefold,
    )
    if filenames:
        metadata["filename"] = filenames[0]

    for field, value in (
        ("title", book.title),
        ("authors", book.authors_text),
        ("isbn", book.isbn),
        ("asin", book.asin),
        ("series", book.series),
    ):
        clean = str(value or "").strip()
        if clean:
            metadata[field] = clean

    if (
        book.series_index is not None
        and not isinstance(book.series_index, bool)
        and isfinite(float(book.series_index))
    ):
        metadata["series_index"] = float(book.series_index)

    return metadata
