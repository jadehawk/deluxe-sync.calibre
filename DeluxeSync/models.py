"""Local and remote book models used by Deluxe Sync matching."""

from __future__ import annotations

import hashlib

from dataclasses import dataclass, field
from os.path import basename
from typing import Any


@dataclass(frozen=True)
class CalibreBook:
    """Stable local identity plus the metadata useful for read-only matching."""

    library_uuid: str
    book_uuid: str
    book_id: int
    title: str
    authors: tuple[str, ...] = ()
    identifiers: dict[str, str] = field(default_factory=dict)
    series: str = ""
    series_index: float | None = None
    rating: float | None = None
    review_note: str | None = None
    filenames: tuple[str, ...] = ()
    cover_hash: str = ""

    @property
    def isbn(self) -> str:
        return str(self.identifiers.get("isbn") or "").strip()

    @property
    def asin(self) -> str:
        for key in ("asin", "amazon", "mobi-asin", "mobi_asin"):
            value = str(self.identifiers.get(key) or "").strip()
            if value:
                return value
        return ""

    @property
    def authors_text(self) -> str:
        return ", ".join(self.authors)


@dataclass(frozen=True)
class RemoteRecord:
    """One durable remote raw document with optional logical-book membership."""

    document: str
    title: str = ""
    authors: str = ""
    filename: str = ""
    isbn: str = ""
    asin: str = ""
    series: str = ""
    series_index: float | None = None
    logical_book_id: int | None = None
    logical_title: str = ""
    logical_authors: str = ""

    @property
    def display_title(self) -> str:
        return self.logical_title or self.title or self.filename or self.document

    @property
    def display_authors(self) -> str:
        return self.logical_authors or self.authors


@dataclass(frozen=True)
class MatchCandidate:
    """A remote record together with the reason it matched."""

    remote: RemoteRecord
    reason: str
    tier: int
    automatic: bool


@dataclass
class MatchResult:
    """Read-only matching result for one selected Calibre book."""

    local: CalibreBook
    state: str
    candidates: list[MatchCandidate] = field(default_factory=list)
    binding: dict[str, Any] | None = None
    binding_changed: bool = False

    @property
    def primary_candidate(self) -> MatchCandidate | None:
        return self.candidates[0] if self.candidates else None


def _clean_authors(value: Any) -> tuple[str, ...]:
    if isinstance(value, (list, tuple)):
        return tuple(str(item).strip() for item in value if str(item).strip())
    text = str(value or "").strip()
    if not text:
        return ()
    return (text,)


def _clean_identifiers(value: Any) -> dict[str, str]:
    if not isinstance(value, dict):
        return {}
    return {
        str(key).strip().lower(): str(identifier or "").strip()
        for key, identifier in value.items()
        if str(key).strip() and str(identifier or "").strip()
    }


def _series_index(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _rating(value: Any) -> float | None:
    """Convert Calibre's native 0-10 rating units to 0.5-5.0 stars."""

    if value is None or value == "":
        return None
    try:
        raw = float(value)
    except (TypeError, ValueError):
        return None
    if raw == 0:
        return None
    if raw < 1 or raw > 10 or raw != int(raw):
        return None
    return raw / 2


def calibre_book_from_db(
    db: Any,
    library_uuid: str,
    book_id: int,
    *,
    include_cover_hash: bool = True,
) -> CalibreBook:
    """Build a CalibreBook from Calibre's stable database API."""

    api = getattr(db, "new_api", None)
    if api is None:
        raise RuntimeError("The active Calibre library does not expose the database API.")

    def field_for(name: str, default: Any = None) -> Any:
        try:
            return api.field_for(name, book_id, default_value=default)
        except (KeyError, TypeError, ValueError):
            return default

    book_uuid = str(field_for("uuid", "") or "").strip()
    if not book_uuid:
        raise RuntimeError(f"Calibre book {book_id} does not have a stable UUID.")

    formats: tuple[str, ...] = ()
    try:
        available = api.formats(book_id, verify_formats=False) or ()
    except Exception:
        available = ()

    filenames: list[str] = []
    for fmt in available:
        try:
            path = api.format_abspath(book_id, fmt)
        except Exception:
            path = None
        if path:
            name = basename(str(path))
            if name and name not in filenames:
                filenames.append(name)

    cover_hash = ""
    try:
        cover_data = api.cover(book_id) if include_cover_hash else None
    except Exception:
        cover_data = None
    if cover_data:
        try:
            cover_hash = hashlib.sha256(bytes(cover_data)).hexdigest()
        except (TypeError, ValueError):
            cover_hash = ""

    review_note: str | None = None
    try:
        from calibre_plugins.deluxe_sync.settings import get_column_mappings

        review_lookup = str(get_column_mappings().get("review_note") or "").strip()
        if review_lookup:
            review_note = str(field_for(review_lookup, "") or "")
    except Exception:
        review_note = None

    return CalibreBook(
        library_uuid=str(library_uuid or "").strip(),
        book_uuid=book_uuid,
        book_id=int(book_id),
        title=str(field_for("title", "") or "").strip(),
        authors=_clean_authors(field_for("authors", ())),
        identifiers=_clean_identifiers(field_for("identifiers", {})),
        series=str(field_for("series", "") or "").strip(),
        series_index=_series_index(field_for("series_index", None)),
        rating=_rating(field_for("rating", 0)),
        review_note=review_note,
        filenames=tuple(filenames),
        cover_hash=cover_hash,
    )


def selected_calibre_books(gui: Any) -> list[CalibreBook]:
    """Return only the books currently selected in Calibre's library view."""

    db = getattr(gui, "current_db", None)
    library_view = getattr(gui, "library_view", None)
    if db is None or library_view is None:
        raise RuntimeError("Open a Calibre library before matching books.")

    library_uuid = str(getattr(db, "library_id", "") or "").strip()
    if not library_uuid:
        raise RuntimeError("The active Calibre library does not have a library UUID.")

    try:
        selected_ids = list(library_view.get_selected_ids(as_set=False) or ())
    except Exception as error:
        raise RuntimeError("Could not read the current Calibre book selection.") from error

    books: list[CalibreBook] = []
    for book_id in selected_ids:
        books.append(calibre_book_from_db(db, library_uuid, int(book_id)))
    return books


def library_book_count(gui: Any) -> int:
    """Return the number of books in the active Calibre library without loading metadata."""

    db = getattr(gui, "current_db", None)
    api = getattr(db, "new_api", None) if db is not None else None
    if db is None or api is None:
        raise RuntimeError("Open a Calibre library before syncing books.")
    try:
        return len(api.all_book_ids())
    except Exception as error:
        raise RuntimeError("Could not read the active Calibre library.") from error


def linked_calibre_books(
    gui: Any,
    bindings: dict[str, dict[str, Any]],
) -> list[CalibreBook]:
    """Resolve enrolled bindings to current Calibre rows using UUID as durable identity."""

    db = getattr(gui, "current_db", None)
    api = getattr(db, "new_api", None) if db is not None else None
    if db is None or api is None:
        raise RuntimeError("Open a Calibre library before syncing books.")

    library_uuid = str(getattr(db, "library_id", "") or "").strip()
    if not library_uuid:
        raise RuntimeError("The active Calibre library does not have a library UUID.")

    targets: dict[str, int | None] = {}
    for book_uuid, binding in bindings.items():
        if not isinstance(binding, dict):
            continue
        if binding.get("enrolled") is not True:
            continue
        if not str(binding.get("server_document") or "").strip():
            continue
        try:
            cached_id = int(binding.get("calibre_book_id"))
        except (TypeError, ValueError):
            cached_id = None
        targets[str(book_uuid)] = cached_id

    resolved: dict[str, int] = {}
    unresolved: set[str] = set()
    for book_uuid, cached_id in targets.items():
        if cached_id is None:
            unresolved.add(book_uuid)
            continue
        try:
            current_uuid = str(
                api.field_for("uuid", cached_id, default_value="") or ""
            ).strip()
        except Exception:
            current_uuid = ""
        if current_uuid == book_uuid:
            resolved[book_uuid] = cached_id
        else:
            unresolved.add(book_uuid)

    if unresolved:
        try:
            all_ids = tuple(api.all_book_ids())
        except Exception as error:
            raise RuntimeError("Could not resolve linked Calibre books.") from error
        for book_id in all_ids:
            try:
                book_uuid = str(
                    api.field_for("uuid", book_id, default_value="") or ""
                ).strip()
            except Exception:
                continue
            if book_uuid in unresolved:
                resolved[book_uuid] = int(book_id)
                unresolved.remove(book_uuid)
                if not unresolved:
                    break

    return [
        calibre_book_from_db(db, library_uuid, book_id, include_cover_hash=False)
        for _book_uuid, book_id in sorted(
            resolved.items(),
            key=lambda item: item[1],
        )
    ]
